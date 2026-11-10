"""Subnautica 2 runtime MCP frontend adapted from the G1R implementation."""

import argparse
import asyncio
import json
import logging
from pathlib import Path
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictInt, StrictStr

from . import __version__
from .native_client import NativeClient, NativeError
from .snapshots import SnapshotError, compare_snapshots
from .development import DevelopmentError, RuntimeService
from .tool_catalog import build_tool_contracts


def _tool_failure(error: SnapshotError | DevelopmentError) -> ToolError:
    return ToolError(json.dumps({"code": error.code, "message": str(error)}, ensure_ascii=False))

class _EnglishOperatingSystemLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            display = traceback.TracebackException(*record.exc_info)

            def normalize(shown: traceback.TracebackException, error: BaseException) -> None:
                if isinstance(error, OSError) and type(error).__module__ == "builtins":
                    shown._str = f"Operating system error (errno={error.errno}, winerror={getattr(error, 'winerror', None)})"
                if shown.__cause__ is not None:
                    normalize(shown.__cause__, error.__cause__)
                elif shown.__context__ is not None and not shown.__suppress_context__:
                    normalize(shown.__context__, error.__context__)
                if shown.exceptions is not None:
                    for child, original in zip(shown.exceptions, error.exceptions):
                        normalize(child, original)

            normalize(display, record.exc_info[1])
            record.exc_text = "".join(display.format())
        return True


