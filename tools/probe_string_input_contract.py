"""FString const input on the owned AS CDO, UTF-16, and confirmed cleanup."""
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

HOST = "G1RMCPStringInputProbe"
MARKER = 211
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "ReadInputLength", "ReadEcho", "ConcatInputs")
SAMPLES = ("", "Bridge", "Gothic", "A😀B")
CONSTRAINTS = {"max_utf16_units": 2147483646, "embedded_nul": "rejected", "ownership": "engine_owned_copy"}


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def check_signature(listing: dict, name: str):
    if (name not in METHODS or not isinstance(listing, dict) or listing.get("truncated") is not False
            or listing.get("scan_complete") is not True or listing.get("scan_truncated") is not False
            or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "The full FString fixture catalog was not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1: raise ProbeError("probe_signature_absent", "The FString method was not uniquely found")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 64
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "NumParms and the bounded FString buffer were not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    arguments = [field for field in fields if field.get("return") is False]
    expected_return = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                       "ReadInputLength": ("IntProperty", 4), "ReadEcho": ("StrProperty", 16),
                       "ConcatInputs": ("StrProperty", 16)}.get(name)
    expected_names = ("Left", "Right") if name == "ConcatInputs" else ("Input",) if name in ("ReadEcho", "ReadInputLength") else ()
    if (len(returned) != (expected_return is not None) or len(arguments) != len(expected_names)
            or len(fields) != len(returned) + len(arguments)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != expected_return
            or any(not isinstance(field.get("name"), str) for field in fields)
            or {field["name"] for field in arguments} != set(expected_names)):
        raise ProbeError("probe_signature_mismatch", "Exact FString method input names, types, and return did not match")
    spans = []
    for field in fields:
        flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
        if (type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80
                or bool(flags & 0x400) is not field["return"] or bool(flags & 0x100) is not field.get("out")
                or bool(flags & 0x2) is not field.get("const") or bool(flags & 0x08000000) is not field.get("reference")
                or type(offset) is not int or offset < 0 or type(width) is not int or width < 1 or offset + width > 64
                or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_flags", "CPF flags, ArrayDim, or FString parameter bounds were not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "FString parameter ranges overlap")
        spans.append((offset, offset + width))
        if field["return"]:
            if flags & (0x2 | 0x08000000): raise ProbeError("probe_signature_flags", "The FString method return must be by value")
        elif (field.get("type") != "StrProperty" or width != 16 or not flags & 0x2
              or not flags & (0x100 | 0x08000000) or field.get("direction") != "input"
              or field.get("passing") != "const_reference" or field.get("argument_constraints") != CONSTRAINTS
              or "argument_marker" in field):
            raise ProbeError("probe_string_input_policy", "FString input did not confirm const-reference with engine_owned_copy/UTF16 limit")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "The FString method did not receive a signature token")
    return method, token


def check_cleanup(result: dict, method: str) -> dict:
    if result.get("cleanup_confirmed") is not True or result.get("input_cleanup_confirmed") is not True:
        error = ProbeError("probe_string_cleanup_unconfirmed", "Engine FString input/return cleanup was not confirmed: " + method)
        error.add_note("FString cleanup payload: " + json.dumps(result, ensure_ascii=False))
        raise error
    return {key: result[key] for key in result if "cleanup" in key}


