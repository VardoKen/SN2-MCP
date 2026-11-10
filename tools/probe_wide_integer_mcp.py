"""Check wide integers only on an owned loaded CDO through MCP."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_function_contract_mcp import TOKEN, make_caller
from probe_object_mcp import ProbeError, _single_error

HOST = "G1RMCPWideIntegerProbe"
MARKER = 151
METHODS = {
    "ReadMarker": ([], ("IntProperty", 4)),
    "ReadCallsCount": ([], ("IntProperty", 4)),
    "ResetProbe": ([], None),
    "EchoInt64": ([("Input", "Int64Property", 8)], ("Int64Property", 8)),
    "EchoUInt64": ([("Input", "UInt64Property", 8)], ("UInt64Property", 8)),
    "EchoUInt32": ([("Input", "UInt32Property", 4)], ("UInt32Property", 4)),
}
SUCCESS_CASES = {
    "EchoInt64": [-(2**63), -(2**53 + 1), 0, 2**53 + 1, 2**63 - 1],
    "EchoUInt64": [0, 2**53 + 1, 2**64 - 1],
    "EchoUInt32": [0, 2**31, 2**32 - 1],
}
INVALID_CASES = {
    "EchoInt64": [-(2**63) - 1, 2**63, True, 1.0, "9223372036854775807", None],
    "EchoUInt64": [-1, 2**64, True, 1.0, "18446744073709551615", None],
    "EchoUInt32": [-1, 2**32, True, 1.0, "4294967295", None],
}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST
            or item.get("class") != HOST or item.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Owned wide-integer CDO is not confirmed")
    return item


def check_signature(functions: dict, method_name: str) -> tuple[dict, str]:
    if (method_name not in METHODS or not isinstance(functions, dict)
            or functions.get("truncated") is not False or functions.get("scan_complete") is not True
            or functions.get("scan_truncated") is not False or not isinstance(functions.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete probe method catalog is not confirmed")
    matches = [item for item in functions["items"] if isinstance(item, dict) and item.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Owned method was not found uniquely")
    method = matches[0]
    parameters, size = method.get("parameters"), method.get("parameter_size")
    inputs, returned = METHODS[method_name]
    if (not isinstance(parameters, list) or any(not isinstance(item, dict) for item in parameters)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 32):
        raise ProbeError("probe_signature_mismatch", "Complete bounded method buffer is not confirmed")
    actual_inputs = [item for item in parameters if item.get("return") is False]
    actual_returns = [item for item in parameters if item.get("return") is True]
    if (len(actual_inputs) != len(inputs) or len(actual_returns) != (returned is not None)
            or len(parameters) != len(actual_inputs) + len(actual_returns)
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(parameters)):
        raise ProbeError("probe_signature_mismatch", "Input and return counts do not match")
    for parameter, expected in zip(actual_inputs, inputs):
        if (parameter.get("name"), parameter.get("type"), parameter.get("element_size")) != expected:
            raise ProbeError("probe_signature_mismatch", "Exact integer input name, type, or size does not match")
    if returned is not None and (actual_returns[0].get("type"), actual_returns[0].get("element_size")) != returned:
        raise ProbeError("probe_signature_mismatch", "Exact integer return type or size does not match")
    for parameter in parameters:
        offset, element_size = parameter.get("offset"), parameter.get("element_size")
        if (type(offset) is not int or type(element_size) is not int or offset < 0 or element_size < 1
                or offset + element_size > 32 or type(parameter.get("array_dim")) is not int
                or parameter["array_dim"] != 1 or parameter.get("reference") is not False
                or type(parameter.get("out")) is not bool
                or parameter["return"] is False and (parameter["out"] is not False or offset + element_size > size)):
            raise ProbeError("probe_signature_mismatch", "ArrayDim=1, out/ref flags, or buffer bounds are not confirmed")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "Owned method did not receive a complete signature token")
    return method, token


def check_result(result: dict, method_name: str, expected: int | None) -> None:
    output = METHODS[method_name][1]
    expected_type = output[0] if output is not None else ""
    if (result.get("executed") is not True or result.get("return_type") != expected_type
            or "result" not in result or (expected is None and result["result"] is not None)
            or (expected is not None and (type(result.get("result")) is not int or result["result"] != expected))):
        raise ProbeError("probe_integer_result", "Integer value or return type changed during the MCP call")


def check_integer_rejection(result: dict, method_name: str, value) -> None:
    effect = result.get("effect")
    effect_valid = effect is None or isinstance(effect, dict)
    nested = effect if isinstance(effect, dict) else {}
    explicit_not_executed = result.get("executed") is False or nested.get("executed") is False
    conflicting_execution = ("executed" in result and result["executed"] is not False
                             or "executed" in nested and nested["executed"] is not False)
    if (result.get("code") != "invalid_argument" or result.get("may_have_executed") is not False
            or not effect_valid or not explicit_not_executed or conflicting_execution
            or "may_have_executed" in nested and nested["may_have_executed"] is not False):
        diagnostic = json.dumps({"method": method_name, "input": value, "payload": result}, ensure_ascii=False)
        raise ProbeError("probe_integer_rejection_unconfirmed",
                         "Invalid integer rejection before execution is not confirmed: " + diagnostic)


async def exercise(call) -> dict:
    """JSON integer bounds, invalid-type rejection, and one protected Reset on the owned CDO."""
    cleanup_required = False
    failure = None
    report = {"checks": [], "successes": {}, "rejections": {}, "cleanup": [],
              "game_launched": False, "dll_loaded": False}
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Ready game thread in the current native session is not confirmed")
        found = await call("sn2_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                                                "path_query": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (not isinstance(found.get("items"), list) or len(found["items"]) != 1
                or found.get("truncated") is not False):
            raise ProbeError("probe_object_absent", "Owned CDO was not found uniquely and completely")
        target = check_target(found["items"][0])
        handle = target["handle"]
        tokens = {}
        for method_name in METHODS:
            functions = await call("sn2_object_functions", {"handle": handle, "function_query": method_name, "limit": 10})
            _, tokens[method_name] = check_signature(functions, method_name)
        if len(set(tokens.values())) != len(tokens):
            raise ProbeError("probe_signature_token_alias", "Different owned methods received the same token")

        async def invoke(method_name, value=None, *, expected_error=None):
            return await call("sn2_invoke", {"handle": handle, "function": method_name,
                              "arguments": {"Input": value} if METHODS[method_name][0] else {},
                              "expected_signature_token": tokens[method_name]}, expected_error=expected_error)

        count = 0
        check_result(await invoke("ReadMarker"), "ReadMarker", MARKER)
        check_result(await invoke("ReadCallsCount"), "ReadCallsCount", count)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        report["checks"].append("own_marker151_complete_signature_tokens_zero_counter")
        for method_name, values in SUCCESS_CASES.items():
            for value in values:
                cleanup_required = True
                check_result(await invoke(method_name, value), method_name, value)
                count += 1
                check_result(await invoke("ReadCallsCount"), "ReadCallsCount", count)
            report["successes"][method_name] = list(values)
        report["checks"].append("signed_unsigned_boundaries_and_values_above_2pow53_preserved_exactly")
        for method_name, values in INVALID_CASES.items():
            refusals = []
            for value in values:
                result = await invoke(method_name, value, expected_error="invalid_argument")
                check_integer_rejection(result, method_name, value)
                check_result(await invoke("ReadCallsCount"), "ReadCallsCount", count)
                refusals.append("invalid_argument")
            report["rejections"][method_name] = refusals
        report["checks"].append("overflow_negative_unsigned_bool_float_string_null_refused_without_counter_change")
        report["completed_calls"] = count
        return report
    except BaseException as error:
        failure = error
        raise
    finally:
        if cleanup_required:
            async def finish_cleanup():
                check_result(await invoke("ResetProbe"), "ResetProbe", None)
                check_result(await invoke("ReadCallsCount"), "ReadCallsCount", 0)
                check_result(await invoke("ReadMarker"), "ReadMarker", MARKER)
                report["cleanup"].append("ResetProbe_and_confirmed_zero_counter_marker151")
            try:
                await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="wide integer cleanup")
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note("Wide-integer cleanup is not confirmed: " + str(error))


async def run(args) -> dict:
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "Owned CDO probe uses only local MCP")
        async with streamable_http_client(args.mcp_url) as connection:
            async with ClientSession(connection[0], connection[1]) as session:
                initialized = await session.initialize()
                return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dev.15 integer calls on an owned loaded fixture")
    parser.add_argument("--mcp-url")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    async def protected():
        with deferred_sigint():
            return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2))
        return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
