"""Check FName returns on an owned object through a ready MCP."""
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

HOST = "G1RMCPNameProbe"
PROPERTIES = {"StoredEntry": "Entry", "StoredNone": "None"}
METHODS = {"ReadMarker": ("IntProperty", 4), "ReadEntry": ("NameProperty", 8), "ReadNone": ("NameProperty", 8),
           "ReadText": ("StrProperty", 16), "ReadEmptyText": ("StrProperty", 16)}
INPUT_METHODS = {"ReadMarker": ([], ("IntProperty", 4)),
                 "EchoName": ([("Input", "NameProperty", 8)], ("NameProperty", 8)),
                 "ReadCallsCount": ([], ("IntProperty", 4)), "ResetProbe": ([], None)}


def check_target(item: dict) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST
            or item.get("class") != HOST or item.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "The owned FName probe CDO was not confirmed")
    return item


def check_signature(functions: dict, method_name: str) -> dict:
    if method_name not in METHODS or not isinstance(functions, dict):
        raise ProbeError("probe_signature_invalid", "Unknown FName fixture method")
    matches = [item for item in functions.get("items", []) if item.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "The FName fixture method was not uniquely found")
    method = matches[0]
    parameters = method.get("parameters")
    if not isinstance(parameters, list) or len(parameters) != 1 or not isinstance(parameters[0], dict):
        raise ProbeError("probe_signature_mismatch", "The method must have only a return value")
    value = parameters[0]
    expected_type, expected_size = METHODS[method_name]
    size, offset = method.get("parameter_size"), value.get("offset")
    if (value.get("return") is not True or value.get("reference") is True
            or value.get("type") != expected_type or value.get("element_size") != expected_size
            or type(size) is not int or not 0 <= size <= 32 or type(offset) is not int
            or offset < 0 or max(size, offset + expected_size) > 32):
        raise ProbeError("probe_signature_mismatch", "The probe method's zero input and return type were not confirmed")
    return method


def check_snapshot(snapshot: dict, target: dict, session_id: str) -> None:
    objects = snapshot.get("objects")
    if (snapshot.get("session_id") != session_id or snapshot.get("partial") is not False
            or snapshot.get("truncated") is not False or not isinstance(objects, list) or len(objects) != 1
            or snapshot.get("source") != "live_native" or snapshot.get("consistency") != "single_tick_sequential"
            or any(objects[0].get(key) != target[key] for key in ("handle", "path", "class")) or objects[0].get("error")):
        raise ProbeError("probe_snapshot_invalid", "The full FName snapshot of the current session was not confirmed")
    for name, expected in PROPERTIES.items():
        matches = [field for field in objects[0].get("properties", []) if field.get("name") == name]
        if (len(matches) != 1 or matches[0].get("type") != "NameProperty" or matches[0].get("supported") is not True
                or matches[0].get("error") or type(matches[0].get("value")) is not str or matches[0]["value"] != expected):
            raise ProbeError("probe_name_property", "The expected owned FName property value did not match")


