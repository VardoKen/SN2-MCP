"""Check numeric enum calls only on the owned CDO through a ready MCP."""
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

HOST = "G1RMCPEnumProbe"
ENUM_NAME = "EG1RMCPEnumProbeMode"
ENUM_PATH = "/Script/Angelscript." + ENUM_NAME
VALUES = {0: "First", 1: "Second", 7: "Third"}
PROPERTIES = ["StoredMode", "CallsCount"]
METHODS = {"ReadMarker", "ReadMode", "EchoMode", "StoreMode", "ResetProbe"}
INTEGER_SIZES = {"ByteProperty": 1, "IntProperty": 4, "UInt32Property": 4,
                 "Int64Property": 8, "UInt64Property": 8}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST or item.get("class") != HOST or
            item.get("path") != "/Script/Angelscript.Default__" + HOST or
            not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "The owned enum probe CDO was not confirmed")
    return item


def check_signature(functions: dict, method_name: str) -> dict:
    if method_name not in METHODS or not isinstance(functions, dict):
        raise ProbeError("probe_signature_invalid", "Unknown enum probe method")
    matches = [item for item in functions.get("items", []) if isinstance(item, dict) and item.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "The enum probe method was not uniquely found")
    method = matches[0]
    params = method.get("parameters")
    size = method.get("parameter_size")
    if not isinstance(params, list) or type(size) is not int or not 0 <= size <= 32:
        raise ProbeError("probe_signature_mismatch", "The bounded probe method layout was not confirmed")
    expected_inputs = 1 if method_name in {"EchoMode", "StoreMode"} else 0
    expected_returns = 1 if method_name in {"ReadMarker", "ReadMode", "EchoMode"} else 0
    if len(params) != expected_inputs + expected_returns or not all(isinstance(param, dict) for param in params):
        raise ProbeError("probe_signature_mismatch", "Invalid number of input or return parameters")
    inputs = [param for param in params if param.get("return") is False]
    returns = [param for param in params if param.get("return") is True]
    if len(inputs) != expected_inputs or len(returns) != expected_returns:
        raise ProbeError("probe_signature_mismatch", "Parameter roles were not confirmed")
    for param in params:
        offset, element_size, array_dim = param.get("offset"), param.get("element_size"), param.get("array_dim")
        if (type(offset) is not int or type(element_size) is not int or offset < 0 or element_size < 1 or
                offset + element_size > 32 or type(array_dim) is not int or array_dim != 1 or param.get("reference") is not False or
                param.get("return") is False and (param.get("out") is not False or offset + element_size > size)):
            raise ProbeError("probe_signature_mismatch", "The parameter has an unsupported layout or out/ref")
        if method_name == "ReadMarker":
            if param.get("type") != "IntProperty" or element_size != 4 or param.get("name") != "ReturnValue":
                raise ProbeError("probe_signature_mismatch", "The marker must return int32")
        else:
            if param.get("type") not in {"EnumProperty", "ByteProperty"} or element_size not in {1, 4, 8}:
                raise ProbeError("probe_signature_mismatch", "The reflected numeric enum was not confirmed")
            if param.get("type") == "ByteProperty" and element_size != 1:
                raise ProbeError("probe_signature_mismatch", "ByteProperty must occupy one byte")
            if param.get("name") != ("ReturnValue" if param["return"] else "Input"):
                raise ProbeError("probe_signature_mismatch", "The reflected enum parameter name did not match")
    if inputs and returns and (inputs[0]["type"], inputs[0]["element_size"]) != (returns[0]["type"], returns[0]["element_size"]):
        raise ProbeError("probe_signature_mismatch", "EchoMode input and return use different layouts")
    return method


def check_enum(metadata: dict, value: int) -> str:
    keys = {"path", "underlying_type", "name_status", "value_name", "matched_count", "ambiguous", "reason"}
    if (type(value) is not int or value not in VALUES or not isinstance(metadata, dict) or set(metadata) != keys or
            metadata["path"] != ENUM_PATH or metadata["underlying_type"] not in INTEGER_SIZES or
            metadata["name_status"] != "matched" or metadata["value_name"] != ENUM_NAME + "::" + VALUES[value] or
            type(metadata["matched_count"]) is not int or metadata["matched_count"] != 1 or
            metadata["ambiguous"] is not False or metadata["reason"] is not None):
        raise ProbeError("probe_enum_metadata", "The declared numeric value of the owned enum was not confirmed")
    return metadata["underlying_type"]


