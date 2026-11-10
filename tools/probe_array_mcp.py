"""Check bounded array reads on an owned CDO through a ready MCP connection."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from probe_cleanup import settle_task, deferred_sigint
from probe_object_mcp import ProbeError, _caller, _single_error

HOST = "G1RMCPPODReadProbe"
METHODS = {
    "ReadMarker": ([], ("IntProperty", 4)),
    "ReadNumberCount": ([], ("IntProperty", 4)),
    "ReadEmptyCount": ([], ("IntProperty", 4)),
    "ReadNameCount": ([], ("IntProperty", 4)),
    "ReadNumberAt": ([("Index", "IntProperty", 4)], ("IntProperty", 4)),
    "ReadNameAt": ([("Index", "IntProperty", 4)], ("NameProperty", 8)),
    "SetupFixture": ([], None),
    "ClearFixture": ([], None),
}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST or item.get("class") != HOST or
            item.get("path") != "/Script/Angelscript.Default__" + HOST or
            not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Owned POD probe CDO is not confirmed")
    return item


def check_signature(functions: dict, method_name: str) -> dict:
    if (method_name not in METHODS or not isinstance(functions, dict) or functions.get("truncated") is not False or
            functions.get("scan_complete") is not True or functions.get("scan_truncated") is not False or
            not isinstance(functions.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete probe function catalog is not confirmed")
    matches = [method for method in functions["items"] if isinstance(method, dict) and method.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Owned POD CDO method was not found uniquely")
    method = matches[0]
    params, size = method.get("parameters"), method.get("parameter_size")
    inputs, output = METHODS[method_name]
    if (not isinstance(params, list) or type(size) is not int or not 0 <= size <= 32 or
            method.get("parameters_truncated") is not False or any(not isinstance(param, dict) for param in params)):
        raise ProbeError("probe_signature_mismatch", "Complete bounded method signature is not confirmed")
    actual_inputs = [param for param in params if param.get("return") is False]
    actual_returns = [param for param in params if param.get("return") is True]
    if len(actual_inputs) != len(inputs) or len(actual_returns) != (output is not None) or len(params) != len(inputs) + (output is not None):
        raise ProbeError("probe_signature_mismatch", "Input or return count does not match")
    for param, expected in zip(actual_inputs, inputs):
        if (param.get("name"), param.get("type"), param.get("element_size")) != expected:
            raise ProbeError("probe_signature_mismatch", "Input type or name does not match")
    if output is not None and (actual_returns[0].get("type"), actual_returns[0].get("element_size")) != output:
        raise ProbeError("probe_signature_mismatch", "Return type does not match")
    for param in params:
        offset, element_size = param.get("offset"), param.get("element_size")
        if (type(offset) is not int or type(element_size) is not int or offset < 0 or element_size <= 0 or
                offset + element_size > 32 or param.get("array_dim") != 1 or type(param.get("array_dim")) is not int or
                param.get("reference") is not False or type(param.get("out")) is not bool or
                (param["return"] is False and (param["out"] is not False or offset + element_size > size))):
            raise ProbeError("probe_signature_mismatch", "Array dimension, out/ref flags, or method buffer is not confirmed")
    return method


def check_page(page: dict, target: dict, session_id: str, property_name: str, *,
               offset: int, limit: int, total: int, element_type: str, element_size: int) -> list:
    """Do not report incomplete items or incorrect bounds as a successfully read page."""
    returned = min(limit, max(0, total - offset))
    next_offset = offset + returned if offset + returned < total else None
    if (not isinstance(page, dict) or page.get("source") != "live_native" or
            page.get("session_id") != session_id or page.get("handle") != target["handle"] or
            page.get("property_name") != property_name or page.get("consistency") != "single_tick_sequential" or
            page.get("element_type") != element_type or type(page.get("element_size")) is not int or
            page["element_size"] != element_size or type(page.get("array_dim")) is not int or page["array_dim"] != 1 or
            type(page.get("total_count")) is not int or page["total_count"] != total or
            type(page.get("offset")) is not int or page["offset"] != offset or page.get("next_offset") != next_offset or
            (next_offset is not None and type(page.get("next_offset")) is not int) or
            type(page.get("has_more")) is not bool or page["has_more"] != (next_offset is not None) or
            type(page.get("truncated")) is not bool or page["truncated"] != (next_offset is not None) or
            page.get("partial") is not False or not isinstance(page.get("items"), list) or
            len(page["items"]) != returned):
        raise ProbeError("probe_array_page_invalid", "Array page provenance, completeness, or bounds are not confirmed")
    values = []
    for expected_index, item in enumerate(page["items"], offset):
        if (not isinstance(item, dict) or type(item.get("index")) is not int or item["index"] != expected_index or
                item.get("supported") is not True or "error" in item or "value" not in item or
                (element_type == "IntProperty" and (type(item["value"]) is not int or not -(2**31) <= item["value"] < 2**31)) or
                (element_type == "NameProperty" and type(item["value"]) is not str)):
            raise ProbeError("probe_array_item_invalid", "Array item is missing, unsupported, or has the wrong type")
        values.append(item["value"])
    return values


async def exercise(call) -> dict:
    report = {"checks": [], "cleanup": [], "game_launched": False, "dll_loaded": False}
    cleanup_required = False
    failure = None
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Ready Tick and current session are not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                           "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (not isinstance(found.get("items"), list) or len(found["items"]) != 1 or found.get("truncated") is not False):
            raise ProbeError("probe_object_absent", "Owned POD CDO was not found uniquely")
        target = check_target(found["items"][0])
        functions = await call("g1r_object_functions", {"handle": target["handle"], "limit": 50})
        for method in METHODS:
            check_signature(functions, method)

        async def invoke(method: str, arguments: dict | None = None):
            result = await call("g1r_invoke", {"handle": target["handle"], "function": method, "arguments": arguments or {}})
            output = METHODS[method][1]
            if (result.get("executed") is not True or "result" not in result or
                    (output is None and result["result"] is not None) or
                    (output is not None and (result.get("return_type") != output[0] or
                                            type(result["result"]) is not (str if output[0] == "NameProperty" else int)))):
                raise ProbeError("probe_call_unconfirmed", "Owned method did not confirm the expected result")
            return result["result"]

        async def read_page(property_name, *, total, offset=0, limit=64, element_type="IntProperty", element_size=4):
            page = await call("g1r_read_array", {"handle": target["handle"], "property_name": property_name,
                             "offset": offset, "limit": limit})
            return check_page(page, target, session_id, property_name, offset=offset, limit=limit,
                              total=total, element_type=element_type, element_size=element_size)

        if await invoke("ReadMarker") != 101:
            raise ProbeError("probe_marker_mismatch", "Owned POD CDO marker does not match")
        initial_counts = [await invoke(method) for method in ("ReadNumberCount", "ReadEmptyCount", "ReadNameCount")]
        if initial_counts != [0, 0, 2]:
            raise ProbeError("probe_initial_state", "Initial arrays differ from defaults; no mutation is performed")
        if await read_page("Numbers", total=0) != [] or await read_page("EmptyNumbers", total=0) != []:
            raise ProbeError("probe_initial_state", "Empty array defaults are not confirmed")
        initial_names = await read_page("Names", total=2, element_type="NameProperty", element_size=8)
        if initial_names != ["Entry", "None"] or [await invoke("ReadNameAt", {"Index": index}) for index in (0, 1)] != initial_names:
            raise ProbeError("probe_initial_state", "FName defaults or independent getter values do not match")
        report["checks"].append("empty_defaults_and_name_constants_verified_before_setup")
        # Cleanup is required even if a previously sent write response is lost.
        cleanup_required = True
        await invoke("SetupFixture")
        if await invoke("ReadNumberCount") != 205:
            raise ProbeError("probe_count_mismatch", "Game array does not contain 205 items after population")
        values = await read_page("Numbers", total=205, limit=200)
        values += await read_page("Numbers", total=205, offset=200, limit=200)
        expected = [index * 3 - 7 for index in range(205)]
        if values != expected:
            raise ProbeError("probe_array_values", "The 205 items do not match the owned CDO formula")
        for index in (0, 199, 200, 204):
            if await invoke("ReadNumberAt", {"Index": index}) != values[index]:
                raise ProbeError("probe_array_values", "Array read does not match the independent getter")
        if await read_page("Numbers", total=205, offset=205, limit=200) != []:
            raise ProbeError("probe_array_boundary", "Page beyond the array end is not empty")
        if await read_page("EmptyNumbers", total=0) != [] or await read_page("Names", total=2, element_type="NameProperty", element_size=8) != initial_names:
            raise ProbeError("probe_unexpected_mutation", "Populating Numbers changed the other arrays")
        report["checks"].append("engine_built_int_array_205_values_two_pages_and_control_getters")
        report["count"] = 205
        report["page_sizes"] = [200, 5]
        report["names"] = initial_names
    except BaseException as error:
        failure = error
        raise
    finally:
        if cleanup_required:
            async def finish_cleanup():
                await invoke("ClearFixture")
                if await invoke("ReadNumberCount") != 0 or await invoke("ReadEmptyCount") != 0:
                    raise ProbeError("probe_cleanup_unconfirmed", "Owned array cleanup is not confirmed")
                if await read_page("Numbers", total=0) != [] or await read_page("EmptyNumbers", total=0) != []:
                    raise ProbeError("probe_cleanup_unconfirmed", "Array pages are not empty after cleanup")
                if await read_page("Names", total=2, element_type="NameProperty", element_size=8) != ["Entry", "None"]:
                    raise ProbeError("probe_cleanup_unconfirmed", "Cleaning Numbers changed Names")
                report["cleanup"].append("ClearFixture_and_confirmed_empty_array_pages")
            try:
                await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="array probe cleanup")
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                failure.add_note("Owned array cleanup is not confirmed: " + str(cleanup_error))
    return report


async def run_probe(args: argparse.Namespace) -> dict:
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT), env={"PYTHONUTF8": "1"})
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            report = await exercise(_caller(client))
            report["server_version"] = initialized.server_info.version
            return report


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            return super().format_help().replace("usage:", "usage:", 1).replace("options:", "options:", 1)
    parser = RussianArgumentParser(description="Check array reads on an owned CDO through a ready MCP connection", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit", help="Official Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT, help="Package workspace")
    parser.add_argument("--native-port", type=int, default=27123, help="Native bridge port")
    args = parser.parse_args(argv)
    try:
        async def protected():
            with deferred_sigint():
                return await run_probe(args)
        report = asyncio.run(protected())
    except BaseException as error:
        cause = _single_error(error)
        if isinstance(cause, (KeyboardInterrupt, SystemExit)):
            raise cause
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause)),
                          "notes": getattr(cause, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