def check_input_signature(functions: dict, method_name: str) -> dict:
    if (method_name not in INPUT_METHODS or not isinstance(functions, dict) or
            functions.get("truncated") is not False or functions.get("scan_complete") is not True or
            functions.get("scan_truncated") is not False or not isinstance(functions.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "The full FName input method catalog was not confirmed")
    methods = [item for item in functions["items"] if isinstance(item, dict) and item.get("name") == method_name]
    if len(methods) != 1:
        raise ProbeError("probe_signature_absent", "The FName input method was not uniquely found")
    method = methods[0]
    params, size = method.get("parameters"), method.get("parameter_size")
    expected_inputs, expected_return = INPUT_METHODS[method_name]
    if (not isinstance(params, list) or type(size) is not int or not 0 <= size <= 32 or
            method.get("parameters_truncated") is not False or any(not isinstance(param, dict) for param in params)):
        raise ProbeError("probe_signature_mismatch", "The full FName input signature was not confirmed")
    inputs = [param for param in params if param.get("return") is False]
    returns = [param for param in params if param.get("return") is True]
    if (len(inputs) != len(expected_inputs) or len(returns) != (expected_return is not None) or
            len(params) != len(inputs) + len(returns)):
        raise ProbeError("probe_signature_mismatch", "The FName input parameter count did not match")
    for param, expected in zip(inputs, expected_inputs):
        if (param.get("name"), param.get("type"), param.get("element_size")) != expected:
            raise ProbeError("probe_signature_mismatch", "The FName input name, type, or size did not match")
    if expected_return is not None and (returns[0].get("type"), returns[0].get("element_size")) != expected_return:
        raise ProbeError("probe_signature_mismatch", "The FName input return type or size did not match")
    for param in params:
        offset, element_size = param.get("offset"), param.get("element_size")
        if (type(offset) is not int or type(element_size) is not int or offset < 0 or element_size <= 0 or
                offset + element_size > 32 or type(param.get("array_dim")) is not int or param["array_dim"] != 1 or
                param.get("reference") is not False or type(param.get("out")) is not bool or
                (param["return"] is False and (param["out"] is not False or offset + element_size > size))):
            raise ProbeError("probe_signature_mismatch", "FName ArrayDim, out/ref, or buffer were not confirmed")
    return method


def input_snapshot_values(snapshot: dict, target: dict, session_id: str) -> tuple[str, str, int]:
    check_snapshot(snapshot, target, session_id)
    objects = snapshot["objects"]
    if objects[0].get("partial") is not False or objects[0].get("truncated") is not False:
        raise ProbeError("probe_snapshot_invalid", "The FName input snapshot must be complete")
    fields = objects[0].get("properties", [])
    counts = [field for field in fields if isinstance(field, dict) and field.get("name") == "CallsCount"]
    if (len(fields) != 3 or len(counts) != 1 or counts[0].get("type") != "IntProperty" or
            counts[0].get("supported") is not True or "error" in counts[0] or
            type(counts[0].get("value")) is not int or counts[0]["value"] < 0):
        raise ProbeError("probe_counter_invalid", "The FName call counter was not confirmed")
    return "Entry", "None", counts[0]["value"]


async def exercise(call) -> dict:
    report = {"checks": [], "cleanup": [], "game_launched": False, "dll_loaded": False}
    watch_id = None
    failure = None
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if not status.get("ready") or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "The ready queue and current session were not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST, "limit": 50})
        exact = [item for item in found.get("items", []) if item.get("name") == "Default__" + HOST]
        if len(exact) != 1:
            raise ProbeError("probe_object_absent", "The owned FName probe CDO was not uniquely found")
        target = check_target(exact[0])
        functions = await call("g1r_object_functions", {"handle": target["handle"], "limit": 50})
        for method_name in METHODS:
            check_signature(functions, method_name)

        async def invoke(method):
            result = await call("g1r_invoke", {"handle": target["handle"], "function": method, "arguments": {}})
            if result.get("executed") is not True:
                raise ProbeError("probe_not_executed", "Execution of the owned method was not confirmed")
            if METHODS[method][0] == "StrProperty" and result.get("cleanup_confirmed") is not True:
                raise ProbeError("probe_string_cleanup_unconfirmed", "The FString return did not confirm memory release")
            return result.get("result")

        marker = await invoke("ReadMarker")
        if type(marker) is not int or marker != 71:
            raise ProbeError("probe_marker_mismatch", "The owned CDO did not return marker 71")
        report["checks"].append("exact_cdo_marker_and_zero_input_signatures")
        results = {}
        for method, expected in (("ReadEntry", "Entry"), ("ReadNone", "None"), ("ReadText", "G1R MCP dev7"), ("ReadEmptyText", "")):
            values = [await invoke(method) for _ in range(3)]
            if any(type(value) is not str or value != expected for value in values):
                raise ProbeError("probe_name_result", "The repeated FName/FString return did not match or changed")
            results[method] = {"text": expected, "repetitions": len(values)}
        report["returns"] = results
        snapshot = await call("g1r_snapshot", {"targets": [{"handle": target["handle"], "properties": list(PROPERTIES)}]})
        check_snapshot(snapshot, target, session_id)
        report["checks"].append("repeated_entry_none_returns_match_properties")
        report["checks"].append("repeated_nonempty_empty_fstring_returns_cleanup_confirmed")
        watch = await call("g1r_watch_start", {"handle": target["handle"], "properties": list(PROPERTIES),
                                                "duration_ms": 5000, "interval_ms": 50, "capacity": 8})
        watch_id = watch["watch_id"]
        await asyncio.sleep(0.25)
        observed = await call("g1r_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
        events = observed.get("events", [])
        if (observed.get("session_id") != session_id or observed.get("state") != "running"
                or observed.get("dropped_events") != 0 or observed.get("gap") is True
                or len(events) != 1 or events[0].get("kind") != "baseline"):
            raise ProbeError("probe_false_name_change", "Unchanged FName properties produced losses or false changes")
        check_snapshot(events[0]["snapshot"], target, session_id)
        report["checks"].append("short_stable_watch_one_baseline_no_emitted_changes")
        report["watch"] = {"events": len(events), "dropped_events": observed["dropped_events"]}
    except BaseException as error:
        failure = error
        raise
    finally:
        async def finish_cleanup():
            errors = []
            if watch_id is not None:
                for method in ("g1r_watch_stop", "g1r_watch_forget"):
                    try:
                        result = await call(method, {"watch_id": watch_id})
                        if (result.get("cleanup_complete") is not True
                                or method == "g1r_watch_forget" and result.get("forgotten") is not True
                                or method == "g1r_watch_stop" and result.get("state") not in {"stopped", "completed", "failed"}):
                            raise ProbeError("probe_cleanup_unconfirmed", "Stopping observation was not confirmed")
                        report["cleanup"].append(method)
                    except BaseException as error:
                        errors.append(f"{method}: {getattr(error, 'code', 'failed')}")
            if errors:
                message = "Cleanup was not confirmed: " + "; ".join(errors)
                if failure is None:
                    raise ProbeError("probe_cleanup_failed", message)
                failure.add_note(message)
        try:
            await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label='name cleanup')
        except BaseException as cleanup_error:
            if failure is None:
                raise
            failure.add_note("Cleanup completion was not confirmed: " + str(cleanup_error))
    return report


async def exercise_name_inputs(call) -> dict:
    report = {"checks": [], "cleanup": [], "game_launched": False, "dll_loaded": False}
    cleanup_required = False
    failure = None
    try:
        status = await call("g1r_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Ready Tick and the current session were not confirmed")
        found = await call("g1r_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                           "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
        if not isinstance(found.get("items"), list) or len(found["items"]) != 1 or found.get("truncated") is not False:
            raise ProbeError("probe_object_absent", "The exact FName input CDO was not uniquely found")
        target = check_target(found["items"][0])
        functions = await call("g1r_object_functions", {"handle": target["handle"], "limit": 50})
        for method in INPUT_METHODS:
            check_input_signature(functions, method)

        async def invoke(method, arguments=None):
            result = await call("g1r_invoke", {"handle": target["handle"], "function": method, "arguments": arguments or {}})
            output = INPUT_METHODS[method][1]
            if (result.get("executed") is not True or "result" not in result or
                    (output is None and result["result"] is not None) or
                    (output is not None and (result.get("return_type") != output[0] or
                                            type(result["result"]) is not (str if output[0] == "NameProperty" else int)))):
                raise ProbeError("probe_call_unconfirmed", "The FName input method did not confirm the expected result")
            return result["result"]

        async def snapshot():
            value = await call("g1r_snapshot", {"targets": [{"handle": target["handle"], "properties": [*PROPERTIES, "CallsCount"]}]})
            return input_snapshot_values(value, target, session_id)

        if await invoke("ReadMarker") != 71:
            raise ProbeError("probe_marker_mismatch", "The FName input CDO did not return marker 71")
        initial = await snapshot()
        if initial != ("Entry", "None", 0) or await invoke("ReadCallsCount") != 0:
            raise ProbeError("probe_initial_state", "FName/CallsCount defaults were not confirmed before writing")
        report["checks"].append("source_name_defaults_and_zero_counter_before_echo")
        for property_name, expected in PROPERTIES.items():
            before = await snapshot()
            cleanup_required = True
            value = await invoke("EchoName", {"Input": {"name_from": {"handle": target["handle"], "property_name": property_name}}})
            after = await snapshot()
            if value != expected or after != (before[0], before[1], before[2] + 1) or await invoke("ReadCallsCount") != after[2]:
                raise ProbeError("probe_name_input_result", "EchoName did not copy the confirmed name exactly once")
        report["checks"].append("entry_and_none_copied_from_own_name_properties")
        source = {"name_from": {"handle": target["handle"], "property_name": "StoredEntry"}}
        rejected = [
            ("Entry", "invalid_name_argument"), (None, "invalid_name_argument"),
            (0, "invalid_name_argument"), ({"bits": 0}, "invalid_name_argument"),
            ({"name_from": {"handle": target["handle"]}}, "invalid_name_argument"),
            ({"name_from": {"handle": target["handle"], "property_name": "StoredEntry", "bits": 0}}, "invalid_name_argument"),
            ({**source, "value": "Entry"}, "invalid_name_argument"),
            ({"name_from": {"handle": target["handle"], "property_name": "CallsCount"}}, "unsupported_name_source"),
            ({"name_from": {"handle": target["handle"], "property_name": "MissingProperty"}}, "property_not_found"),
            ({"name_from": {"handle": "obj-missing-name-source", "property_name": "StoredEntry"}}, "stale_handle"),
        ]
        rejections = []
        for argument, code in rejected:
            before = await snapshot()
            refusal = await call("g1r_invoke", {"handle": target["handle"], "function": "EchoName", "arguments": {"Input": argument}},
                                 expected_error=code)
            if refusal.get("code") != code or refusal.get("may_have_executed") is not False or await snapshot() != before:
                raise ProbeError("probe_name_rejection_executed", "Invalid FName input was not confirmed as rejected before execution")
            rejections.append(code)
        report["rejections"] = rejections
        report["checks"].append("raw_and_invalid_name_sources_rejected_without_counter_change")
    except BaseException as error:
        failure = error
        raise
    finally:
        if cleanup_required:
            async def finish_cleanup():
                await invoke("ResetProbe")
                if await snapshot() != ("Entry", "None", 0) or await invoke("ReadCallsCount") != 0:
                    raise ProbeError("probe_cleanup_unconfirmed", "CallsCount and original FName values were not confirmed after ResetProbe")
                report["cleanup"].append("ResetProbe_and_confirmed_names_zero_counter")
            try:
                await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="name input cleanup")
            except BaseException as cleanup_error:
                if failure is None:
                    raise
                failure.add_note("FName input cleanup was not confirmed: " + str(cleanup_error))
    return report


async def run_probe(args) -> dict:
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
        "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            report = await exercise(_caller(client))
            if getattr(args, "check_name_inputs", False):
                report["name_inputs"] = await exercise_name_inputs(_caller(client))
            report["server_version"] = initialized.server_info.version
            return report


async def _run_with_signals(args):
    with deferred_sigint():
        return await run_probe(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check FName returns on the owned fixture through a ready MCP")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    parser.add_argument("--check-name-inputs", action="store_true", help="Also check FName input copying by the owned new fixture")
    args = parser.parse_args()
    try:
        report = asyncio.run(_run_with_signals(args))
    except Exception as error:
        cause = _single_error(error)
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause)),
                          "notes": getattr(cause, "__notes__", [])}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
