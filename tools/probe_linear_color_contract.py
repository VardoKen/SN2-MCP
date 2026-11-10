"""FLinearColor value input/return on the owned AS CDO and a safe native math series."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_function_contract_mcp import TOKEN, make_caller
from probe_object_mcp import ProbeError, _single_error
from probe_scalar_out_contract import UNKNOWN_GAME, check_rejection

HOST = "G1RMCPLinearColorProbe"
MARKER = 231
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "ReadColorR", "EchoColor", "MakeStoredColor")
STORED = {"R": 0.25, "G": -0.5, "B": 2.0, "A": 0.125}
NATIVE_CLASS = "KismetMathLibrary"
NATIVE_PATH = "/Script/Engine.Default__KismetMathLibrary"
NATIVE_METHODS = ("MakeColor", "LinearColor_ToNewOpacity")


def check_metadata(metadata):
    if (not isinstance(metadata, dict) or metadata.get("struct_path") != "/Script/CoreUObject.LinearColor"
            or metadata.get("struct_name") != "LinearColor" or type(metadata.get("properties_size")) is not int
            or metadata["properties_size"] != 16 or type(metadata.get("min_alignment")) is not int
            or metadata["min_alignment"] != 4 or not isinstance(metadata.get("components"), list)
            or len(metadata["components"]) != 4 or any(not isinstance(component, dict)
                or not isinstance(component.get("name"), str) for component in metadata["components"])):
        raise ProbeError("probe_linear_color_metadata", "CoreUObject.LinearColor size/alignment 16/4 was not confirmed")
    components = {component["name"]: component for component in metadata["components"]}
    if set(components) != {"R", "G", "B", "A"}:
        raise ProbeError("probe_linear_color_metadata", "Color components did not match RGBA")
    for name, expected_offset in zip(("R", "G", "B", "A"), (0, 4, 8, 12)):
        component = components[name]
        if (component.get("type") != "FloatProperty" or type(component.get("element_size")) is not int
                or component["element_size"] != 4 or type(component.get("offset")) is not int or component["offset"] != expected_offset
                or type(component.get("array_dim")) is not int or component["array_dim"] != 1):
            raise ProbeError("probe_linear_color_metadata", "RGBA did not confirm Float4 and exact offsets 0/4/8/12")
    return metadata


def check_color(value, expected):
    if (not isinstance(value, dict) or set(value) != set(STORED)
            or any(type(value[name]) not in (int, float) or not math.isfinite(value[name]) or value[name] != expected[name] for name in STORED)):
        raise ProbeError("probe_linear_color_value", "RGBA did not preserve exact finite values without clamping")
    return value


def check_signature(listing, name, *, native=False):
    if (name not in (NATIVE_METHODS if native else METHODS) or not isinstance(listing, dict)
            or listing.get("truncated") is not False or listing.get("scan_complete") is not True
            or listing.get("scan_truncated") is not False or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "The full LinearColor method catalog was not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1: raise ProbeError("probe_signature_absent", "The LinearColor method was not uniquely found")
    method, fields = matches[0], matches[0].get("parameters")
    size = method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 64
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "NumParms and the bounded LinearColor buffer were not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    inputs = [field for field in fields if field.get("return") is False]
    count = (4 if name == "MakeColor" else 2) if native else (1 if name in ("ReadColorR", "EchoColor") else 0)
    expected = (("StructProperty", 16) if native else {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                "EchoColor": ("StructProperty", 16), "MakeStoredColor": ("StructProperty", 16)}.get(name))
    if name == "ReadColorR" and len(returned) == 1:
        expected = (returned[0].get("type"), returned[0].get("element_size"))
        if expected not in (("FloatProperty", 4), ("DoubleProperty", 8)):
            raise ProbeError("probe_signature_mismatch", "ReadColorR did not confirm a Float4 or Double8 return")
    if (len(returned) != (expected is not None) or len(inputs) != count or len(fields) != len(returned) + len(inputs)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != expected):
        raise ProbeError("probe_signature_mismatch", "Exact LinearColor method inputs and return did not match")
    spans, color_inputs, scalar_inputs = [], [], []
    for field in fields:
        flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
        if (not isinstance(field.get("name"), str) or not field["name"] or type(flags) is not int or not 0 <= flags < 2**64
                or not flags & 0x80 or bool(flags & 0x400) is not field["return"] or bool(flags & 0x100) is not field.get("out")
                or field.get("const") is not False or field.get("reference") is not False or flags & (0x2 | 0x08000000)
                or type(offset) is not int or offset < 0 or type(width) is not int or width < 1 or offset + width > 64
                or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or field["return"] is False and (offset + width > size or flags & 0x100)):
            raise ProbeError("probe_signature_flags", "CPF/ArrayDim/bounds of the LinearColor value contract were not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "LinearColor parameters overlap")
        spans.append((offset, offset + width))
        if field.get("type") == "StructProperty":
            if width != 16 or offset % 4 or field.get("direction") != ("return" if field["return"] else "input"):
                raise ProbeError("probe_linear_color_metadata", "LinearColor did not confirm width16/align4/direction")
            check_metadata(field.get("struct"))
            if field["return"] is False:
                if field.get("passing") != "value" or "argument_marker" in field:
                    raise ProbeError("probe_linear_color_policy", "LinearColor input is supported only by value")
                color_inputs.append(field)
        elif field["return"] is False:
            if (field.get("type"), width) not in (("FloatProperty", 4), ("DoubleProperty", 8)):
                raise ProbeError("probe_signature_mismatch", "Native color scalar input did not confirm Float/Double")
            scalar_inputs.append(field)
    if (not native and inputs and (len(color_inputs) != 1 or color_inputs[0]["name"] != "Input")
            or native and name == "MakeColor" and (color_inputs or len(scalar_inputs) != 4)
            or native and name == "LinearColor_ToNewOpacity" and (len(color_inputs) != 1 or len(scalar_inputs) != 1)
            or len({field["name"] for field in inputs}) != len(inputs)):
        raise ProbeError("probe_signature_mismatch", "LinearColor inputs are not uniquely determined")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "The LinearColor method did not receive a signature token")
    return method, token


async def exercise(call):
    cleanup_required, failure = False, None
    report = {"checks": [], "calls": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "native_math": {}, "game_launched": False, "dll_loaded": False, "const_ref": "not_tested", "color_out": "not_tested"}
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and the native session were not confirmed")

        async def find(native=False):
            host = NATIVE_CLASS if native else HOST
            path = NATIVE_PATH if native else "/Script/Angelscript.Default__" + HOST
            result = await call("sn2_find_objects", {"query": "Default__" + host, "class_query": host, "exact_path": path, "limit": 2})
            if result.get("truncated") is not False or not isinstance(result.get("items"), list) or len(result["items"]) != 1:
                raise ProbeError("probe_object_absent", "The exact LinearColor/math CDO was not uniquely found")
            item = result["items"][0]
            if (not isinstance(item, dict) or item.get("name") != "Default__" + host or item.get("class") != host or item.get("path") != path
                    or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
                raise ProbeError("unsafe_probe_target", "The exact LinearColor/math CDO was not confirmed")
            return item

        target = await find()
        handle, methods, tokens = target["handle"], {}, {}
        for name in METHODS:
            listing = await call("sn2_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            methods[name], tokens[name] = check_signature(listing, name)
        if len(set(tokens.values())) != len(tokens): raise ProbeError("probe_signature_token_alias", "Different LinearColor methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            supplied = {} if arguments is None else arguments
            json.dumps(supplied, allow_nan=False)
            return await call("sn2_invoke", {"handle": handle, "function": name, "arguments": supplied,
                              "expected_signature_token": tokens[name]}, expected_error=expected_error)

        def scalar(result, kind, value):
            actual = result.get("result")
            if (result.get("executed") is not True or result.get("return_type") != kind
                    or type(actual) is bool or type(actual) not in (int, float) or kind == "IntProperty" and type(actual) is not int
                    or not math.isfinite(actual) or actual != value):
                raise ProbeError("probe_linear_color_scalar", "The LinearColor marker/counter/component were not confirmed")

        def color_result(result, expected):
            if result.get("executed") is not True or result.get("return_type") != "StructProperty":
                raise ProbeError("probe_linear_color_return", "The LinearColor return was not confirmed")
            check_color(result.get("result"), expected)
            check_metadata(result.get("result_metadata"))

        scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        count = 0
        for value in ({"R": 0, "G": 0, "B": 0, "A": 0}, STORED):
            for name in ("ReadColorR", "EchoColor"):
                cleanup_required = True
                result = await invoke(name, {"Input": value})
                if name == "ReadColorR": scalar(result, next(field["type"] for field in methods[name]["parameters"] if field["return"]), value["R"])
                else: color_result(result, value)
                count += 1
                scalar(await invoke("ReadCallsCount"), "IntProperty", count)
                report["calls"].append({"method": name, "input": value, "result": result["result"]})
        cleanup_required = True
        result = await invoke("MakeStoredColor")
        color_result(result, STORED)
        count += 1
        scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["calls"].append({"method": "MakeStoredColor", "result": result["result"]})
        report["checks"].append("own_marker231_zero_counter_color16align4_rgba_float32_negative_hdr_no_clamp")
        invalid = [None, True, [], "color", {}, {"R": 1, "G": 2, "B": 3}, {**STORED, "X": 0},
                   {**STORED, "R": True}, {**STORED, "G": "1"}, {**STORED, "B": 1e100}, {"out": True}]
        cases = [({"Input": value}, "invalid_argument") for value in invalid]
        cases += [({}, "missing_argument"), ({"Input": STORED, "UnexpectedForProbe": 1}, "unknown_argument")]
        for supplied, code in cases:
            refusal = await invoke("EchoColor", supplied, expected_error=code)
            report["last_refusal"] = refusal
            check_rejection(refusal, "EchoColor", supplied, code)
            scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["rejections"].append({"arguments": supplied, "code": code})
        local_nonfinite = 0
        for value in (float("nan"), float("inf"), -float("inf")):
            try: await invoke("EchoColor", {"Input": {**STORED, "R": value}})
            except ValueError: local_nonfinite += 1
            else: raise ProbeError("probe_nonfinite_transport", "Nonfinite JSON was sent to MCP")
        report["local_nonfinite_json_rejections"] = local_nonfinite

        native = await find(native=True)
        native_methods = {}
        for name in NATIVE_METHODS:
            listing = await call("sn2_object_functions", {"handle": native["handle"], "function_query": name, "limit": 10})
            report["native_math"][name] = {"reflected_functions": listing, "tested": False}
            try:
                native_methods[name] = check_signature(listing, name, native=True)
            except ProbeError as error:
                if error.code not in {"probe_signature_absent", "probe_signature_mismatch", "probe_signature_flags",
                                      "probe_linear_color_metadata", "probe_linear_color_policy", "probe_signature_token_absent"}:
                    raise
                report["native_math"][name].update(status="unsupported_not_tested", reason_code=error.code, reason=str(error))
        if "MakeColor" in native_methods:
            method, token = native_methods["MakeColor"]
            inputs = [field for field in method["parameters"] if field["return"] is False]
            # RGBA name semantics are confirmed by the API; reflection order does not replace names.
            by_axis = {field["name"]: field for field in inputs}
            if set(by_axis) != set(STORED):
                report["native_math"]["MakeColor"].update(status="unsupported_not_tested", reason="Native MakeColor did not confirm R/G/B/A input names")
            else:
                result = await call("sn2_invoke", {"handle": native["handle"], "function": "MakeColor", "arguments": STORED, "expected_signature_token": token})
                color_result(result, STORED)
                report["native_math"]["MakeColor"].update(tested=True, result=result["result"], signature_token=token)
        if "LinearColor_ToNewOpacity" in native_methods:
            method, token = native_methods["LinearColor_ToNewOpacity"]
            color_field = next(field for field in method["parameters"] if field["return"] is False and field["type"] == "StructProperty")
            alpha_field = next(field for field in method["parameters"] if field["return"] is False and field["type"] != "StructProperty")
            result = await call("sn2_invoke", {"handle": native["handle"], "function": "LinearColor_ToNewOpacity",
                "arguments": {color_field["name"]: STORED, alpha_field["name"]: 0.75}, "expected_signature_token": token})
            color_result(result, {**STORED, "A": 0.75})
            report["native_math"]["LinearColor_ToNewOpacity"].update(tested=True, result=result["result"], signature_token=token)
        report["checks"].append("native_color_math_only_after_exact_supported_value_metadata")
        scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        error.add_note("LinearColor report: " + json.dumps(report, ensure_ascii=False, allow_nan=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME or getattr(failure, "code", None) in {
                    "game_result_error", "probe_contract_rejection_unconfirmed", "probe_scalar_out_rejection_unconfirmed"}:
                failure.add_note("LinearColor cleanup was skipped after an uncertain call/rejection; the development session owner terminates the game")
            else:
                async def finish_cleanup():
                    result = await invoke("ResetProbe")
                    if result.get("executed") is not True or result.get("return_type") != "" or result.get("result") is not None:
                        raise ProbeError("probe_linear_color_cleanup", "ResetProbe did not confirm void")
                    scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                    report["cleanup"].append("ResetProbe_zero_counter_marker231")
                try: await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="linear-color cleanup")
                except BaseException as error:
                    if failure is None: raise
                    failure.add_note("LinearColor CDO cleanup was not confirmed: " + str(error))


async def run(args):
    if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
        raise ProbeError("invalid_mcp_url", "The LinearColor probe uses the shared local HTTP MCP")
    async with streamable_http_client(args.mcp_url) as connection:
        async with ClientSession(connection[0], connection[1]) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main():
    parser = argparse.ArgumentParser(description="Check dev.23 LinearColor value input/return of the owned fixture and native math")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:27124/mcp")
    args = parser.parse_args()
    async def protected():
        with deferred_sigint(): return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2, ensure_ascii=False, allow_nan=False)); return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": str(error), "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr); return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8"); sys.exit(main())
