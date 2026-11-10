"""One FVector const-reference series on an owned AS fixture and native math CDO."""
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
from probe_scalar_out_contract import UNKNOWN_GAME, check_rejection
from probe_vector_contract import STORED, check_layout, check_parameter_vector_metadata, check_scalar, check_vector

HOST = "G1RMCPConstVectorProbe"
MARKER = 181
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "GetVector", "EchoConstVector")
NATIVE_CLASS = "KismetMathLibrary"
NATIVE_PATH = "/Script/Engine.Default__KismetMathLibrary"
NATIVE_METHOD = "Vector_IsZero"


def check_target(item, *, native=False) -> dict:
    host = NATIVE_CLASS if native else HOST
    path = NATIVE_PATH if native else "/Script/Angelscript.Default__" + HOST
    if (not isinstance(item, dict) or item.get("name") != "Default__" + host or item.get("class") != host
            or item.get("path") != path or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Exact owned const-vector fixture or native math CDO is not confirmed")
    return item


def check_signature(listing: dict, name: str, vector_size: int, *, native=False) -> tuple[dict, str]:
    if (name not in ((NATIVE_METHOD,) if native else METHODS) or not isinstance(listing, dict)
            or listing.get("truncated") is not False or listing.get("scan_complete") is not True
            or listing.get("scan_truncated") is not False or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete const-vector method catalog is not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Const-vector method was not found uniquely")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 64
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "NumParms and bounded const-vector buffer are not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    arguments = [field for field in fields if field.get("return") is False]
    expected_return = (("BoolProperty", 1) if native else
        {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
         "GetVector": ("StructProperty", vector_size), "EchoConstVector": ("StructProperty", vector_size)}.get(name))
    has_input = native or name == "EchoConstVector"
    if (len(returned) != (expected_return is not None) or len(arguments) != has_input
            or len(fields) != len(returned) + len(arguments)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != expected_return):
        raise ProbeError("probe_signature_mismatch", "Exact const FVector input and by-value return do not match")
    if arguments and (not isinstance(arguments[0].get("name"), str) or not arguments[0]["name"]
                      or not native and arguments[0]["name"] != "Input"):
        raise ProbeError("probe_signature_mismatch", "Single FVector input name is not confirmed")
    spans = []
    for field in fields:
        flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
        if (not isinstance(field.get("name"), str) or type(offset) is not int or type(width) is not int
                or offset < 0 or width < 1 or offset + width > 64 or type(field.get("array_dim")) is not int
                or field["array_dim"] != 1 or type(flags) is not int or not 0 <= flags < 2**64
                or not flags & 0x80 or bool(flags & 0x400) is not field["return"]
                or bool(flags & 0x100) is not field.get("out")
                or bool(flags & 0x08000000) is not field.get("reference")
                or bool(flags & 0x2) is not field.get("const")
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_flags", "CPF flags, ArrayDim, or const-vector buffer bounds are not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "Const-vector parameter ranges overlap")
        spans.append((offset, offset + width))
        if field["return"]:
            if flags & (0x2 | 0x08000000):
                raise ProbeError("probe_signature_mismatch", "Return must be by value without const/ref")
            if field["type"] == "StructProperty":
                check_parameter_vector_metadata(field, vector_size, "return")
        else:
            if (field.get("type") != "StructProperty" or width != vector_size or not flags & 0x2
                    or not flags & (0x100 | 0x08000000) or field.get("passing") != "const_reference"
                    or "argument_marker" in field):
                raise ProbeError("probe_const_vector_policy", "Input did not confirm Const+(Ref|Out), passing=const_reference, without an out marker")
            check_parameter_vector_metadata(field, vector_size, "input")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "Const-vector method did not receive a valid token")
    return method, token


async def exercise(call) -> dict:
    cleanup_required, failure = False, None
    report = {"checks": [], "echoes": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "native_math": {"tested": False}, "game_launched": False, "dll_loaded": False, "inout": "not_tested"}
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and current native session are not confirmed")

        async def find(native=False):
            host = NATIVE_CLASS if native else HOST
            found = await call("sn2_find_objects", {"query": "Default__" + host, "class_query": host,
                "exact_path": NATIVE_PATH if native else "/Script/Angelscript.Default__" + HOST, "limit": 2})
            if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
                raise ProbeError("probe_object_absent", "Exact const-vector/native-math CDO was not found uniquely")
            return check_target(found["items"][0], native=native)

        target = await find()
        handle = target["handle"]
        layout = check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
        methods, tokens = {}, {}
        for name in METHODS:
            listing = await call("sn2_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            methods[name], tokens[name] = check_signature(listing, name, layout["size"])
        if len(set(tokens.values())) != len(tokens):
            raise ProbeError("probe_signature_token_alias", "Different const-vector methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            payload = {} if arguments is None else arguments
            json.dumps(payload, allow_nan=False)
            return await call("sn2_invoke", {"handle": handle, "function": name, "arguments": payload,
                              "expected_signature_token": tokens[name]}, expected_error=expected_error)

        check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        result = await invoke("GetVector")
        if result.get("executed") is not True or result.get("return_type") != "StructProperty":
            raise ProbeError("probe_const_vector_return", "GetVector did not confirm a by-value return")
        check_vector(result.get("result"), STORED)
        check_parameter_vector_metadata({"struct": result.get("result_metadata"), "direction": "return"}, layout["size"], "return")
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens, layout=layout)
        report["checks"].append("own_marker181_counter0_exact_const_reference_flags_and_stored_vector")
        count = 0
        vectors = [{"X": 0, "Y": 0, "Z": 0}, {"X": -1.25, "Y": 2.5, "Z": -3.75}]
        if layout["component_width"] == 8:
            vectors.append({"X": 2**53 + 2, "Y": -(2**40) - 0.5, "Z": 0.125})
        for value in vectors:
            cleanup_required = True
            result = await invoke("EchoConstVector", {"Input": value})
            if result.get("executed") is not True or result.get("return_type") != "StructProperty":
                raise ProbeError("probe_const_vector_return", "EchoConstVector did not confirm a by-value return")
            check_vector(result.get("result"), value)
            check_parameter_vector_metadata({"struct": result.get("result_metadata"), "direction": "return"}, layout["size"], "return")
            count += 1
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["echoes"].append(value)
        report["checks"].append("const_reference_nested_xyz_echo_zero_negative_fractional_layout_aware_large_double")
        invalid = [None, True, "FVector", [], {"X": 1, "Y": 2}, {"X": 1, "Y": 2, "Z": 3, "W": 4},
                   {"X": True, "Y": 2, "Z": 3}, {"X": "1", "Y": 2, "Z": 3}, {"out": True}]
        cases = [({"Input": value}, "invalid_argument") for value in invalid]
        cases += [({}, "missing_argument"), ({"Input": STORED, "UnexpectedForProbe": 1}, "unknown_argument")]
        for supplied, code in cases:
            refusal = await invoke("EchoConstVector", supplied, expected_error=code)
            check_rejection(refusal, "EchoConstVector", supplied, code)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["rejections"].append({"arguments": supplied, "code": code})
        local_nonfinite = 0
        for value in (float("nan"), float("inf"), -float("inf")):
            try:
                await invoke("EchoConstVector", {"Input": {"X": value, "Y": 2, "Z": 3}})
            except ValueError:
                local_nonfinite += 1
            else:
                raise ProbeError("probe_nonfinite_transport", "Nonfinite JSON was sent to MCP instead of being rejected locally")
        report["local_nonfinite_json_rejections"] = local_nonfinite
        check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
        report["checks"].append("malformed_const_vector_missing_unknown_and_nonfinite_json_refused_without_counter_change")

        native_target = await find(native=True)
        native_listing = await call("sn2_object_functions", {"handle": native_target["handle"], "function_query": NATIVE_METHOD, "limit": 10})
        report["native_math"].update(target=native_target, reflected_functions=native_listing)
        native_method, native_token = check_signature(native_listing, NATIVE_METHOD, layout["size"], native=True)
        input_name = next(field["name"] for field in native_method["parameters"] if field["return"] is False)
        results = []
        for value, expected in (({"X": 0, "Y": 0, "Z": 0}, True), (STORED, False)):
            result = await call("sn2_invoke", {"handle": native_target["handle"], "function": NATIVE_METHOD,
                "arguments": {input_name: value}, "expected_signature_token": native_token})
            check_scalar(result, "BoolProperty", expected)
            results.append({"input": value, "result": expected})
        report["native_math"].update(tested=True, signature_token=native_token, input_name=input_name, results=results)
        report["checks"].append("native_kismet_const_vector_math_zero_true_nonzero_false_exact_reflection")
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        error.add_note("Const-vector report: " + json.dumps(report, ensure_ascii=False, allow_nan=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("Const-vector cleanup skipped after an uncertain call or unavailable connection; the development session owner stops the game")
            else:
                async def finish_cleanup():
                    check_scalar(await invoke("ResetProbe"), "", None)
                    check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                    check_layout(await call("sn2_read_struct", {"handle": handle, "property_name": "StoredVector"}), target, session_id)
                    report["cleanup"].append("ResetProbe_zero_counter_marker181_unchanged_stored_vector")
                try:
                    await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="const-vector contract cleanup")
                except BaseException as error:
                    if failure is None:
                        raise
                    failure.add_note("Owned const-vector CDO cleanup is not confirmed: " + str(error))


async def run(args) -> dict:
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "Const-vector probe uses only local MCP")
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
    parser = argparse.ArgumentParser(description="Check dev.18 const FVector input on an owned AS fixture and native math")
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
