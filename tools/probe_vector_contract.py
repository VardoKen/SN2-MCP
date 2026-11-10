"""One FVector input/out/return series on an owned loaded CDO."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
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

HOST = "G1RMCPVectorProbe"
MARKER = 161
STORED = {"X": 1.25, "Y": -2.5, "Z": 3.75}
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "GetVector", "EchoVector", "WriteVector")


def _finite(value) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def check_vector(value, expected: dict) -> dict:
    if (not isinstance(value, dict) or set(value) != {"X", "Y", "Z"}
            or any(not _finite(value[axis]) or value[axis] != expected[axis] for axis in expected)):
        raise ProbeError("probe_vector_value", "FVector did not preserve exactly three finite numeric XYZ components")
    return {axis: value[axis] for axis in ("X", "Y", "Z")}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST
            or item.get("class") != HOST or item.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Owned FVector fixture CDO is not confirmed")
    return item


def check_layout(result: dict, target: dict, session_id: str) -> dict:
    if (result.get("source") != "live_native" or result.get("session_id") != session_id
            or result.get("handle") != target["handle"] or result.get("property_name") != "StoredVector"
            or result.get("struct_path") != "/Script/CoreUObject.Vector"
            or result.get("consistency") != "copied_bytes_then_metadata_revalidated"
            or result.get("partial") is not False or result.get("truncated") is not False or "error" in result):
        raise ProbeError("probe_vector_layout_owner", "FVector contract, source, and complete layout are not confirmed")
    size, alignment, components = result.get("properties_size"), result.get("min_alignment"), result.get("components")
    if (type(size) is not int or size not in (12, 24) or type(alignment) is not int
            or alignment not in (1, 2, 4, 8) or alignment > size // 3
            or not isinstance(components, list) or len(components) != 3
            or any(not isinstance(component, dict) for component in components)):
        raise ProbeError("probe_vector_layout", "FVector size 12/24 with three components is not confirmed")
    width = size // 3
    by_name = {component.get("name"): component for component in components}
    if set(by_name) != set(STORED):
        raise ProbeError("probe_vector_layout", "FVector components do not match XYZ")
    offsets = []
    values = {}
    for name, component in by_name.items():
        offset = component.get("offset")
        if (component.get("type") != ("FloatProperty" if width == 4 else "DoubleProperty")
                or type(component.get("element_size")) is not int or component["element_size"] != width
                or type(offset) is not int or offset not in (0, width, width * 2)):
            raise ProbeError("probe_vector_layout", "Exact component sizes, types, or offsets do not match")
        offsets.append(offset)
        values[name] = component.get("value")
    if sorted(offsets) != [0, width, width * 2]:
        raise ProbeError("probe_vector_layout", "Components do not cover FVector exactly once")
    check_vector(values, STORED)
    return {"struct_path": result["struct_path"], "size": size, "component_width": width,
            "component_type": "FloatProperty" if width == 4 else "DoubleProperty", "min_alignment": alignment}


def check_parameter_vector_metadata(field: dict, vector_size: int, direction: str) -> None:
    metadata = field.get("struct")
    width = vector_size // 3
    if (not isinstance(metadata, dict) or metadata.get("struct_path") != "/Script/CoreUObject.Vector"
            or metadata.get("struct_name") != "Vector" or type(metadata.get("properties_size")) is not int
            or metadata["properties_size"] != vector_size or type(metadata.get("min_alignment")) is not int
            or metadata["min_alignment"] not in (1, 2, 4, 8) or metadata["min_alignment"] > width
            or field.get("direction") != direction):
        raise ProbeError("probe_signature_vector_metadata", "Parameter is not confirmed as CoreUObject.Vector with exact layout and direction")
    components = metadata.get("components")
    if (not isinstance(components, list) or len(components) != 3
            or any(not isinstance(component, dict) for component in components)):
        raise ProbeError("probe_signature_vector_metadata", "Three FVector parameter components are not confirmed")
    by_name = {component.get("name"): component for component in components}
    if set(by_name) != {"X", "Y", "Z"}:
        raise ProbeError("probe_signature_vector_metadata", "Parameter components do not match XYZ")
    offsets = []
    for component in components:
        offset = component.get("offset")
        if (component.get("type") != ("FloatProperty" if width == 4 else "DoubleProperty")
                or type(component.get("element_size")) is not int or component["element_size"] != width
                or type(offset) is not int or offset not in (0, width, width * 2)
                or type(component.get("array_dim")) is not int or component["array_dim"] != 1):
            raise ProbeError("probe_signature_vector_metadata", "Component type, width, offset, or ArrayDim is not confirmed")
        offsets.append(offset)
    if sorted(offsets) != [0, width, width * 2]:
        raise ProbeError("probe_signature_vector_metadata", "Components do not cover the FVector parameter bytes")
    if direction == "output" and field.get("argument_marker") != {"out": True}:
        raise ProbeError("probe_signature_vector_metadata", "Required Output out marker is not confirmed")


def check_signature(functions: dict, method_name: str, vector_size: int) -> tuple[dict, str]:
    if (method_name not in METHODS or not isinstance(functions, dict)
            or functions.get("truncated") is not False or functions.get("scan_complete") is not True
            or functions.get("scan_truncated") is not False or not isinstance(functions.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete owned FVector CDO catalog is not confirmed")
    methods = [item for item in functions["items"] if isinstance(item, dict) and item.get("name") == method_name]
    if len(methods) != 1:
        raise ProbeError("probe_signature_absent", "Probe method was not found uniquely")
    method = methods[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 64
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "Field count and bounded FVector method buffer are not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    arguments = [field for field in fields if field.get("return") is False]
    expected_return = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                       "GetVector": ("StructProperty", vector_size), "EchoVector": ("StructProperty", vector_size),
                       "WriteVector": ("BoolProperty", 1)}.get(method_name)
    if (len(returned) != (expected_return is not None) or len(arguments) != (method_name in {"EchoVector", "WriteVector"})
            or len(fields) != len(returned) + len(arguments)):
        raise ProbeError("probe_signature_mismatch", "FVector method input, out, and return do not match")
    if expected_return is not None and (returned[0].get("type"), returned[0].get("element_size")) != expected_return:
        raise ProbeError("probe_signature_mismatch", "Exact return type or size does not match")
    if arguments:
        argument = arguments[0]
        if ((argument.get("name"), argument.get("type"), argument.get("element_size")) !=
                ("Input" if method_name == "EchoVector" else "Output", "StructProperty", vector_size)):
            raise ProbeError("probe_signature_mismatch", "Exact Input/Output FVector does not match")
        if method_name == "EchoVector" and (argument.get("out") is not False or argument.get("reference") is not False):
            raise ProbeError("probe_signature_mismatch", "EchoVector must take FVector by value")
        if method_name == "WriteVector" and argument.get("out") is not True:
            raise ProbeError("probe_signature_mismatch", "WriteVector must have a confirmed Output out parameter")
    spans = []
    for field in fields:
        offset, element_size = field.get("offset"), field.get("element_size")
        if (type(offset) is not int or type(element_size) is not int or offset < 0 or element_size < 1
                or offset + element_size > 64 or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or type(field.get("out")) is not bool or type(field.get("reference")) is not bool
                or field["return"] is True and field["reference"] is not False
                or field["return"] is False and offset + element_size > size):
            raise ProbeError("probe_signature_mismatch", "ArrayDim, out/ref flags, or FVector buffer bounds are not confirmed")
        if any(offset < previous_end and previous_start < offset + element_size for previous_start, previous_end in spans):
            raise ProbeError("probe_signature_mismatch", "FVector method parameter ranges overlap")
        spans.append((offset, offset + element_size))
        if field["type"] == "StructProperty":
            direction = "return" if field["return"] is True else "output" if method_name == "WriteVector" else "input"
            check_parameter_vector_metadata(field, vector_size, direction)
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "Owned FVector method did not receive a valid token")
    return method, token


def check_scalar(result: dict, return_type: str, expected) -> None:
    if (result.get("executed") is not True or result.get("return_type") != return_type or "result" not in result
            or type(result["result"]) is not type(expected) or result["result"] != expected):
        raise ProbeError("probe_vector_scalar_result", "Marker, bool, or call counter is not confirmed")


def check_rejection(result: dict, method: str, value) -> None:
    effect = result.get("effect")
    nested = effect if isinstance(effect, dict) else {}
    if (result.get("code") != "invalid_argument" or result.get("may_have_executed") is not False
            or effect is not None and not isinstance(effect, dict)
            or not (result.get("executed") is False or nested.get("executed") is False)
            or "executed" in result and result["executed"] is not False
            or "executed" in nested and nested["executed"] is not False
            or "may_have_executed" in nested and nested["may_have_executed"] is not False):
        diagnostic = json.dumps({"method": method, "argument": value, "payload": result}, ensure_ascii=False)
        raise ProbeError("probe_vector_rejection_unconfirmed", "FVector rejection before execution is not confirmed: " + diagnostic)


async def exercise(call) -> dict:
    cleanup_required = False
    failure = None
    report = {"checks": [], "echoes": [], "rejections": [], "cleanup": [],
              "game_launched": False, "dll_loaded": False, "inout": "not_tested"}
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and current native session are not confirmed")
        found = await call("sn2_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                                                "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
            raise ProbeError("probe_object_absent", "Owned FVector CDO was not found uniquely")
        target = check_target(found["items"][0])
        handle = target["handle"]
        layout = check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
        tokens = {}
        for method in METHODS:
            _, tokens[method] = check_signature(await call("sn2_object_functions", {"handle": handle, "function_query": method, "limit": 10}), method, layout["size"])
        if len(set(tokens.values())) != len(tokens):
            raise ProbeError("probe_signature_token_alias", "Different FVector methods received the same token")

        async def invoke(method, arguments=None, *, expected_error=None):
            arguments = arguments or {}
            # JSON NaN/Infinity must not be sent to transport and reported as a native rejection.
            json.dumps(arguments, allow_nan=False)
            return await call("sn2_invoke", {"handle": handle, "function": method, "arguments": arguments,
                              "expected_signature_token": tokens[method]}, expected_error=expected_error)

        count = 0
        check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        get = await invoke("GetVector")
        if get.get("executed") is not True or get.get("return_type") != "StructProperty":
            raise ProbeError("probe_vector_return", "GetVector did not confirm a StructProperty return")
        check_vector(get.get("result"), STORED)
        check_parameter_vector_metadata({"struct": get.get("result_metadata"), "direction": "return"}, layout["size"], "return")
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens, layout=layout)
        report["checks"].append("own_marker161_zero_counter_reflected_vector_layout_and_return")
        vectors = [{"X": 0, "Y": 0, "Z": 0}, {"X": -1.25, "Y": 2.5, "Z": -3.75}]
        if layout["component_width"] == 8:
            vectors.append({"X": 2**53 + 2, "Y": -(2**40) - 0.5, "Z": 0.125})
        for value in vectors:
            cleanup_required = True
            result = await invoke("EchoVector", {"Input": value})
            if result.get("executed") is not True or result.get("return_type") != "StructProperty":
                raise ProbeError("probe_vector_return", "EchoVector did not confirm a StructProperty return")
            check_vector(result.get("result"), value)
            check_parameter_vector_metadata({"struct": result.get("result_metadata"), "direction": "return"}, layout["size"], "return")
            count += 1
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["echoes"].append(value)
        report["checks"].append("input_by_value_return_fractional_negative_zero_and_layout_aware_large_double")
        cleanup_required = True
        written = await invoke("WriteVector", {"Output": {"out": True}})
        check_scalar(written, "BoolProperty", True)
        out = written.get("out_parameters")
        if not isinstance(out, dict) or set(out) != {"Output"}:
            raise ProbeError("probe_vector_out", "WriteVector did not return exactly one Output")
        check_vector(out["Output"], STORED)
        output_metadata = written.get("out_parameter_metadata")
        if not isinstance(output_metadata, dict) or set(output_metadata) != {"Output"}:
            raise ProbeError("probe_vector_out", "WriteVector did not return metadata for one Output")
        check_parameter_vector_metadata({"struct": output_metadata["Output"], "direction": "output", "argument_marker": {"out": True}}, layout["size"], "output")
        count += 1
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["out_parameters"] = out
        report["checks"].append("out_marker_true_bool_return_and_finite_output")
        invalid_inputs = [None, True, "FVector", [], {"X": 1, "Y": 2}, {"X": 1, "Y": 2, "Z": 3, "W": 4},
                          {"X": True, "Y": 2, "Z": 3}, {"X": "1", "Y": 2, "Z": 3}]
        invalid_out = [None, True, STORED, {"out": False}, {"out": 1}, {"out": True, "value": STORED}, {}]
        for method, argument_name, values in (("EchoVector", "Input", invalid_inputs), ("WriteVector", "Output", invalid_out)):
            for value in values:
                refusal = await invoke(method, {argument_name: value}, expected_error="invalid_argument")
                check_rejection(refusal, method, value)
                check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
                report["rejections"].append({"method": method, "code": "invalid_argument"})
        local_nonfinite = 0
        for value in (float("nan"), float("inf"), -float("inf")):
            try:
                await invoke("EchoVector", {"Input": {"X": value, "Y": 2, "Z": 3}})
            except ValueError:
                local_nonfinite += 1
            else:
                raise ProbeError("probe_nonfinite_transport", "Nonfinite JSON was sent to MCP instead of being rejected locally")
        report["local_nonfinite_json_rejections"] = local_nonfinite
        report["checks"].append("malformed_vector_and_out_marker_refused_without_counter_change")
        check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in {"game_timeout", "game_call_fault", "native_timeout",
                                                  "native_disconnected", "session_closed", "native_unavailable"}:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("FVector cleanup skipped after an uncertain call or unavailable connection; the development session owner stops the game")
                return_cleanup = False
            else:
                return_cleanup = True
            async def finish_cleanup():
                check_scalar(await invoke("ResetProbe"), "", None)
                check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
                report["cleanup"].append("ResetProbe_zero_counter_marker161_unchanged_stored_vector")
            try:
                if return_cleanup:
                    await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="FVector contract cleanup")
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note("Owned FVector CDO cleanup is not confirmed: " + str(error))


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
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dev.16 FVector input/out/return on an owned loaded fixture")
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
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