def create_server(workspace_root: Path, native_port: int = 27223,
                  game_root: Path | None = None, *, transport: str = "stdio",
                  runtime_root: Path | None = None) -> MCPServer:
    """Create the inherited runtime surface without Gothic package services."""
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("Transport must be stdio or streamable-http")
    workspace_root = workspace_root.absolute()
    if not workspace_root.is_dir():
        raise ValueError("Workspace directory does not exist")
    configured_runtime_root = runtime_root.resolve() if runtime_root is not None else Path(__file__).resolve().parents[2]
    configured_game_root = (game_root or Path(r"E:\Games\Subnautica 2")).absolute()
    native = NativeClient(port=native_port)
    native_lock = threading.Lock()
    service_lock = threading.Lock()
    runtime = None
    selected_target = None

    def runtime_service() -> RuntimeService:
        nonlocal runtime
        with service_lock:
            if runtime is None:
                runtime = RuntimeService(repository_root=configured_runtime_root, game_root=configured_game_root)
            return runtime

    @asynccontextmanager
    async def server_lifespan(_server: MCPServer):
        try:
            yield
        finally:
            try:
                native.close()
            finally:
                with service_lock:
                    if runtime is not None:
                        runtime.close()

    server = MCPServer("Subnautica 2 MCP", version=__version__, log_level="WARNING", lifespan=server_lifespan,
        instructions=("Native runtime bridge adapted from G1R. Begin with sn2_status and sn2_capabilities. "
                      "Inspect sn2_target_processes before selecting an exact target. DLL loading, transport, "
                      "discovery and observed Tick are distinct states. Read current object/function metadata "
                      "before an explicit sn2_invoke. Handles and observations belong to a native session; "
                      "refresh after reconnect. Investigate uncertain effects; never automatically replay commands."))
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def watch_owner(ctx: Context | None) -> str:
        return "shared_native_connection"

    def native_request(operation: str, params: dict[str, Any],
                       timeout_seconds: float | None = None) -> dict[str, Any]:
        try:
            with native_lock:
                client = native
            if selected_target is not None:
                params = {**params, "expected_process_id": selected_target["pid"],
                          "expected_creation_filetime": selected_target["creation_time"]}
            return client.request(operation, params) if timeout_seconds is None else client.request(operation, params, timeout=timeout_seconds)
        except NativeError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "may_have_executed": error.may_have_executed, "request_id": error.request_id,
                "effect": error.details}, ensure_ascii=False)) from None

    def select_target(target_process: dict[str, Any]) -> dict[str, Any]:
        nonlocal selected_target
        try:
            target = runtime_service().validate_target(target_process)
        except DevelopmentError as error:
            raise _tool_failure(error) from None
        selected_target = target
        return target

    @server.tool(annotations=read_only)
    def sn2_status() -> dict[str, Any]:
        """Report local frontend configuration; this does not probe the game."""
        return {"server": {"name": "Subnautica 2 MCP", "version": __version__, "transport": transport, "ready": True},
                "workspace": {"root": str(workspace_root), "installed_game_deployment": False},
                "runtime_root": str(configured_runtime_root), "game_root": str(configured_game_root),
                "native": {"endpoint": f"127.0.0.1:{native_port}", "connection_checked": False},
                "target_process": selected_target, "game_ready": False, "game_ready_known": False}

    @server.tool(annotations=read_only)
    async def sn2_capabilities(target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return registered contracts and distinguish source support from target verification."""
        observed = None
        if target_process is not None:
            select_target(target_process)
            observed = sn2_native_status()
        tools = build_tool_contracts(await server.list_tools())
        return {"catalog_revision": __version__, "tool_contracts": tools,
                "target_process": selected_target, "native_status": observed,
                "capabilities": {"frontend": {"state": "verified", "source": "registered_mcp_tools"},
                    "target_abi": {"state": "unverified" if observed is None else observed.get("abi_state", "unverified"),
                                   "reason": "per_shape_runtime_receipts_required"}},
                "accepted_types": {} if observed is None else observed.get("accepted_types", {}),
                "automatic_retries": False}

    @server.tool(annotations=read_only)
    def sn2_target_processes(executable: str | None = None) -> dict[str, Any]:
        """Enumerate selected Subnautica processes without launching or choosing an instance."""
        try:
            return runtime_service().process_state(executable)
        except DevelopmentError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_native_load(target_process: dict[str, Any], dll_path: str, expected_dll_sha256: str) -> dict[str, Any]:
        """Load the selected DLL; preserve unknown outcomes and never imply executor readiness."""
        target = select_target(target_process)
        try:
            return runtime_service().load_native(target, dll_path, expected_dll_sha256)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt,
                "do_not_retry": error.outcome_unknown}, ensure_ascii=False)) from None

    @server.tool(annotations=read_only)
    def sn2_native_status(target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read native handshake/discovery/Tick state independently from executor readiness."""
        if target_process is not None:
            select_target(target_process)
        result = native_request("status", {})
        if selected_target is not None and (result.get("process_id") != selected_target["pid"]
                or str(result.get("process_creation_filetime")) != selected_target["creation_time"]):
            raise ToolError(json.dumps({"code": "native_process_mismatch", "message": "The native connection belongs to another process"}))
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def sn2_native_enable(target_process: dict[str, Any], expected_native_session_id: str) -> dict[str, Any]:
        """Enable the target Tick instrumentation in the selected loaded native session."""
        target = select_target(target_process)
        state = native_request("status", {})
        if (state.get("process_id") != target["pid"]
                or str(state.get("process_creation_filetime")) != target["creation_time"]
                or state.get("session_id") != expected_native_session_id):
            raise ToolError(json.dumps({"code": "native_identity_mismatch", "message": "The native process or session changed"}))
        return native_request("enable_game", {"expected_process_id": target["pid"], "expected_session_id": expected_native_session_id})

    @server.tool(annotations=read_only)
    def sn2_find_objects(query: str = "", class_query: str = "", limit: int = 50,
                         exact_path: str = "", timeout_seconds: float | None = None,
                         offset: StrictInt = 0) -> dict[str, Any]:
        """Find UObject by partial name/class on game Tick; return opaque handles."""
        params = {"query": query, "class_query": class_query, "limit": limit}
        if offset:
            params["offset"] = offset
        if exact_path:
            params["exact_path"] = exact_path
        return native_request("objects", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_object_properties(handle: str, limit: int = 50,
                              property_names: list[StrictStr] | None = None,
                              timeout_seconds: float | None = None,
                              offset: StrictInt = 0) -> dict[str, Any]:
        """Read handle properties; property_names selects exact unique names in the specified order."""
        params = {"handle": handle, "limit": limit}
        if offset:
            params["offset"] = offset
        if property_names is not None:
            params["property_names"] = property_names
        return native_request("properties", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_array(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                        timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy an array page, including owned reflected struct fields with per-field support/errors; pages do not share an atomic snapshot."""
        return native_request("read_array", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_struct(handle: str, property_name: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy owned reflected fields with per-field support/errors and known nested Vector, Rotator, Transform, LinearColor, and Quat layouts."""
        return native_request("read_struct", {"handle": handle, "property_name": property_name}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_set(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Set<Name> page; offset counts occupied slots, items retain sparse_index, and pages do not share an atomic snapshot."""
        return native_request("read_set", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_map(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Map<Name,Object> page with object descriptors or null; offset counts occupied slots and pages do not share an atomic snapshot."""
        return native_request("read_map", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_object_functions(handle: str, limit: int = 50, function_query: str = "",
                              timeout_seconds: float | None = None,
                              offset: StrictInt = 0) -> dict[str, Any]:
        """Get UFunction, parameters, declared classes, and enums with allowed values."""
        params = {"handle": handle, "limit": limit}
        if offset:
            params["offset"] = offset
        if function_query:
            params["function_query"] = function_query
        return native_request("functions", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_snapshot(targets: list[dict[str, Any]], timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy targets={handle, properties:[names]} on one Tick; reads are sequential."""
        return native_request("snapshot", {"targets": targets}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_snapshot_compare(before: dict[str, Any], after: dict[str, Any],
                              limit: int = 100) -> dict[str, Any]:
        """Compare copies with the selected limit; does not verify the original runtime or provenance of client data."""
        try:
            return compare_snapshots(before, after, limit)
        except SnapshotError as error:
            raise _tool_failure(error) from None


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_start(handle: str, properties: list[str], duration_ms: int,
                         interval_ms: int, capacity: int = 32, ctx: Context = None,
                         timeout_seconds: float | None = None,
                         expected_session_id: str | None = None) -> dict[str, Any]:
        """Observe selected properties on Tick with explicit duration, interval, and buffer capacity."""
        params = {"handle": handle, "properties": properties,
                              "duration_ms": duration_ms, "interval_ms": interval_ms,
                              "capacity": capacity, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_start", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_watch_read(watch_id: str, after_seq: int = 0, limit: int = 100,
                       ctx: Context = None, timeout_seconds: float | None = None,
                       expected_session_id: str | None = None) -> dict[str, Any]:
        """Read events after seq without deleting them; gaps and overflow are explicitly reported."""
        params = {"watch_id": watch_id, "after_seq": after_seq, "limit": limit, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_read", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False))
    def sn2_watch_stop(watch_id: str, ctx: Context = None,
                        timeout_seconds: float | None = None,
                        expected_session_id: str | None = None) -> dict[str, Any]:
        """Stop subsequent reads on the game thread; retain final events."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_stop", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_forget(watch_id: str, ctx: Context = None,
                          timeout_seconds: float | None = None,
                          expected_session_id: str | None = None) -> dict[str, Any]:
        """Release an already completed observation in the current session."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_forget", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_watch_list(ctx: Context = None, timeout_seconds: float | None = None,
                       expected_session_id: str | None = None) -> dict[str, Any]:
        """List observation metadata for the current native connection without events or values."""
        params = {"client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_list", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_resume(watch_id: str, expected_session_id: str | None = None,
                        expected_client_owner: str | None = None, ctx: Context = None,
                         timeout_seconds: float | None = None) -> dict[str, Any]:
        """Update optional observation client metadata within the surviving native session."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        if expected_client_owner is not None:
            params["expected_client_owner"] = expected_client_owner
        return native_request("watch_resume", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def sn2_invoke(handle: str, function: str, arguments: dict[str, Any] | None = None,
                    expected_signature_token: StrictStr | None = None,
                    timeout_seconds: float | None = None) -> dict[str, Any]:
        """Invoke UFunction by signature; Do not retry an unknown effect."""
        params = {"handle": handle, "function": function, "arguments": arguments or {}}
        if expected_signature_token is not None:
            params["expected_signature_token"] = expected_signature_token
        return native_request("invoke", params, timeout_seconds)


    return server


def main(argv: list[str] | None = None) -> int:
    class EnglishArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage: ", "Usage: ", 1).replace("options:", "Options:")

        def error(self, message):
            self.exit(2, "Invalid Subnautica 2 MCP arguments; use --help\n")

    parser = EnglishArgumentParser(description="Subnautica 2 MCP: native runtime server", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, required=True, help="Development workspace root")
    parser.add_argument("--native-port", type=int, default=27223, help="Localhost port of the native bridge")
    parser.add_argument("--game-root", type=Path, help="Selected game root for process identity and native loading")
    parser.add_argument("--runtime-root", type=Path, help="Root of native DLL, loader, and fixed development tools")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio",
                        help="MCP transport; default: stdio")
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1",
                        help="HTTP server address; only 127.0.0.1 is allowed")
    parser.add_argument("--port", type=int, default=27224, help="Streamable HTTP port; default: 27224")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("HTTP port must be in the range 1024–65535")
    try:
        server = create_server(args.workspace_root, args.native_port, args.game_root,
                               transport=args.transport, runtime_root=args.runtime_root)
    except OSError as error:
        print(f"Could not create Subnautica 2 MCP: operating system access error (errno={error.errno}, winerror={getattr(error, 'winerror', None)})", file=sys.stderr)
        return 2
    except (ValueError, NativeError) as error:
        print(f"Could not create Subnautica 2 MCP: {error}", file=sys.stderr)
        return 2
    logging.getLogger("mcp.server.mcpserver.server").addFilter(_EnglishOperatingSystemLog())
    if args.transport == "streamable-http":
        logging.getLogger("asyncio").addFilter(_EnglishOperatingSystemLog())
        server.run(transport="streamable-http", host=args.host, port=args.port,
                   streamable_http_path="/mcp", stateless_http=False, json_response=True,
                   max_request_body_size=sys.maxsize,
                   transport_security=TransportSecuritySettings(
                       enable_dns_rebinding_protection=True,
                       allowed_hosts=[f"{args.host}:{args.port}"],
                       allowed_origins=[f"http://{args.host}:{args.port}"]))
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