async def exercise(call):
    cleanup_required, failure = False, None
    report = {"checks": [], "calls": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "game_launched": False, "dll_loaded": False, "native_string": "not_tested", "by_value": "not_tested"}
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and the native session were not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
            "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
            raise ProbeError("probe_object_absent", "The exact owned FString CDO was not uniquely found")
        target = found["items"][0]
        if (not isinstance(target, dict) or target.get("name") != "Default__" + HOST or target.get("class") != HOST
                or target.get("path") != "/Script/Angelscript.Default__" + HOST or not isinstance(target.get("handle"), str)
                or not target["handle"].startswith("obj-")):
            raise ProbeError("unsafe_probe_target", "The exact owned FString CDO was not confirmed")
        handle, tokens = target["handle"], {}
        for name in METHODS:
            listing = await call("g1r_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            _, tokens[name] = check_signature(listing, name)
        if len(set(tokens.values())) != len(tokens): raise ProbeError("probe_signature_token_alias", "Different FString methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            return await call("g1r_invoke", {"handle": handle, "function": name,
                "arguments": {} if arguments is None else arguments, "expected_signature_token": tokens[name]}, expected_error=expected_error)

        check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        report["checks"].append("own_marker211_counter0_exact_const_fstring16_full_reflection")
        count = 0
        for text in SAMPLES:
            for name, expected in (("ReadInputLength", utf16_units(text)), ("ReadEcho", text)):
                cleanup_required = True
                result = await invoke(name, {"Input": text})
                cleanup = check_cleanup(result, name)
                check_scalar(result, "IntProperty" if name == "ReadInputLength" else "StrProperty", expected)
                count += 1
                check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
                report["calls"].append({"method": name, "input": text, "utf16_units": utf16_units(text), "result": expected, "cleanup": cleanup})
        supplied = {"Left": "Gothic ", "Right": "😀"}
        cleanup_required = True
        result = await invoke("ConcatInputs", supplied)
        expected = supplied["Left"] + supplied["Right"]
        cleanup = check_cleanup(result, "ConcatInputs")
        check_scalar(result, "StrProperty", expected)
        count += 1
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["calls"].append({"method": "ConcatInputs", "inputs": supplied, "utf16_units": utf16_units(expected), "result": expected, "cleanup": cleanup})
        report["checks"].append("empty_ascii_cyrillic_emoji_utf16_length_echo_and_two_owned_inputs_concat")
        invalid = [None, True, 1, 1.25, [], {}, {"out": True}, "A\0B", "x" * 32768, "😀" * 16384]
        cases = [("ReadInputLength", {"Input": value}, "invalid_argument") for value in invalid]
        cases += [("ReadEcho", {"Input": {"value": "Bridge"}}, "invalid_argument"),
                  ("ReadEcho", {}, "missing_argument"), ("ReadEcho", {"Input": "Bridge", "UnexpectedForProbe": 1}, "unknown_argument"),
                  ("ConcatInputs", {"Left": "Bridge", "Right": "A\0B"}, "invalid_argument"),
                  ("ConcatInputs", {"Left": "Bridge"}, "missing_argument"),
                  ("ConcatInputs", {**supplied, "UnexpectedForProbe": 1}, "unknown_argument")]
        for name, arguments, code in cases:
            refusal = await invoke(name, arguments, expected_error=code)
            check_rejection(refusal, name, {key: {"utf16_units": utf16_units(value)} if isinstance(value, str) else value for key, value in arguments.items()}, code)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            compact = {key: {"utf16_units": utf16_units(value), "contains_nul": "\0" in value}
                       if isinstance(value, str) else value for key, value in arguments.items()}
            diagnostics = {key: value for key, value in refusal.items() if "cleanup" in key or "quarantin" in key}
            effect = refusal.get("effect")
            if isinstance(effect, dict):
                diagnostics.update({key: value for key, value in effect.items() if "cleanup" in key or "quarantin" in key})
            report["rejections"].append({"method": name, "arguments": compact, "code": code, "cleanup": diagnostics})
        report["checks"].append("wrong_shapes_nul_utf16_limit_missing_unknown_refused_before_counter_change")
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        report["failure_signal"] = {"code": getattr(error, "code", "probe_failed"), "message": str(error)}
        error.add_note("String input report: " + json.dumps(report, ensure_ascii=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME or getattr(failure, "code", None) in {
                    "game_result_error", "probe_string_cleanup_unconfirmed", "string_ownership_unknown",
                    "string_input_quarantined", "string_inputs_quarantined"}:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("FString fixture cleanup was skipped after uncertain ownership or an unavailable connection; the development session owner terminates the game")
            else:
                async def finish_cleanup():
                    check_scalar(await invoke("ResetProbe"), "", None)
                    check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    check_scalar(await invoke("ReadMarker"), "IntProperty", MARKER)
                    report["cleanup"].append("ResetProbe_zero_counter_marker211")
                try: await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="string input contract cleanup")
                except BaseException as error:
                    if failure is None: raise
                    failure.add_note("Owned FString CDO cleanup was not confirmed: " + str(error))


async def run(args):
    if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
        raise ProbeError("invalid_mcp_url", "The FString probe uses the shared local HTTP MCP")
    async with streamable_http_client(args.mcp_url) as connection:
        async with ClientSession(connection[0], connection[1]) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main():
    parser = argparse.ArgumentParser(description="Check dev.21 const FString inputs of the owned fixture through the shared HTTP MCP")
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
