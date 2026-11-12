"""Subnautica 2 runtime MCP frontend adapted from the G1R implementation."""

import argparse
import asyncio
import json
import hashlib
import logging
import math
import time
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
from .workspace import SourceWorkspace, WorkspaceError
from .source_identity import inspect as inspect_source_identity
from .package_export import ExportError, export_sources, verify_source_export
from .build_workflow import BuildError, BuildWorkflow
from .runtime_lifecycle import RuntimeLifecycle
from .mod_projects import ModProjects, ModProjectError
from .mod_runtime import ModRuntime


def _tool_failure(error: SnapshotError | DevelopmentError | WorkspaceError | ExportError) -> ToolError:
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
    sources = None
    builds = None
    mods = None

    def mod_projects() -> ModProjects:
        nonlocal mods
        if mods is None:
            mods = ModProjects(source_workspace(), configured_runtime_root)
        return mods

    def mod_project_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(mod_projects(), operation)(*args, **kwargs)
        except (ModProjectError, WorkspaceError) as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": getattr(error, "receipt", None)}, ensure_ascii=False)) from None

    def build_service() -> BuildWorkflow:
        nonlocal builds
        with service_lock:
            if builds is None:
                builds = BuildWorkflow(configured_runtime_root, workspace_root=workspace_root)
            return builds

    def build_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(build_service(), operation)(*args, **kwargs)
        except BuildError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": error.receipt}, ensure_ascii=False)) from None

    def source_workspace() -> SourceWorkspace:
        nonlocal sources
        with service_lock:
            if sources is None:
                protected = (configured_game_root,) if configured_game_root.is_dir() else ()
                sources = SourceWorkspace(workspace_root, protected_roots=protected)
            return sources

    def source_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(source_workspace(), operation)(*args, **kwargs)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

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
        instructions=("Research game interfaces and develop, prototype and debug mods using the G1R-based native bridge. "
                      "Source tools operate on the configured workspace without assuming game source or a mod format. "
                      "Begin with sn2_status and sn2_capabilities. "
                      "Inspect sn2_target_processes before selecting an exact target. DLL loading, transport, "
                      "discovery and observed Tick are distinct states. Read current object/function metadata "
                      "before an explicit sn2_invoke. Handles and observations belong to a native session; "
                      "refresh after reconnect. Investigate uncertain effects; never automatically replay commands."))
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    source_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @server.tool(annotations=read_only)
    def sn2_build_recipe(target: str = "all", configuration: str = "Release",
                         run_tests: StrictBool = True, project_path: str = "native") -> dict[str, Any]:
        """Identify the fixed CMake/MSVC build, source inputs and toolchain without compiling."""
        return build_call("recipe", target=target, configuration=configuration, run_tests=run_tests,
                          project_path=project_path)

    @server.tool(annotations=source_write)
    def sn2_build(target: str = "all", configuration: str = "Release",
                  run_tests: StrictBool = True, timeout_seconds: float = 900,
                  parallel: StrictInt | None = None, project_path: str = "native") -> dict[str, Any]:
        """Build captured native inputs and return complete stage, diagnostic and artifact receipts."""
        return build_call("build", target=target, configuration=configuration, run_tests=run_tests,
                          timeout_seconds=timeout_seconds, parallel=parallel, project_path=project_path)

    @server.tool(annotations=read_only)
    def sn2_build_status(build_id: str | None = None) -> dict[str, Any]:
        """Read an actual recorded build receipt; no resident worker is implied."""
        return build_call("status", build_id=build_id)

    @server.tool(annotations=read_only)
    def sn2_compile_diagnostics(build_id: str | None = None) -> dict[str, Any]:
        """Read complete recorded MSVC/CMake diagnostics correlated to captured source bytes."""
        return build_call("diagnostics", build_id=build_id)

    @server.tool(annotations=source_write)
    def sn2_create_mod_project(project_path: str, mod_id: str) -> dict[str, Any]:
        """Create an editable standalone native widget modification using the copied Unreal SDK."""
        return mod_project_call("create", project_path, mod_id)

    @server.tool(annotations=read_only)
    def sn2_validate_mod_project(project_path: str) -> dict[str, Any]:
        """Validate native project inputs and the supported initialization/build contract."""
        return mod_project_call("validate", project_path)

    @server.tool(annotations=source_write)
    def sn2_package_artifact(build_receipt_path: str, destination: str) -> dict[str, Any]:
        """Package the actual compiled modification and its source/build identities for workspace loading."""
        return mod_project_call("package", build_receipt_path, destination)

    @server.tool(annotations=read_only)
    def sn2_verify_artifact_package(package_path: str) -> dict[str, Any]:
        """Verify complete compiled package inventory before activation."""
        return mod_project_call("verify", package_path)

    def watch_owner(ctx: Context | None) -> str:
        return "shared_native_connection"

    def native_request(operation: str, params: dict[str, Any],
                       timeout_seconds: float | None = None, *,
                       target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with native_lock:
                client = native
                target = target_process if target_process is not None else selected_target
                target = None if target is None else dict(target)
            if target is not None:
                params = {**params, "expected_process_id": target["pid"],
                          "expected_creation_filetime": target["creation_time"]}
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
        with native_lock:
            selected_target = target
        return target

    def invalidate_native() -> None:
        nonlocal selected_target, native
        with native_lock:
            native.close()
            native = NativeClient(port=native_port)
            selected_target = None

    def lifecycle_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(RuntimeLifecycle(runtime_service()), operation)(*args, **kwargs)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None

    def terminating_lifecycle_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            result = getattr(RuntimeLifecycle(runtime_service()), operation)(*args, **kwargs)
        except DevelopmentError as error:
            receipt = error.receipt if isinstance(error.receipt, dict) else {}
            # Validation/snapshot failures and a known not-started command leave
            # the current connection, handles and watches intact. A completed
            # stop or uncertain command must invalidate the previous session.
            if error.outcome_unknown or receipt.get("stop_receipt") or receipt.get("cleanup"):
                invalidate_native()
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None
        invalidate_native()
        return result

    def checked_mod_package(package_path: str, expected_manifest_sha256: str | None) -> dict[str, Any]:
        package = mod_project_call("verify", package_path)
        if expected_manifest_sha256 is not None and package["manifest_sha256"] != expected_manifest_sha256:
            raise ToolError(json.dumps({"code": "package_changed", "message": "The verified package manifest differs from the selected revision"}))
        return package

    def mod_runtime_call(operation: str, target_process: dict[str, Any], package_path: str,
                         expected_manifest_sha256: str | None, timeout_seconds: float,
                         request_id: int | None = None) -> dict[str, Any]:
        package = checked_mod_package(package_path, expected_manifest_sha256)
        artifact = package["artifact"]
        try:
            service = ModRuntime(runtime_service(), workspace_root=workspace_root)
            kwargs = {"timeout_seconds": timeout_seconds}
            if operation == "status":
                kwargs["request_id"] = request_id
            result = getattr(service, operation)(target_process, artifact["path"], artifact["sha256"], **kwargs)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None
        status = result.get("mod_receipt", {}).get("status", {})
        metadata = package["metadata"]
        if status and (status.get("mod_id") != metadata["mod_id"]
                       or status.get("source_revision") != package["source_revision"]):
            raise ToolError(json.dumps({"code": "mod_revision_mismatch", "message": "The initialized mod does not match the verified package", "receipt": result}))
        return {**result, "package": package}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_mod_load(target_process: dict[str, Any], package_path: str,
                      expected_manifest_sha256: str | None = None,
                      timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Verify and initialize the selected standalone compiled modification; inspect status for activation."""
        return mod_runtime_call("load", target_process, package_path, expected_manifest_sha256, timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_mod_status(target_process: dict[str, Any], package_path: str,
                       expected_manifest_sha256: str | None = None,
                       timeout_seconds: float = 30.0, request_id: StrictInt | None = None) -> dict[str, Any]:
        """Inspect exact mod revision, initialization, semantic effect and the original command receipt."""
        return mod_runtime_call("status", target_process, package_path, expected_manifest_sha256,
                                timeout_seconds, request_id)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_disable(target_process: dict[str, Any], package_path: str,
                        expected_manifest_sha256: str | None = None,
                        timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Stop the mod action and request baseline restoration; module and hook remain until process exit."""
        return mod_runtime_call("disable", target_process, package_path, expected_manifest_sha256, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_reload(target_process: dict[str, Any], package_path: str,
                       bridge_dll_path: str, expected_bridge_sha256: str,
                       expected_manifest_sha256: str | None = None,
                       discovery_timeout_seconds: float = 30.0,
                       readiness_timeout_seconds: float = 30.0,
                       timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Iterate a compiled revision by confirmed process restart, fresh bridge readiness and mod initialization."""
        for value in (readiness_timeout_seconds, discovery_timeout_seconds, timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ToolError(json.dumps({"code": "invalid_timeout", "message": "All lifecycle timeouts must be positive and finite"}))
        package = checked_mod_package(package_path, expected_manifest_sha256)
        # Validate the selected artifact before terminating the current process.
        try:
            bridge = Path(bridge_dll_path).resolve(strict=True)
            if not bridge.is_relative_to(configured_runtime_root) or hashlib.sha256(bridge.read_bytes()).hexdigest() != expected_bridge_sha256:
                raise ValueError("bridge_changed")
        except (OSError, ValueError):
            raise ToolError(json.dumps({"code": "bridge_changed", "message": "Select an existing workspace bridge DLL with the expected SHA-256"})) from None
        restart = sn2_development_restart(target_process, discovery_timeout_seconds)
        process = restart["process"]
        progress = {"restart": restart, "process": process, "stage": "bridge_load"}
        try:
            bridge_load = sn2_native_load(process, str(bridge), expected_bridge_sha256)
            progress.update(bridge_load=bridge_load, stage="transport")
            deadline = time.monotonic() + readiness_timeout_seconds
            while True:
                try:
                    status = sn2_native_status(process)
                    break
                except ToolError as error:
                    detail = json.loads(str(error))
                    if detail.get("code") != "native_unavailable" or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            progress.update(native_status=status, stage="enable")
            enabled = sn2_native_enable(process, status["session_id"])
            progress.update(native_status=enabled, stage="readiness")
            while not enabled.get("ready", False) and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                enabled = sn2_native_status(process)
                progress["native_status"] = enabled
            if not enabled.get("ready", False):
                raise ToolError(json.dumps({"code": "bridge_readiness_unconfirmed", "message": "The fresh bridge did not establish Tick/reflection readiness"}))
            progress["stage"] = "mod_initialization"
            mod = sn2_mod_load(process, package_path, package["manifest_sha256"], timeout_seconds)
        except ToolError as error:
            try:
                detail = json.loads(str(error))
            except (TypeError, ValueError):
                detail = {"code": "revision_iteration_unconfirmed", "message": str(error)}
            detail["iteration_receipt"] = progress
            raise ToolError(json.dumps(detail, ensure_ascii=False)) from None
        return {"state": "revision_initialization_requested", "iteration": "controlled_process_restart",
                "hot_reload": False, "process": process, "previous_process": target_process,
                "restart": restart, "bridge_load": bridge_load, "native_status": enabled, "mod": mod}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_development_start(discovery_timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Launch Subnautica and identify the fresh Shipping process; loading and readiness remain separate."""
        return lifecycle_call("start", discovery_timeout_seconds=discovery_timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_development_stop(target_process: dict[str, Any]) -> dict[str, Any]:
        """Stop the exact selected game instance and confirm process cleanup."""
        return terminating_lifecycle_call("stop", target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_development_restart(target_process: dict[str, Any],
                                discovery_timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Confirm the selected instance stopped, then launch and identify a fresh instance."""
        return terminating_lifecycle_call("restart", target_process,
                                          discovery_timeout_seconds=discovery_timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_status() -> dict[str, Any]:
        """Report local frontend configuration; this does not probe the game."""
        return {"server": {"name": "Subnautica 2 MCP", "version": __version__, "transport": transport, "ready": True},
                "workspace": {"root": str(workspace_root), "installed_game_deployment": False},
                "runtime_root": str(configured_runtime_root), "game_root": str(configured_game_root),
                "native": {"endpoint": f"127.0.0.1:{native_port}", "connection_checked": False},
                "target_process": selected_target, "game_ready": False, "game_ready_known": False}

    @server.tool(annotations=read_only)
    def sn2_source_identity(relative_path: str = ".", configured_revision: str | None = None) -> dict[str, Any]:
        """Read workspace Git facts; a revision label does not establish game compatibility."""
        try:
            root = source_workspace()._selected(relative_path)
            return {"source": "workspace_source", **inspect_source_identity(root, configured_revision)}
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_list_source_files(relative_path: str = ".", limit: StrictInt = 100, offset: StrictInt = 0) -> dict[str, Any]:
        """List workspace files, including binary entries; omit repository internals and build outputs."""
        return source_call("list_files", relative_path, limit, offset)

    @server.tool(annotations=read_only)
    def sn2_find_source_files(query: str, relative_path: str = ".", limit: StrictInt = 50, offset: StrictInt = 0) -> dict[str, Any]:
        """Find literal workspace paths without assuming an AngelScript or game source tree."""
        return source_call("search_files", query, relative_path, limit, offset)

    @server.tool(annotations=read_only)
    def sn2_search_source(query: str, relative_path: str = ".", limit: StrictInt = 50,
                          case_sensitive: StrictBool = False, offset: StrictInt = 0) -> dict[str, Any]:
        """Find literal UTF-8 source lines and report files that could not be decoded or read."""
        return source_call("search_text", query, relative_path, limit, case_sensitive, offset)

    @server.tool(annotations=read_only)
    def sn2_read_source(file_path: str, start_line: StrictInt = 1, line_count: StrictInt = 100) -> dict[str, Any]:
        """Read display lines with the complete source byte hash; no declaration parser is implied."""
        return source_call("read_file", file_path, start_line, line_count)

    @server.tool(annotations=read_only)
    def sn2_read_source_exact(file_path: str) -> dict[str, Any]:
        """Read complete UTF-8 source preserving BOM, line endings and EOF for exact revision."""
        return source_call("read_file_exact", file_path)

    @server.tool(annotations=source_write)
    def sn2_write_source(file_path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
        """Create source or replace bytes matching their SHA-256; no compilation or game write occurs."""
        return source_call("write_file", file_path, content, expected_sha256)

    @server.tool(annotations=source_write)
    def sn2_create_source_directory(directory_path: str) -> dict[str, Any]:
        """Create a selected workspace directory without inventing a game mod descriptor or entry point."""
        return source_call("create_directory", directory_path)

    @server.tool(annotations=source_write)
    def sn2_export_sources(source_files: list[str], destination: str) -> dict[str, Any]:
        """Export exact selected source bytes to a ZIP and hash manifest; this is not an installable mod."""
        try:
            return export_sources(source_workspace(), source_files, destination)
        except (WorkspaceError, ExportError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_verify_source_export(relative_path: str) -> dict[str, Any]:
        """Verify a workspace source ZIP and inventory without compiling, installing or loading it."""
        try:
            from .workspace import _parts, _checked
            parts = _parts(relative_path)
            if len(parts) < 2 or parts[0] != "artifacts":
                raise WorkspaceError("unsafe_path", "Select an export under workspace artifacts")
            root = source_workspace()._unprotected(_checked(workspace_root.joinpath(*parts), workspace_root, directory=True))
            return verify_source_export(root)
        except (WorkspaceError, ExportError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_development_recipe(source_files: list[str], targets: list[dict[str, Any]] | None = None,
                               timeout_seconds: float | None = None) -> dict[str, Any]:
        """Relate exact workspace revisions to current reflected interfaces and runtime diagnostics.

        Each optional target has a current handle and explicit property/function
        names. This records evidence; it never builds, loads or invokes a mod.
        """
        if not source_files or len(set(source_files)) != len(source_files):
            raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select nonempty unique source paths"}))
        selected = []
        for path in source_files:
            value = source_call("read_file_exact", path)
            selected.append({key: value[key] for key in ("path", "sha256", "bytes")})
        interfaces = []
        initial_runtime_state = sn2_native_status() if targets is not None else None
        if targets is not None:
            if not targets:
                raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select nonempty runtime targets"}))
            for target in targets:
                if (not isinstance(target, dict) or set(target) - {"handle", "properties", "functions"}
                        or not isinstance(target.get("handle"), str) or not target["handle"]
                        or not (target.get("properties") or target.get("functions"))):
                    raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select a handle and explicit property or function names"}))
                for key in ("properties", "functions"):
                    names = target.get(key, [])
                    if (not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names)
                            or len(set(names)) != len(names)):
                        raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Interface names must be unique nonempty strings"}))
            for target in targets:
                captured = {"handle": target["handle"], "functions": []}
                if target.get("properties"):
                    captured["properties"] = native_request("properties", {
                        "handle": target["handle"], "property_names": target["properties"],
                        "limit": len(target["properties"])}, timeout_seconds)
                for name in target.get("functions", []):
                    page = 0
                    found = []
                    while True:
                        params = {"handle": target["handle"], "function_query": name, "limit": 100}
                        if page:
                            params["offset"] = page
                        value = native_request("functions", params, timeout_seconds)
                        found.extend(item for item in value.get("items", []) if item.get("name") == name)
                        continuation = value.get("next_offset")
                        if continuation is None:
                            break
                        if type(continuation) is not int or continuation <= page:
                            raise ToolError(json.dumps({"code": "invalid_continuation", "message": "The function page has no forward continuation"}))
                        page = continuation
                    captured["functions"].append({"requested_name": name, "matches": found,
                        "status": "found" if found else "missing"})
                interfaces.append(captured)
        after = [source_call("read_file_exact", item["path"])["sha256"] for item in selected]
        source_stable = after == [item["sha256"] for item in selected]
        identity = sn2_source_identity()
        runtime_state = sn2_native_status() if targets is not None else None
        runtime_stable = None
        if targets is not None:
            identity_keys = ("session_id", "process_id", "process_creation_filetime")
            initial_world = initial_runtime_state.get("world")
            final_world = runtime_state.get("world")
            identity_known = all(state.get(key) is not None
                for state in (initial_runtime_state, runtime_state) for key in identity_keys)
            world_known = all(isinstance(world, dict) and world.get("state") == "observed"
                and type(world.get("generation")) is int for world in (initial_world, final_world))
            if identity_known and world_known:
                runtime_stable = (all(initial_runtime_state.get(key) == runtime_state.get(key)
                    for key in (*identity_keys, "reflection_generation")) and initial_world == final_world)
        inventory = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"kind": "mod_development_evidence", "source_files": selected,
            "source_inventory_sha256": hashlib.sha256(inventory).hexdigest(), "source_stable": source_stable,
            "source_identity": identity, "interfaces": interfaces, "native_status": runtime_state,
            "initial_native_status": initial_runtime_state, "runtime_identity_stable": runtime_stable,
            "snapshot_kind": "sequential_source_and_runtime_reads", "compiled": False,
            "mod_loaded": False, "mod_execution_checked": False,
            "next_steps": ["Inspect captured signatures and unsupported types", "Prototype explicitly with sn2_invoke",
                           "Compare sn2_snapshot results and inspect sn2_watch events", "Revise source with its exact SHA-256"],
            "limits": ["Workspace text is not shipped game source", "Reflection is not a C++ header or compiler",
                       "This evidence is not an installable mod or a source-level debugger"]}

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
            target = select_target(target_process)
        else:
            with native_lock:
                target = None if selected_target is None else dict(selected_target)
        result = native_request("status", {}, target_process=target)
        if target is not None and (result.get("process_id") != target["pid"]
                or str(result.get("process_creation_filetime")) != target["creation_time"]):
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