def snapshot_values(snapshot: dict, target: dict, session_id: str) -> tuple[int, int, str]:
    objects = snapshot.get("objects")
    if (snapshot.get("source") != "live_native" or snapshot.get("consistency") != "single_tick_sequential" or
            snapshot.get("session_id") != session_id or snapshot.get("partial") is not False or snapshot.get("truncated") is not False or
            not isinstance(objects, list) or len(objects) != 1 or not isinstance(objects[0], dict)):
        raise ProbeError("probe_snapshot_invalid", "The owned enum snapshot was not fully confirmed in the current session")
    obj = objects[0]
    if (any(obj.get(key) != target[key] for key in ("handle", "path", "class")) or obj.get("partial") is not False or
            obj.get("truncated") is not False or "error" in obj or not isinstance(obj.get("properties"), list)):
        raise ProbeError("probe_snapshot_invalid", "Enum CDO identity or field completeness was not confirmed")
    fields = obj["properties"]
    if not all(isinstance(field, dict) and isinstance(field.get("name"), str) for field in fields):
        raise ProbeError("probe_snapshot_invalid", "Property names were not confirmed")
    by_name = {field["name"]: field for field in fields}
    if set(by_name) != set(PROPERTIES) or len(by_name) != len(fields):
        raise ProbeError("probe_snapshot_invalid", "Properties are missing or duplicated")
    mode, count = by_name["StoredMode"], by_name["CallsCount"]
    if (mode.get("type") not in {"EnumProperty", "ByteProperty"} or mode.get("supported") is not True or "error" in mode or
            type(mode.get("value")) is not int or count.get("type") != "IntProperty" or count.get("supported") is not True or
            "error" in count or type(count.get("value")) is not int or count["value"] < 0):
        raise ProbeError("probe_snapshot_invalid", "Numeric values of probe fields were not confirmed")
    underlying = check_enum(mode.get("enum"), mode["value"])
    if mode["type"] == "ByteProperty" and underlying != "ByteProperty":
        raise ProbeError("probe_snapshot_invalid", "The ByteProperty enum has an incompatible underlying type")
    return mode["value"], count["value"], underlying


def check_enum_result(result: dict, expected: int, method: dict, underlying: str) -> None:
    params = [param for param in method["parameters"] if param["return"]]
    if (result.get("executed") is not True or type(result.get("result")) is not int or result["result"] != expected or
            result.get("return_type") != params[0]["type"]):
        raise ProbeError("probe_enum_result", "The enum call did not confirm the expected numeric value and type")
    actual = check_enum(result.get("return_enum"), expected)
    if actual != underlying or params[0]["element_size"] != INTEGER_SIZES[underlying]:
        raise ProbeError("probe_enum_result", "The enum return does not match the confirmed underlying type")


