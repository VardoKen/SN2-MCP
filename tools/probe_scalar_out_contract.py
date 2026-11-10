"""One series of seven scalar pure-out parameters only on the owned loaded CDO."""
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

HOST = "G1RMCPScalarOutProbe"
MARKER = 171
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "GetOutputs")
VALUES = {"OutBool": True, "OutInt": -7, "OutInt64": -(2**53 + 1),
          "OutUInt32": 2**32 - 1, "OutUInt64": 2**64 - 1,
          "OutFloat32": 1.25, "OutFloat": -2.5}
TYPES = {"OutBool": ("BoolProperty", 1), "OutInt": ("IntProperty", 4),
         "OutInt64": ("Int64Property", 8), "OutUInt32": ("UInt32Property", 4),
         "OutUInt64": ("UInt64Property", 8), "OutFloat32": ("FloatProperty", 4)}
UNKNOWN_GAME = {"game_timeout", "game_call_fault", "native_timeout", "native_disconnected",
                "session_closed", "native_unavailable"}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST
            or item.get("class") != HOST or item.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "The owned scalar-out fixture CDO was not confirmed")
    return item


def check_metadata(metadata, kind: str, width: int) -> dict:
    if (not isinstance(metadata, dict) or metadata.get("type") != kind
            or type(metadata.get("element_size")) is not int or metadata["element_size"] != width
            or type(metadata.get("array_dim")) is not int or metadata["array_dim"] != 1):
        raise ProbeError("probe_scalar_out_metadata", "The exact scalar out type, width, and ArrayDim were not confirmed")
    if kind == "BoolProperty":
        layout = metadata.get("bool_layout")
        if (not isinstance(layout, dict) or set(layout) != {"field_size", "byte_offset", "byte_mask", "field_mask"}
                or any(type(value) is not int for value in layout.values())
                or layout["field_size"] != 1 or layout["byte_offset"] != 0
                or not 1 <= layout["field_mask"] <= 255 or not 1 <= layout["byte_mask"] <= 255
                or layout["field_mask"] != 255 and layout["field_mask"] & (layout["field_mask"] - 1)
                or layout["byte_mask"] != 255 and layout["byte_mask"] & (layout["byte_mask"] - 1)
                or layout["field_mask"] != 255 and layout["byte_mask"] != layout["field_mask"]):
            raise ProbeError("probe_scalar_out_metadata", "Bool out did not confirm one byte and a valid mask")
    return metadata


def check_signature(listing: dict, name: str) -> tuple[dict, str]:
    if (name not in METHODS or not isinstance(listing, dict) or listing.get("truncated") is not False
            or listing.get("scan_complete") is not True or listing.get("scan_truncated") is not False
            or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "The full owned scalar-out CDO catalog was not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "The scalar-out method was not uniquely found")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 64
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "The field count and bounded scalar-out buffer were not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    outputs = [field for field in fields if field.get("return") is False]
    return_type = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                   "GetOutputs": ("BoolProperty", 1)}.get(name)
    if (len(returned) != (return_type is not None) or len(outputs) != (7 if name == "GetOutputs" else 0)
            or len(fields) != len(returned) + len(outputs)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != return_type):
        raise ProbeError("probe_signature_mismatch", "The seven scalar out parameters and exact method return did not match")
    if any(not isinstance(field.get("name"), str) for field in fields):
        raise ProbeError("probe_signature_mismatch", "The scalar out parameter did not receive a string name")
    if outputs and ({field.get("name") for field in outputs} != set(VALUES)
                    or len({field.get("name") for field in outputs}) != len(outputs)):
        raise ProbeError("probe_signature_mismatch", "Scalar out names did not match the owned fixture")
    spans = []
    for field in fields:
        offset, width = field.get("offset"), field.get("element_size")
        if (type(offset) is not int or type(width) is not int or offset < 0 or width < 1 or offset + width > 64
                or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or type(field.get("out")) is not bool or field.get("reference") is not False
                or field.get("const") is not False
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_mismatch", "ArrayDim, const/ref, or scalar-out buffer bounds were not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "Scalar-out parameter ranges overlap")
        spans.append((offset, offset + width))
        if "flags" in field:
            flags = field["flags"]
            if (type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80
                    or bool(flags & 0x400) is not field["return"] or bool(flags & 0x100) is not field["out"]
                    or flags & (0x2 | 0x08000000)):
                raise ProbeError("probe_signature_flags", "Public CPF flags did not match pure out/return without const/ref")
        if field["return"] is False:
            expected = TYPES.get(field["name"])
            if field["name"] == "OutFloat":
                expected = (field.get("type"), width)
                if expected not in (("FloatProperty", 4), ("DoubleProperty", 8)):
                    raise ProbeError("probe_signature_mismatch", "OutFloat did not confirm Float4 or Double8")
            marker = field.get("argument_marker")
            if ((field.get("type"), width) != expected or field.get("out") is not True
                    or field.get("direction") != "output" or not isinstance(marker, dict)
                    or set(marker) != {"out"} or marker["out"] is not True):
                raise ProbeError("probe_signature_mismatch", "Scalar out did not confirm the exact type, direction, and marker")
            check_metadata(field.get("out_metadata"), *expected)
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "The scalar-out method did not receive a valid token")
    return method, token


def check_scalar(result: dict, kind: str, expected) -> None:
    if (result.get("executed") is not True or result.get("return_type") != kind
            or "result" not in result or type(result["result"]) is not type(expected) or result["result"] != expected):
        raise ProbeError("probe_scalar_out_result", "The scalar out marker, return, or counter were not confirmed")


