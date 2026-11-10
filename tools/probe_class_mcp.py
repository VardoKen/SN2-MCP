"""Check only the owned class fixture through ready MCP without launching the game."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import settle_task, deferred_sigint
from probe_object_mcp import ProbeError, _caller, _single_error

HOST = "G1RMCPClassProbe"
CLASS_NAMES = ("G1RMCPClassBase", "G1RMCPClassDerived", "G1RMCPClassOther")
PROPERTIES = ["StoredAnyClass", "StoredTypedClass"]
SIGNATURES = {
    "ReadMarker": ([], ("IntProperty", 4)),
    "ReadBaseClass": ([], ("ClassProperty", 8)),
    "EchoAnyClass": ([("Input", "ClassProperty", 8)], ("ClassProperty", 8)),
    "EchoTypedClass": ([("Input", "ClassProperty", 8)], ("ClassProperty", 8)),
    "StoreAnyClass": ([("Input", "ClassProperty", 8)], None),
    "StoreTypedClass": ([("Input", "ClassProperty", 8)], None),
}


def check_target(item: dict, name: str, *, host: bool = False) -> dict:
    expected = "Default__" + HOST if host else name
    if (name not in ((HOST,) if host else CLASS_NAMES) or not isinstance(item, dict)
            or item.get("name") != expected or item.get("path") != "/Script/Angelscript." + expected
            or item.get("class") not in ((HOST,) if host else ("Class", "AngelscriptClass", "ASClass"))
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Exact owned class fixture object is not confirmed")
    return item


def check_signature(functions: dict, name: str) -> dict:
    if name not in SIGNATURES or not isinstance(functions, dict):
        raise ProbeError("probe_signature_invalid", "Unknown probe method")
    matches = [item for item in functions.get("items", []) if item.get("name") == name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Probe method was not found uniquely")
    method, expected = matches[0], SIGNATURES[name]
    parameters, size = method.get("parameters"), method.get("parameter_size")
    if not isinstance(parameters, list) or type(size) is not int or not 0 <= size <= 32:
        raise ProbeError("probe_signature_mismatch", "Probe method buffer is not confirmed")
    if any(not isinstance(parameter, dict) for parameter in parameters):
        raise ProbeError("probe_signature_mismatch", "Invalid probe method parameters")
    inputs, output = expected
    actual_inputs = [parameter for parameter in parameters if parameter.get("return") is not True]
    actual_outputs = [parameter for parameter in parameters if parameter.get("return") is True]
    if len(actual_inputs) != len(inputs) or len(actual_outputs) != (output is not None):
        raise ProbeError("probe_signature_mismatch", "Inputs or return do not match")
    for parameter, wanted in zip(actual_inputs, inputs):
        if (parameter.get("name"), parameter.get("type"), parameter.get("element_size")) != wanted:
            raise ProbeError("probe_signature_mismatch", "Argument type does not match")
    if output is not None and (actual_outputs[0].get("type"), actual_outputs[0].get("element_size")) != output:
        raise ProbeError("probe_signature_mismatch", "Return type does not match")
    for parameter in parameters:
        offset, element_size = parameter.get("offset"), parameter.get("element_size")
        if (parameter.get("reference") is True or parameter.get("out") is True and parameter.get("return") is not True
                or type(offset) is not int or type(element_size) is not int or offset < 0 or element_size < 1
                or offset + element_size > 32 or parameter.get("return") is not True and offset + element_size > size):
            raise ProbeError("probe_signature_mismatch", "Bounded parameter layout does not match")
    return method


def check_result(result: dict, expected: dict | None, session_id: str | None = None) -> None:
    if result.get("executed") is not True:
        raise ProbeError("probe_not_executed", "Probe method execution is not confirmed")
    value = result.get("result")
    if expected is None:
        if value is not None:
            raise ProbeError("probe_null_mismatch", "Null class was not returned as null")
    elif (not isinstance(value, dict) or any(value.get(key) != expected.get(key)
          for key in ("handle", "index", "name", "path", "class"))
          or not isinstance(value.get("session_id"), str) or not value["session_id"]
          or session_id is not None and value["session_id"] != session_id):
        raise ProbeError("probe_class_identity", "Class object identity was not preserved")


def snapshot_values(snapshot: dict, host: dict, session_id: str | None = None) -> dict:
    objects = snapshot.get("objects")
    if (snapshot.get("partial") is not False or snapshot.get("truncated") is not False
            or not isinstance(objects, list) or len(objects) != 1
            or objects[0].get("handle") != host["handle"] or objects[0].get("error")
            or session_id is not None and (snapshot.get("session_id") != session_id
                or snapshot.get("source") != "live_native" or snapshot.get("consistency") != "single_tick_sequential"
                or any(objects[0].get(key) != host[key] for key in ("path", "class")))):
        raise ProbeError("probe_snapshot_invalid", "Complete owned-field snapshot is not confirmed")
    result = {}
    for name in PROPERTIES:
        fields = [field for field in objects[0].get("properties", []) if field.get("name") == name]
        if (len(fields) != 1 or fields[0].get("supported") is not True
                or fields[0].get("type") != "ClassProperty" or fields[0].get("error") or "value" not in fields[0]):
            raise ProbeError("probe_snapshot_invalid", "Class property is not confirmed")
        result[name] = fields[0].get("value")
    return result


async def exercise(call) -> dict:
    report = {"checks": [], "cleanup": [], "game_launched": False, "dll_loaded": False}
    host = None
    can_write = False
    watch_id = None
    failure = None
    try:
        status = await call("sn2_native_status", {})
        if not status.get("ready"):
            raise ProbeError("game_not_ready", "Native game queue is not ready")
        session_id = status.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ProbeError("probe_owner_unknown", "Native session owner is not confirmed")
        found = await call("sn2_find_objects", {"query": "Default__" + HOST, "class_query": HOST, "limit": 50})
        exact = [item for item in found.get("items", []) if item.get("name") == "Default__" + HOST]
        if len(exact) != 1:
            raise ProbeError("probe_object_absent", "Owned class CDO was not found uniquely")
        host = check_target(exact[0], HOST, host=True)
        functions = await call("sn2_object_functions", {"handle": host["handle"], "limit": 100})
        for method in SIGNATURES:
            check_signature(functions, method)
        marker = await call("sn2_invoke", {"handle": host["handle"], "function": "ReadMarker", "arguments": {}})
        if marker.get("executed") is not True or type(marker.get("result")) is not int or marker["result"] != 61:
            raise ProbeError("probe_marker_mismatch", "Owned CDO did not return marker 61")
        classes = {}
        for name in CLASS_NAMES:
            found = await call("sn2_find_objects", {"query": name, "class_query": "", "limit": 100})
            exact = [item for item in found.get("items", []) if item.get("name") == name]
            if len(exact) != 1:
                raise ProbeError("probe_class_absent", "Owned class object was not found uniquely")
            classes[name] = check_target(exact[0], name)
        base, derived, other = (classes[name] for name in CLASS_NAMES)
        report["checks"].append("exact_host_marker_classes_signatures")

        async def invoke(method, value=...):
            return await call("sn2_invoke", {"handle": host["handle"], "function": method,
                                             "arguments": {} if value is ... else {"Input": value}})

        initial = await call("sn2_snapshot", {"targets": [{"handle": host["handle"], "properties": PROPERTIES}]})
        if initial.get("session_id") != session_id:
            raise ProbeError("probe_snapshot_owner", "Initial snapshot belongs to a different session")
        for value in snapshot_values(initial, host).values():
            check_result({"executed": True, "result": value}, base, session_id)
        report["checks"].append("initial_class_defaults_match_base_and_session")
        check_result(await invoke("ReadBaseClass"), base, session_id)
        for method in ("EchoAnyClass", "EchoTypedClass"):
            for target in (base, derived):
                check_result(await invoke(method, {"handle": target["handle"]}), target, session_id)
            check_result(await invoke(method, None), None, session_id)
        check_result(await invoke("EchoAnyClass", {"handle": other["handle"]}), other, session_id)
        report["foreign_class"] = await call("sn2_invoke", {"handle": host["handle"], "function": "EchoTypedClass",
                                                             "arguments": {"Input": {"handle": other["handle"]}}},
                                             expected_error="incompatible_class_argument")
        report["ordinary_object"] = await call("sn2_invoke", {"handle": host["handle"], "function": "EchoAnyClass",
                                                               "arguments": {"Input": {"handle": host["handle"]}}},
                                               expected_error="invalid_class_argument")
        report["checks"].append("class_returns_base_derived_null_and_two_pre_execution_rejections")
        can_write = True
        await invoke("StoreAnyClass", None)
        await invoke("StoreTypedClass", None)
        before = await call("sn2_snapshot", {"targets": [{"handle": host["handle"], "properties": PROPERTIES}]})
        if before.get("session_id") != session_id:
            raise ProbeError("probe_snapshot_owner", "Snapshot belongs to a different session")
        if any(value is not None for value in snapshot_values(before, host).values()):
            raise ProbeError("probe_initial_state", "Owned class fields are not normalized to null")
        watch = await call("sn2_watch_start", {"handle": host["handle"], "properties": PROPERTIES,
                                                "duration_ms": 5000, "interval_ms": 50, "capacity": 16})
        watch_id = watch["watch_id"]
        await invoke("StoreAnyClass", {"handle": other["handle"]})
        await invoke("StoreTypedClass", {"handle": derived["handle"]})
        after = await call("sn2_snapshot", {"targets": [{"handle": host["handle"], "properties": PROPERTIES}]})
        if after.get("session_id") != session_id:
            raise ProbeError("probe_snapshot_owner", "Snapshot belongs to a different session")
        values = snapshot_values(after, host)
        for name, target in (("StoredAnyClass", other), ("StoredTypedClass", derived)):
            check_result({"executed": True, "result": values[name]}, target, session_id)
        observed = None
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            events = await call("sn2_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
            if events.get("dropped_events") != 0 or events.get("gap") is True:
                raise ProbeError("probe_watch_loss", "Short probe lost events")
            changed = [event for event in events.get("events", []) if event.get("kind") == "changed"]
            if changed and snapshot_values(changed[-1]["snapshot"], host) == values:
                observed = events
                break
            await asyncio.sleep(0.05)
        if observed is None:
            raise ProbeError("probe_watch_change_absent", "Watch did not confirm class properties")
        await asyncio.sleep(0.2)
        stable = await call("sn2_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
        if stable.get("last_seq") != observed.get("last_seq"):
            raise ProbeError("probe_false_class_change", "Unchanged classes produced false events")
        diff = await call("sn2_compare_snapshots", {"before": before, "after": after, "limit": 100})
        if diff.get("value_changes") != 2:
            raise ProbeError("probe_snapshot_diff", "Comparison did not confirm two class changes")
        report["watch"] = {"changed_events": sum(event.get("kind") == "changed" for event in stable["events"]),
                           "dropped_events": stable["dropped_events"], "last_seq": stable["last_seq"]}
        report["checks"].append("class_properties_watch_stability_and_snapshot_diff")
        report["previous_handle"] = host["handle"]
    except BaseException as error:
        failure = error
        raise
    finally:
        async def finish_cleanup():
            cleanup_errors = []
            if can_write:
                for method in ("StoreAnyClass", "StoreTypedClass"):
                    try:
                        result = await call("sn2_invoke", {"handle": host["handle"], "function": method, "arguments": {"Input": None}})
                        if result.get("executed") is not True:
                            raise ProbeError("probe_cleanup_unconfirmed", "Field cleanup execution is not confirmed")
                        report["cleanup"].append(method)
                    except BaseException as error:
                        cleanup_errors.append(f"{method}: {getattr(error, 'code', 'failed')}")
                try:
                    restored = await call("sn2_snapshot", {"targets": [{"handle": host["handle"], "properties": PROPERTIES}]})
                    if any(value is not None for value in snapshot_values(restored, host, session_id).values()):
                        raise ProbeError("probe_restore_mismatch", "Owned class fields were not restored to null")
                    report["cleanup"].append("restored_null_snapshot")
                except BaseException as error:
                    cleanup_errors.append(f"restore_snapshot: {getattr(error, 'code', 'failed')}")
            if watch_id is not None:
                for method in ("sn2_watch_stop", "sn2_watch_forget"):
                    try:
                        result = await call(method, {"watch_id": watch_id})
                        if (result.get("cleanup_complete") is not True
                                or method == "sn2_watch_forget" and result.get("forgotten") is not True
                                or method == "sn2_watch_stop" and result.get("state") not in {"stopped", "completed", "failed"}):
                            raise ProbeError("probe_cleanup_unconfirmed", "Watch release is not confirmed")
                        report["cleanup"].append(method)
                    except BaseException as error:
                        cleanup_errors.append(f"{method}: {getattr(error, 'code', 'failed')}")
            if cleanup_errors:
                message = "Cleanup is not confirmed: " + "; ".join(cleanup_errors)
                if failure is None:
                    raise ProbeError("probe_cleanup_failed", message)
                failure.add_note(message)
        try:
            await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label='class cleanup')
        except BaseException as cleanup_error:
            if failure is None:
                raise
            failure.add_note("Cleanup completion is not confirmed: " + str(cleanup_error))
    return report


async def run_probe(args) -> dict:
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
        "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            report = await exercise(_caller(client))
            report["server_version"] = initialized.server_info.version
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            await client.initialize()
            call = _caller(client)
            await call("sn2_native_status", {})
            report["new_session_rejection"] = await call("sn2_invoke", {"handle": report["previous_handle"], "function": "ReadMarker", "arguments": {}},
                                                         expected_error="stale_handle")
    report.pop("previous_handle", None)
    report["checks"].append("old_session_handle_rejected_before_execution")
    return report


async def _run_with_signals(args):
    with deferred_sigint():
        return await run_probe(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check only the owned class fixture through ready MCP")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    try:
        report = asyncio.run(_run_with_signals(args))
    except Exception as error:
        cause = _single_error(error)
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause)),
                          "error_type": type(cause).__name__, "notes": getattr(cause, "__notes__", [])}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
