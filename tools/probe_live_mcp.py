"""Check the existing bridge through stdio MCP without starting the game or DLL.

Check the compilation marker by default. --reload explicitly replaces only
the prepared fixture constant after verifying SHA-256. --observations
checks snapshots/observation of user-selected properties without changing them.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from prepare_compile_probe import METHOD_NAME, prepare_probe

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ProbeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def check_signature(functions: dict) -> dict:
    """Do not call a method before checking its actually read signature."""
    matches = [item for item in functions.get("items", []) if item.get("name") == METHOD_NAME]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "The exact probe method was not uniquely found")
    method = matches[0]
    parameters = method.get("parameters")
    if not isinstance(parameters, list) or len(parameters) != 1:
        raise ProbeError("probe_signature_mismatch", "The probe method must have only a return parameter")
    result = parameters[0]
    if (result.get("return") is not True or result.get("type") != "IntProperty"
            or result.get("element_size") != 4 or result.get("reference") is True
            or method.get("parameter_size") != 4):
        raise ProbeError("probe_signature_mismatch", "A scalar int32 result without input parameters was not confirmed")
    return method


async def run_probe(args: argparse.Namespace) -> dict:
    if args.reload and (args.probe_workspace is None or args.expected_sha256 is None):
        raise ProbeError("invalid_reload", "Reload requires --probe-workspace and --expected-sha256")
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT),
    )
    report = {"source": "real_stdio_mcp", "game_launched": False, "dll_loaded_by_probe": False,
              "compiled_probe": False, "reloaded_probe": False}
    previous_watch_id = None
    previous_handle = None
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            report["server_version"] = initialized.server_info.version
            report["tool_count"] = len((await client.list_tools()).tools)

            async def call(name: str, arguments: dict) -> dict:
                result = await client.call_tool(name, arguments)
                if result.is_error:
                    message = next((item.text for item in result.content if item.type == "text"), "MCP error")
                    raise ProbeError("mcp_call_failed", f"{name}: {message}")
                if not isinstance(result.structured_content, dict):
                    raise ProbeError("mcp_result_invalid", f"{name}: the response is not a structured object")
                return result.structured_content

            status = await call("g1r_native_status", {})
            if args.enable and not status.get("hook_installed"):
                await call("g1r_native_enable", {})
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    status = await call("g1r_native_status", {})
                    if status.get("ready"):
                        break
                    await asyncio.sleep(0.1)
            report["status"] = status
            if not status.get("ready"):
                raise ProbeError("game_not_ready", "Game Tick was not confirmed; enable the existing bridge")

            found = await call("g1r_find_objects", {"query": "Default__G1RMCPCompileProbe",
                                                    "class_query": "G1RMCPCompileProbe", "limit": 50})
            matches = [item for item in found.get("items", [])
                       if item.get("name") == "Default__G1RMCPCompileProbe"]
            if len(matches) != 1:
                raise ProbeError("probe_object_absent", "The exact compilation marker CDO was not uniquely found")
            target = matches[0]
            report["probe_object"] = target
            functions = await call("g1r_object_functions", {"handle": target["handle"], "limit": 100})
            report["signature"] = check_signature(functions)
            baseline = await call("g1r_invoke", {"handle": target["handle"], "function": METHOD_NAME, "arguments": {}})
            report["baseline_call"] = baseline
            if baseline.get("executed") is not True or type(baseline.get("result")) is not int or baseline["result"] != args.expected_value:
                raise ProbeError("probe_value_mismatch", "The marker returned a value different from the expected value")
            report["compiled_probe"] = True

            if args.observations:
                if not args.observe_query or not args.property:
                    raise ProbeError("invalid_observation", "Observations require --observe-query and at least one --property")
                found = await call("g1r_find_objects", {"query": args.observe_query,
                                                        "class_query": args.observe_class or "", "limit": 50})
                exact = [item for item in found.get("items", []) if item.get("name") == args.observe_query]
                if len(exact) != 1:
                    raise ProbeError("observation_object_absent", "The exact observed object was not uniquely found")
                observed = exact[0]
                previous_handle = observed["handle"]
                targets = [{"handle": observed["handle"], "properties": args.property}]
                before = await call("g1r_snapshot", {"targets": targets})
                watch = await call("g1r_watch_start", {"handle": observed["handle"], "properties": args.property,
                                                       "duration_ms": 1200, "interval_ms": 100, "capacity": 4})
                watch_id = watch["watch_id"]
                try:
                    await asyncio.sleep(0.35)
                    events = await call("g1r_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
                    stopped = await call("g1r_watch_stop", {"watch_id": watch_id})
                    final = await call("g1r_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
                    after = await call("g1r_snapshot", {"targets": targets})
                    comparison = await call("g1r_compare_snapshots", {"before": before, "after": after, "limit": 100})
                    report["observations"] = {"object": observed, "before": before, "watch": watch,
                                               "events": events, "stopped": stopped, "final": final,
                                               "after": after, "comparison": comparison}
                finally:
                    await call("g1r_watch_stop", {"watch_id": watch_id})
                    await call("g1r_watch_forget", {"watch_id": watch_id})
                disconnect_watch = await call("g1r_watch_start", {"handle": observed["handle"], "properties": args.property,
                                                                  "duration_ms": 2000, "interval_ms": 100, "capacity": 4})
                previous_watch_id = disconnect_watch["watch_id"]
                report["disconnect_watch"] = disconnect_watch

            if args.reload:
                if args.expected_value != 1:
                    raise ProbeError("invalid_reload", "The reload phase starts only with a confirmed baseline of 1")
                prepared = prepare_probe(args.probe_workspace, phase="reload", expected_sha256=args.expected_sha256)
                report["reload_preparation"] = prepared
                # Only the read-only marker is repeated; game actions are not repeated.
                deadline = time.monotonic() + 5
                attempts = 0
                while time.monotonic() < deadline:
                    await asyncio.sleep(0.25)
                    functions = await call("g1r_object_functions", {"handle": target["handle"], "limit": 100})
                    check_signature(functions)
                    result = await call("g1r_invoke", {"handle": target["handle"], "function": METHOD_NAME, "arguments": {}})
                    attempts += 1
                    report["reload_call"] = result
                    if result.get("executed") is True and type(result.get("result")) is int and result["result"] == 2:
                        report["reloaded_probe"] = True
                        break
                report["reload_readonly_probe_attempts"] = attempts
                if not report["reloaded_probe"]:
                    report["reload_result"] = "not_confirmed_within_probe_deadline"
    if args.observations:
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                await client.initialize()
                await client.call_tool("g1r_native_status", {})
                stale = {}
                for name, arguments in (
                        ("g1r_watch_read", {"watch_id": previous_watch_id, "after_seq": 0, "limit": 10}),
                        ("g1r_object_properties", {"handle": previous_handle, "limit": 2})):
                    result = await client.call_tool(name, arguments)
                    message = next((item.text for item in result.content if item.type == "text"), "")
                    stale[name] = {"is_error": result.is_error, "message": message}
                    if not result.is_error:
                        raise ProbeError("owner_boundary_failed", "The new MCP session accepted a reference from the previous session")
                report["new_session_rejections"] = stale
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a ready game session through a real MCP client")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    parser.add_argument("--enable", action="store_true", help="Explicitly enable the existing DLL queue")
    parser.add_argument("--expected-value", type=int, choices=(1, 2), default=1)
    parser.add_argument("--reload", action="store_true", help="Explicitly change the baseline constant and check reload")
    parser.add_argument("--probe-workspace", type=Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--observations", action="store_true")
    parser.add_argument("--observe-query", help="Exact object name for read-only snapshots/observation")
    parser.add_argument("--observe-class")
    parser.add_argument("--property", action="append", help="Observed property name; may be repeated")
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(args))
        print(json.dumps(report, ensure_ascii=False))
        return 0 if not args.reload or report.get("reloaded_probe") else 3
    except Exception as error:
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
