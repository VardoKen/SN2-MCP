"""Observe the owned PostInitialize through MCP without manually calling the callback."""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_object_mcp import ProbeError, _caller, _single_error
from probe_cleanup import deferred_sigint, settle_task

HOST = "G1RMCPWorldPostInitializeProbe"
COUNT = "PostInitializeCount"
READ_METHOD = "ReadPostInitializeCount"
MARKER = 111


async def exercise(call, *, wait_seconds=3, clock=time.monotonic, sleep=asyncio.sleep):
    # A separate instance of the existing read-only check preserves its boundaries,
    # without changing the global state of the IngameTool probe or its fixture.
    spec = importlib.util.spec_from_file_location("_sn2_world_lifecycle_observation", REPOSITORY_ROOT / "tools" / "probe_lifecycle_mcp.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.TOOL = HOST
    implementation.CLASSES = {HOST: {"marker": MARKER, "fields": {COUNT: READ_METHOD}}}
    implementation.READ_METHODS = {"ReadMarker", READ_METHOD}
    report = await implementation.exercise(call, wait_seconds=wait_seconds, clock=clock, sleep=sleep,
                                           positive_counter=COUNT)
    world = report.pop("tool")
    if world["status"] == "automatic_initialize_confirmed":
        world["status"] = "automatic_post_initialize_confirmed"
    report["world"] = world
    if report["status"] != "completed":
        report["status"] = "not_tested_not_triggered"
    report["callback"] = "PostInitialize"
    report["gameplay_ready_confirmed"] = False
    report["limitations"] = ["A positive counter proves only execution of the owned subsystem PostInitialize",
                             "A missing instance or zero counter in the main menu is not considered confirmed lifecycle",
                             "Gameplay start, the full lifecycle, and saves are not checked"]
    return report


async def exercise_reload_watch(call, *, wait_seconds=120, clock=time.monotonic, sleep=asyncio.sleep):
    """Two reloads of the owned package: baseline, then watch invalidation."""
    report, watch_id = {"status": "started", "watch_trial": {"status": "not_started"}}, None

    async def find_live():
        found = await call("sn2_find_objects", {"query": HOST, "class_query": HOST, "limit": 100})
        return [item for item in found["items"] if item.get("class") == HOST
                and item.get("name", "").startswith(HOST) and item.get("path", "").startswith("/Game/")]

    async def invoke(target, name):
        listing = await call("sn2_object_functions", {"handle": target["handle"], "function_query": name, "limit": 20})
        method = next(item for item in listing["items"] if item["name"] == name)
        result = await call("sn2_invoke", {"handle": target["handle"], "function": name,
                            "arguments": {}, "expected_signature_token": method["signature_token"]})
        return {"signature": method, "result": result}

    async def observe(target):
        return {"target": target, "map": await invoke(target, "GetCurrentMapName"),
                "loading_type": await invoke(target, "GetCurrentLoadingScreenType"),
                "marker": await invoke(target, "ReadMarker"),
                "counter": await call("sn2_object_properties", {"handle": target["handle"], "property_names": [COUNT]})}

    def initialized(observed):
        return (observed["marker"]["result"].get("result") == MARKER
                and any(item.get("name") == COUNT and item.get("supported") is True
                        and item.get("value") == 1 for item in observed["counter"].get("items", [])))

    async def reload_trial(target, native_before):
        trial = {"before": await observe(target), "travel_observations": []}
        if not initialized(trial["before"]):
            trial["status"] = "initialization_not_confirmed"
            return trial, None
        trial["reload"] = await invoke(target, "ReloadCurrentMap")
        deadline, new_target = clock() + wait_seconds, None
        while clock() < deadline:
            try:
                new_target = next((item for item in await find_live() if item["handle"] != target["handle"]), None)
                if new_target is not None:
                    break
            except Exception as error:
                trial["travel_observations"].append({"code": getattr(error, "code", None), "message": str(error)})
            await sleep(.5)
        trial["after"] = await observe(new_target) if new_target is not None else {}
        try:
            trial["old_handle"] = await call("sn2_object_properties", {"handle": target["handle"], "property_names": [COUNT]})
        except Exception as error:
            trial["old_handle"] = {"error": {"code": getattr(error, "code", None), "message": str(error)}}
        trial["native_after"] = await call("sn2_native_status", {})
        trial["checks"] = {"reload_executed": trial["reload"]["result"].get("executed") is True,
                           "same_game": trial["native_after"].get("process_id") == native_before.get("process_id"),
                           "same_native_session": trial["native_after"].get("session_id") == native_before.get("session_id"),
                           "new_instance_initialized": new_target is not None and initialized(trial["after"]),
                           "same_map": new_target is not None and trial["after"]["map"]["result"].get("result")
                           == trial["before"]["map"]["result"].get("result"),
                           "black_screen": new_target is not None and trial["after"]["loading_type"]["result"].get("result") == 1
                           and trial["after"]["loading_type"]["result"].get("return_enum", {}).get("path") == "/Script/G1R.ELoadingScreenType"
                           and trial["after"]["loading_type"]["result"].get("return_enum", {}).get("value_name") == "ELoadingScreenType::BlackScreen",
                           "old_handle_stale": trial["old_handle"].get("error", {}).get("code") == "stale_handle"}
        trial["status"] = "completed" if all(trial["checks"].values()) else "observed_different"
        return trial, new_target

    try:
        report["bridge_status"] = await call("sn2_status", {})
        report["capabilities"] = await call("sn2_capabilities", {})
        native = report["native_before"] = await call("sn2_native_status", {})
        baseline, target = await reload_trial((await find_live())[0], native)
        report["baseline_without_watch"] = baseline
        if baseline["status"] != "completed":
            report["status"] = "baseline_failed"
            return report
        watched = report["watch_trial"] = {"status": "started", "target": target}
        watched["watch_started"] = await call("sn2_watch_start", {"handle": target["handle"], "properties": [COUNT],
                                                               "duration_ms": 180000, "interval_ms": 50, "capacity": 32})
        watch_id = watched["watch_started"]["watch_id"]
        watched["watch_before"] = await call("sn2_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
        watched["baseline_confirmed"] = (watched["watch_started"].get("state") == "running"
            and watched["watch_before"].get("state") == "running"
            and any(event.get("kind") == "baseline" and event.get("snapshot", {}).get("process_id") == native.get("process_id")
                    and event.get("snapshot", {}).get("session_id") == native.get("session_id")
                    and any(item.get("handle") == target["handle"]
                            and any(field.get("name") == COUNT and field.get("value") == 1 and field.get("supported") is True
                                    for field in item.get("properties", []))
                            for item in event.get("snapshot", {}).get("objects", []))
                    for event in watched["watch_before"].get("events", [])))
        if not watched["baseline_confirmed"]:
            report["status"] = "watch_baseline_not_confirmed"
            return report
        trial, _ = await reload_trial(target, baseline["native_after"])
        watched["reload_trial"] = trial
        deadline = clock() + wait_seconds
        while True:
            watched["watch_after"] = await call("sn2_watch_read", {"watch_id": watch_id, "after_seq": 0, "limit": 100})
            if watched["watch_after"].get("state") != "running" or clock() >= deadline:
                break
            await sleep(.05)
        terminal = watched["watch_after"]
        watched["watch_invalidated"] = (terminal.get("state") == "failed"
            and terminal.get("reason") == "object_or_property_invalidated"
            and terminal.get("cleanup_complete") is True)
        report["status"] = "completed" if trial["status"] == "completed" and watched["watch_invalidated"] else "observed_different"
    except Exception as error:
        report.update(status="error", error={"code": getattr(error, "code", None), "message": str(error)})
    finally:
        if watch_id is not None:
            async def cleanup():
                cleaned = report["watch_trial"]["watch_cleanup"] = {}
                for action in ("stop", "forget"):
                    try:
                        cleaned[action] = await call("sn2_watch_" + action, {"watch_id": watch_id})
                    except Exception as error:
                        cleaned[action] = {"error": {"code": getattr(error, "code", None), "message": str(error)}}
                return (cleaned["stop"].get("cleanup_complete") is True
                        and cleaned["forget"].get("cleanup_complete") is True and cleaned["forget"].get("forgotten") is True)
            confirmed = await settle_task(asyncio.create_task(cleanup()), 45, "world watch cleanup")
            report["watch_trial"]["watch_cleanup_confirmed"] = confirmed
            if report["status"] == "completed" and not confirmed:
                report["status"] = "observed_different"
    return report


@asynccontextmanager
async def connect_client(args):
    if getattr(args, "transport", "http") == "http":
        async with streamable_http_client(getattr(args, "mcp_url", "http://127.0.0.1:27124/mcp")) as connection:
            async with ClientSession(connection[0], connection[1]) as client:
                yield client
    else:
        parameters = StdioServerParameters(command=sys.executable, args=["-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
            "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                yield client


async def run_probe(args):
    with deferred_sigint():
        async with connect_client(args) as client:
            initialized = await client.initialize()
            call = _caller(client)
            if getattr(args, "reload_and_watch", False):
                await call("sn2_status", {})
                await call("sn2_capabilities", {})
                await call("sn2_session_acquire", {"reason": "Owned PostInitialize check: baseline reload and watch invalidation"})
                try:
                    report = await exercise_reload_watch(call, wait_seconds=args.wait_seconds)
                finally:
                    released = await settle_task(asyncio.create_task(call("sn2_session_release", {})), 45, "world lifecycle ownership release")
                    if released.get("released") is not True:
                        raise ProbeError("ownership_release_unconfirmed", "The lifecycle probe claim was not released")
            else:
                report = await exercise(call, wait_seconds=args.wait_seconds)
            report["server_version"] = initialized.server_info.version
            return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe the owned PostInitialize without loading the game world")
    parser.add_argument("--transport", choices=("http", "stdio"), default="http")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:27124/mcp")
    parser.add_argument("--reload-and-watch", action="store_true", help="Reload the current map twice and check the owned package watch")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    parser.add_argument("--wait-seconds", type=float, default=None)
    args = parser.parse_args()
    if args.wait_seconds is None:
        args.wait_seconds = 120 if args.reload_and_watch else 3
    try:
        report = asyncio.run(run_probe(args))
    except Exception as error:
        cause = _single_error(error)
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause))}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if not args.reload_and_watch or report.get("status") == "completed" else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
