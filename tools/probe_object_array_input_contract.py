"""Const TArray<UObject> input on the owned AS CDO, handles, effects, and copy cleanup."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_function_contract_mcp import TOKEN, make_caller
from probe_object_mcp import ProbeError, _single_error
from probe_scalar_out_contract import UNKNOWN_GAME, check_rejection, check_scalar

HOST = "G1RMCPObjectArrayInputProbe"
MARKER = 221
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "ReadCount", "ReadNullCount", "ContainsSelf")
INPUT_METHODS = ("ReadCount", "ReadNullCount", "ContainsSelf")
CONSTRAINTS = {"max_elements": 2147483647, "element_form": "null_or_handle", "ownership": "engine_owned_pointer_copy"}


def check_array_metadata(metadata):
    if (not isinstance(metadata, dict) or metadata.get("inner_type") != "ObjectProperty"
            or type(metadata.get("inner_element_size")) is not int or metadata["inner_element_size"] != 8
            or type(metadata.get("inner_array_dim")) is not int or metadata["inner_array_dim"] != 1
            or type(metadata.get("inner_offset")) is not int or metadata["inner_offset"] != 0
            or not isinstance(metadata.get("property_class"), dict)
            or metadata["property_class"].get("path") != "/Script/CoreUObject.Object"
            or metadata["property_class"].get("name") != "Object"):
        raise ProbeError("probe_object_array_metadata", "ObjectProperty8 inner ArrayDim1/offset0 with declared CoreUObject.Object was not confirmed")
    return metadata


def check_signature(listing, name):
    if (name not in METHODS or not isinstance(listing, dict) or listing.get("truncated") is not False
            or listing.get("scan_complete") is not True or listing.get("scan_truncated") is not False
            or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "The full object-array fixture catalog was not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1: raise ProbeError("probe_signature_absent", "The object-array method was not uniquely found")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 32
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "NumParms and the bounded object-array buffer were not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    arguments = [field for field in fields if field.get("return") is False]
    expected = ("BoolProperty", 1) if name == "ContainsSelf" else ("IntProperty", 4) if name != "ResetProbe" else None
    if (len(returned) != (expected is not None) or len(arguments) != (name in INPUT_METHODS)
            or len(fields) != len(returned) + len(arguments)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != expected):
        raise ProbeError("probe_signature_mismatch", "Object-array input and the exact scalar return did not match")
    spans = []
    for field in fields:
        flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
        if (not isinstance(field.get("name"), str) or type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80
                or bool(flags & 0x400) is not field["return"] or bool(flags & 0x100) is not field.get("out")
                or bool(flags & 0x2) is not field.get("const") or bool(flags & 0x08000000) is not field.get("reference")
                or type(offset) is not int or offset < 0 or type(width) is not int or width < 1 or offset + width > 32
                or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_flags", "Object-array CPF, ArrayDim, or bounds were not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "Object-array ranges overlap")
        spans.append((offset, offset + width))
        if field["return"]:
            if flags & (0x2 | 0x08000000): raise ProbeError("probe_signature_flags", "The object-array scalar return must be by value")
        else:
            if (field["name"] != "Input" or field.get("type") != "ArrayProperty" or width != 16 or not flags & 0x2
                    or not flags & (0x100 | 0x08000000) or field.get("direction") != "input"
                    or field.get("passing") != "const_reference" or field.get("argument_constraints") != CONSTRAINTS
                    or "argument_marker" in field):
                raise ProbeError("probe_object_array_policy", "Object-array did not confirm const-reference input/ownedcopy/limit32")
            check_array_metadata(field.get("array"))
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "The object-array method did not receive a signature token")
    return method, token


def check_cleanup(result, method):
    if result.get("cleanup_confirmed") is not True or result.get("array_input_cleanup_confirmed") is not True:
        error = ProbeError("probe_object_array_cleanup_unconfirmed", "Object-array copy cleanup was not confirmed: " + method)
        error.add_note("Object-array cleanup payload: " + json.dumps(result, ensure_ascii=False))
        raise error
    return {key: result[key] for key in result if "cleanup" in key}


async def exercise(call):
    cleanup_required, failure = False, None
    report = {"checks": [], "calls": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "game_launched": False, "dll_loaded": False, "native_functions": "not_tested", "actor_spawn": "not_tested"}
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and the native session were not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
            "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
            raise ProbeError("probe_object_absent", "The exact owned object-array CDO was not uniquely found")
        target = found["items"][0]
        if (not isinstance(target, dict) or target.get("name") != "Default__" + HOST or target.get("class") != HOST
                or target.get("path") != "/Script/Angelscript.Default__" + HOST or not isinstance(target.get("handle"), str)
                or not target["handle"].startswith("obj-")):
            raise ProbeError("unsafe_probe_target", "The exact owned object-array CDO was not confirmed")
        handle, tokens = target["handle"], {}
        for name in METHODS:
            listing = await call("g1r_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            _, tokens[name] = check_signature(listing, name)
        if len(set(tokens.values())) != len(tokens): raise ProbeError("probe_signature_token_alias", "Different object-array methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            return await call("g1r_invoke", {"handle": handle, "function": name,
                "arguments": {} if arguments is None else arguments, "expected_signature_token": tokens[name]}, expected_error=expected_error)

        check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        report["checks"].append("own_marker221_counter0_objectproperty8_declaredObject_constarray16_fullreflection")
        own = {"handle": handle}
        inputs = [[], [None], [own], [own, None, own], [own] * 32]
        count = 0
        for values in inputs:
            for name, expected in (("ReadCount", len(values)), ("ReadNullCount", values.count(None)), ("ContainsSelf", own in values)):
                cleanup_required = True
                result = await invoke(name, {"Input": values})
                cleanup = check_cleanup(result, name)
                check_scalar(result, "BoolProperty" if name == "ContainsSelf" else "IntProperty", expected)
                count += 1
                check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
                report["calls"].append({"method": name, "input": values, "result": expected, "cleanup": cleanup})
        report["checks"].append("empty_null_self_duplicate_mixed_and32boundary_preserve_order_values_and_ownedcopy_cleanup")
        wrong_arrays = [None, True, 1, "array", {}, {"items": [own]}, [None] * 33]
        bad_elements = [True, 1, 1.25, "obj-1", {}, {"handle": ""}, {"handle": True}, {"handle": handle, "extra": 1}]
        cases = [({"Input": value}, "invalid_argument") for value in wrong_arrays]
        cases += [({"Input": [element]}, "invalid_object_argument") for element in bad_elements]
        stale = "obj-18446744073709551615"
        if stale == handle: raise ProbeError("unsafe_probe_handle", "The synthetic stale reference matched the owned CDO")
        cases += [({"Input": [{"handle": stale}]}, "stale_handle"), ({}, "missing_argument"),
                  ({"Input": [], "UnexpectedForProbe": 1}, "unknown_argument")]
        for arguments, code in cases:
            refusal = await invoke("ReadCount", arguments, expected_error=code)
            # Preserve the exact envelope before checking effects and before any new game call.
            report["last_refusal"] = refusal
            effect = refusal.get("effect")
            cleanup_sources = [refusal] + ([effect] if isinstance(effect, dict) else [])
            if any(source.get("cleanup_confirmed") is False
                   or source.get("array_input_cleanup_confirmed") is False
                   or source.get("cleanup_state") == "unknown"
                   or source.get("object_array_input_quarantined") is True
                   for source in cleanup_sources):
                error = ProbeError("probe_object_array_cleanup_unconfirmed", "The rejection did not confirm object-array cleanup; further calls were stopped")
                error.add_note("Object-array refusal payload: " + json.dumps(refusal, ensure_ascii=False))
                raise error
            check_rejection(refusal, "ReadCount", arguments, code)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            diagnostics = {key: value for key, value in refusal.items() if "cleanup" in key or "quarantin" in key}
            effect = refusal.get("effect")
            if isinstance(effect, dict): diagnostics.update({key: value for key, value in effect.items() if "cleanup" in key or "quarantin" in key})
            report["rejections"].append({"arguments": arguments, "code": code, "cleanup": diagnostics})
        report["checks"].append("array_shapes_countlimit_elementforms_stalehandle_missing_unknown_refused_before_counter_change")
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        report["failure_signal"] = {"code": getattr(error, "code", "probe_failed"), "message": str(error)}
        error.add_note("Object-array input report: " + json.dumps(report, ensure_ascii=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME or getattr(failure, "code", None) in {
                    "game_result_error", "probe_object_array_cleanup_unconfirmed", "object_array_input_quarantined",
                    "probe_contract_rejection_unconfirmed", "probe_scalar_out_rejection_unconfirmed"}:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("Object-array fixture cleanup was skipped after uncertain ownership or an unavailable connection; the development session owner terminates the game")
            else:
                async def finish_cleanup():
                    check_scalar(await invoke("ResetProbe"), "", None)
                    check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                    report["cleanup"].append("ResetProbe_zero_counter_marker221")
                try: await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="object-array input contract cleanup")
                except BaseException as error:
                    if failure is None: raise
                    failure.add_note("Owned object-array CDO cleanup was not confirmed: " + str(error))


async def run(args):
    if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
        raise ProbeError("invalid_mcp_url", "The object-array probe uses the shared local HTTP MCP")
    async with streamable_http_client(args.mcp_url) as connection:
        async with ClientSession(connection[0], connection[1]) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main():
    parser = argparse.ArgumentParser(description="Check dev.22 const object-array inputs of the owned CDO through the shared HTTP MCP")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:27124/mcp")
    args = parser.parse_args()
    async def protected():
        with deferred_sigint(): return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2, ensure_ascii=False))
        return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": str(error), "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