def check_rejection(result: dict, method: str, value, code: str = "invalid_argument") -> None:
    effect = result.get("effect")
    nested = effect if isinstance(effect, dict) else {}
    if (result.get("code") != code or result.get("may_have_executed") is not False
            or effect is not None and not isinstance(effect, dict)
            or not (result.get("executed") is False or nested.get("executed") is False)
            or "executed" in result and result["executed"] is not False
            or "executed" in nested and nested["executed"] is not False
            or "may_have_executed" in nested and nested["may_have_executed"] is not False):
        diagnostic = json.dumps({"method": method, "arguments": value, "expected_code": code, "payload": result}, ensure_ascii=False)
        raise ProbeError("probe_scalar_out_rejection_unconfirmed", "Scalar out rejection before execution was not confirmed: " + diagnostic)


def check_outputs(result: dict, method: dict) -> dict:
    check_scalar(result, "BoolProperty", True)
    values, metadata = result.get("out_parameters"), result.get("out_parameter_metadata")
    if (not isinstance(values, dict) or set(values) != set(VALUES)
            or not isinstance(metadata, dict) or set(metadata) != set(VALUES)):
        raise ProbeError("probe_scalar_out_names", "The return did not preserve exactly seven named scalar out parameters and metadata")
    fields = {field["name"]: field for field in method["parameters"] if field["return"] is False}
    for name, expected in VALUES.items():
        value, field = values[name], fields[name]
        if (type(value) is not type(expected) or value != expected
                or type(value) is float and not math.isfinite(value)):
            raise ProbeError("probe_scalar_out_value", "The named scalar out did not preserve the exact value and JSON type: " + name)
        check_metadata(metadata[name], field["type"], field["element_size"])
        if metadata[name] != field["out_metadata"]:
            raise ProbeError("probe_scalar_out_metadata", "Result metadata differs from the verified reflection: " + name)
    return values


async def exercise(call) -> dict:
    cleanup_required, failure = False, None
    report = {"checks": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "game_launched": False, "dll_loaded": False, "const_ref": "not_tested", "inout": "not_tested"}
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and the current native session were not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                    "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
            raise ProbeError("probe_object_absent", "The owned scalar-out CDO was not uniquely found")
        target = check_target(found["items"][0])
        handle, tokens, methods = target["handle"], {}, {}
        for name in METHODS:
            listing = await call("g1r_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            # Preserve the public listing, CPF flags, and metadata without an additional game series.
            report["reflected_functions"][name] = listing
            methods[name], tokens[name] = check_signature(listing, name)
        if len(set(tokens.values())) != len(tokens):
            raise ProbeError("probe_signature_token_alias", "Different scalar-out methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            payload = {} if arguments is None else arguments
            json.dumps(payload, allow_nan=False)
            return await call("g1r_invoke", {"handle": handle, "function": name, "arguments": payload,
                              "expected_signature_token": tokens[name]}, expected_error=expected_error)

        check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        report["checks"].append("own_marker171_zero_counter_seven_exact_pure_out_signatures")
        arguments = {name: {"out": True} for name in VALUES}
        count = 0
        for supplied in (arguments, dict(reversed(list(arguments.items())))):
            cleanup_required = True
            result = await invoke("GetOutputs", supplied)
            report["out_parameters"] = check_outputs(result, methods["GetOutputs"])
            report["out_parameter_metadata"] = result["out_parameter_metadata"]
            count += 1
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["checks"].append("named_outputs_preserve_bool_wide_signed_unsigned_fractional_values_and_argument_order")
        malformed = [None, True, {}, {"out": False}, {"out": 1}, {"out": True, "value": 0}]
        rejected = [(name, {**arguments, name: value}, "invalid_argument") for name in VALUES for value in malformed]
        rejected += [(name, {key: value for key, value in arguments.items() if key != name}, "missing_argument") for name in VALUES]
        rejected.append(("unknown_name", {**arguments, "UnknownForProbe": {"out": True}}, "unknown_argument"))
        for name, supplied, code in rejected:
            refusal = await invoke("GetOutputs", supplied, expected_error=code)
            check_rejection(refusal, "GetOutputs", supplied, code)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["rejections"].append({"argument": name, "arguments": supplied, "code": code})
        report["checks"].append("every_out_marker_and_missing_or_unknown_name_refused_without_counter_change")
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        # Preserve signatures on rejection as well so that one run remains diagnostic evidence.
        if hasattr(error, "add_note"):
            error.add_note("Scalar-out report: " + json.dumps(report, ensure_ascii=False, allow_nan=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("Scalar out cleanup was skipped after an uncertain call or unavailable connection; the development session owner terminates the game")
            else:
                async def finish_cleanup():
                    check_scalar(await invoke("ResetProbe"), "", None)
                    check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                    report["cleanup"].append("ResetProbe_zero_counter_marker171")
                try:
                    await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="scalar-out contract cleanup")
                except BaseException as error:
                    if failure is None:
                        raise
                    failure.add_note("Owned scalar-out CDO cleanup was not confirmed: " + str(error))


async def run(args) -> dict:
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "The owned CDO check uses only local MCP")
        async with streamable_http_client(args.mcp_url) as connection:
            async with ClientSession(connection[0], connection[1]) as session:
                initialized = await session.initialize()
                return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dev.17 seven scalar pure-out parameters of the owned loaded fixture")
    parser.add_argument("--mcp-url")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    async def protected():
        with deferred_sigint():
            return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2, allow_nan=False))
        return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