async def exercise(call) -> dict:
    report = {"checks": [], "cleanup": [], "game_launched": False, "dll_loaded": False}
    target = None
    session_id = None
    cleanup_allowed = False
    failure = None
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Ready Tick and the current session were not confirmed")
        found = await call("sn2_find_objects", {"query": "", "class_query": HOST, "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        items = found.get("items")
        if not isinstance(items, list) or len(items) != 1 or found.get("truncated") is not False:
            raise ProbeError("probe_object_absent", "The owned enum CDO was not uniquely found")
        target = check_target(items[0])
        functions = await call("sn2_object_functions", {"handle": target["handle"], "limit": 20})
        signatures = {method: check_signature(functions, method) for method in METHODS}

        async def invoke(method: str, value=...):
            args = {} if value is ... else {"Input": value}
            result = await call("sn2_invoke", {"handle": target["handle"], "function": method, "arguments": args})
            if result.get("executed") is not True:
                raise ProbeError("probe_not_executed", "The owned probe method did not confirm execution")
            return result

        async def snapshot():
            result = await call("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": PROPERTIES}]})
            return result, snapshot_values(result, target, session_id)

        marker = await invoke("ReadMarker")
        if type(marker.get("result")) is not int or marker["result"] != 81:
            raise ProbeError("probe_marker_mismatch", "The owned enum CDO marker was not confirmed")
        _, initial = await snapshot()
        if initial[:2] != (0, 0):
            raise ProbeError("probe_initial_state", "Original StoredMode/CallsCount did not match the declared defaults")
        cleanup_allowed = True
        underlying = initial[2]
        for method, signature in signatures.items():
            if method in {"ReadMode", "EchoMode", "StoreMode"} and any(param["element_size"] != INTEGER_SIZES[underlying] for param in signature["parameters"]):
                raise ProbeError("probe_signature_mismatch", "Enum inputs and returns do not match the property underlying type")
        check_enum_result(await invoke("ReadMode"), 0, signatures["ReadMode"], underlying)
        for value in VALUES:
            before, old = await snapshot()
            check_enum_result(await invoke("EchoMode", value), value, signatures["EchoMode"], underlying)
            after, new = await snapshot()
            if new[:2] != (old[0], old[1] + 1):
                raise ProbeError("probe_call_count", "EchoMode did not execute exactly once or changed StoredMode")
            await invoke("StoreMode", value)
            stored_snapshot, stored = await snapshot()
            if stored[:2] != (value, new[1] + 1):
                raise ProbeError("probe_store_value", "StoreMode did not confirm one execution and the selected value")
            check_enum_result(await invoke("ReadMode"), value, signatures["ReadMode"], underlying)
            comparison = await call("sn2_compare_snapshots", {"before": before, "after": stored_snapshot})
            if (comparison.get("comparable") is not True or type(comparison.get("unknown_count")) is not int or
                    comparison["unknown_count"] != 0 or comparison.get("coverage", {}).get("complete") is not True or
                    comparison.get("truncated") is not False):
                raise ProbeError("probe_compare_invalid", "Comparison of owned enum snapshots did not confirm a complete result")
        report["checks"].append("declared_values_zero_one_seven_echo_store_return")
        rejections = []
        negative_code = "invalid_argument" if underlying in {"ByteProperty", "UInt32Property", "UInt64Property"} else "unknown_enum_value"
        outside = {"ByteProperty": 256, "IntProperty": 2**31, "UInt32Property": 2**32,
                   "Int64Property": 2**63, "UInt64Property": 2**64}[underlying]
        for value, code in [(2, "unknown_enum_value"), (-1, negative_code), (outside, "invalid_argument"), (True, "invalid_argument"),
                            (1.0, "invalid_argument"), (None, "invalid_argument"), ("First", "invalid_argument"),
                            ({"value": 1}, "invalid_argument")]:
            _, before = await snapshot()
            result = await call("sn2_invoke", {"handle": target["handle"], "function": "EchoMode", "arguments": {"Input": value}}, expected_error=code)
            _, after = await snapshot()
            if before != after or result.get("may_have_executed") is not False:
                raise ProbeError("probe_rejection_executed", "The rejected enum argument changed CallsCount or StoredMode")
            rejections.append(code)
        _, before = await snapshot()
        await call("sn2_invoke", {"handle": target["handle"], "function": "EchoMode",
                                  "arguments": {"Input": 1, "Unexpected": 7}}, expected_error="unknown_argument")
        _, after = await snapshot()
        if before != after:
            raise ProbeError("probe_rejection_executed", "The extra argument changed probe fields")
        rejections.append("unknown_argument")
        report["checks"].append("invalid_args_rejected_before_execution_calls_count_unchanged")
        report["underlying_type"] = underlying
        report["rejections"] = rejections
        report["declared_values"] = list(VALUES)
    except BaseException as error:
        failure = error
        raise
    finally:
        async def finish_cleanup():
            if not cleanup_allowed:
                return
            result = await call("sn2_invoke", {"handle": target["handle"], "function": "ResetProbe", "arguments": {}})
            if result.get("executed") is not True:
                raise ProbeError("probe_cleanup_unconfirmed", "ResetProbe execution was not confirmed")
            cleaned = await call("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": PROPERTIES}]})
            if snapshot_values(cleaned, target, session_id)[:2] != (0, 0):
                raise ProbeError("probe_cleanup_unconfirmed", "StoredMode/CallsCount did not return to their original values")
            report["cleanup"].append("ResetProbe_and_confirmed_zero_snapshot")
        try:
            await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="enum cleanup")
        except BaseException as cleanup_error:
            if failure is None:
                raise
            failure.add_note("Owned enum CDO cleanup was not confirmed: " + str(cleanup_error))
    return report


async def run_probe(args) -> dict:
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
        "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT), env={"PYTHONUTF8": "1"})
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            with deferred_sigint():
                report = await exercise(_caller(client))
            report["server_version"] = initialized.server_info.version
            return report


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "usage:", 1).replace("options:", "options:", 1)
    parser = RussianArgumentParser(description="Check enum calls of the owned fixture in an already ready game", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_probe(args))
    except BaseException as error:
        error = _single_error(error)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise error
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
