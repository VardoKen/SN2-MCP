"""Observe automatic lifecycle of the owned fixture in the main menu through MCP."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
import time

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_object_mcp import ProbeError, _caller, _single_error
from probe_cleanup import deferred_sigint

TOOL = "G1RMCPIngameLifecycleProbe"
WORLD = "G1RMCPWorldLifecycleProbe"
CLASSES = {TOOL: {"marker": 91, "fields": {"InitializeCount": "ReadInitializeCount"}}}
READ_METHODS = {"ReadMarker", "ReadInitializeCount"}


def check_target(item: dict, class_name: str, *, cdo: bool) -> dict:
    if (class_name not in CLASSES or not isinstance(item, dict) or item.get("class") != class_name
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")
            or not isinstance(item.get("name"), str) or not isinstance(item.get("path"), str)):
        raise ProbeError("unsafe_probe_target", "The owned lifecycle object was not confirmed")
    if cdo:
        if item["name"] != "Default__" + class_name or item["path"] != "/Script/Angelscript.Default__" + class_name:
            raise ProbeError("unsafe_probe_target", "The exact lifecycle fixture CDO was not confirmed")
    elif (item["name"].startswith("Default__") or "Default__" in item["path"]
            or item["path"].startswith("/Script/") or not item["name"].startswith(class_name)
            or not item["path"].endswith(item["name"])):
        raise ProbeError("unsafe_probe_target", "A template or unverified name must not be considered a live instance")
    return item


def check_signature(functions: dict, method_name: str) -> None:
    if method_name not in READ_METHODS:
        raise ProbeError("unsafe_probe_method", "Lifecycle callbacks must not be called manually")
    if functions.get("truncated") is not False or functions.get("scan_complete") is not True:
        raise ProbeError("probe_signature_incomplete", "The function catalog was not confirmed as complete")
    matches = [item for item in functions.get("items", []) if item.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "The lifecycle reader method was not uniquely found")
    method = matches[0]
    params, size = method.get("parameters"), method.get("parameter_size")
    if not isinstance(params, list) or len(params) != 1 or not isinstance(params[0], dict):
        raise ProbeError("probe_signature_mismatch", "The lifecycle method must have only a scalar return")
    result, offset = params[0], params[0].get("offset")
    if (result.get("return") is not True or result.get("reference") is not False or result.get("type") != "IntProperty"
            or type(result.get("array_dim")) is not int or result["array_dim"] != 1 or method.get("parameters_truncated") is not False
            or result.get("element_size") != 4 or type(size) is not int or not 0 <= size <= 32
            or type(offset) is not int or offset < 0 or max(size, offset + 4) > 32):
        raise ProbeError("probe_signature_mismatch", "Zero input and IntProperty return were not confirmed")


def snapshot_counts(snapshot: dict, target: dict, session_id: str, fields: dict) -> dict:
    objects = snapshot.get("objects")
    if (snapshot.get("session_id") != session_id or snapshot.get("source") != "live_native"
            or snapshot.get("consistency") != "single_tick_sequential" or snapshot.get("partial") is not False
            or snapshot.get("truncated") is not False or not isinstance(objects, list) or len(objects) != 1
            or objects[0].get("partial") is not False or objects[0].get("truncated") is not False
            or any(objects[0].get(key) != target[key] for key in ("handle", "path", "class")) or objects[0].get("error")):
        raise ProbeError("probe_snapshot_invalid", "The current session lifecycle snapshot was not confirmed")
    counts = {}
    for name in fields:
        matches = [field for field in objects[0].get("properties", []) if field.get("name") == name]
        if (len(matches) != 1 or matches[0].get("supported") is not True or matches[0].get("type") != "IntProperty"
                or matches[0].get("error") or "value" not in matches[0]
                or type(matches[0]["value"]) is not int or not 0 <= matches[0]["value"] <= 2**31 - 1):
            raise ProbeError("probe_counter_invalid", "The lifecycle counter was not confirmed as a nonnegative integer")
        counts[name] = matches[0]["value"]
    return counts


async def exercise(call, *, wait_seconds: float = 3, clock=time.monotonic, sleep=asyncio.sleep,
                   positive_counter: str = "InitializeCount") -> dict:
    if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or not 0 <= wait_seconds <= 10:
        raise ProbeError("invalid_wait", "Lifecycle waiting is limited to ten seconds")
    status = await call("sn2_native_status", {})
    session_id = status.get("session_id")
    if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
        raise ProbeError("game_not_ready", "The ready queue and current owner were not confirmed")
    verified = set()
    prior_counts = {}

    async def read_target(target: dict, class_name: str) -> dict:
        config = CLASSES[class_name]
        identity = (session_id, class_name, target["path"], target["handle"])
        if identity not in verified:
            functions = await call("sn2_object_functions", {"handle": target["handle"], "limit": 100})
            for method in ("ReadMarker", *config["fields"].values()):
                check_signature(functions, method)
            marker = await call("sn2_invoke", {"handle": target["handle"], "function": "ReadMarker", "arguments": {}})
            if marker.get("executed") is not True or type(marker.get("result")) is not int or marker["result"] != config["marker"]:
                raise ProbeError("probe_marker_mismatch", "The owned lifecycle class marker was not confirmed")
            verified.add(identity)
        snapshot = await call("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": list(config["fields"])}]})
        counts = snapshot_counts(snapshot, target, session_id, config["fields"])
        for field, method in config["fields"].items():
            returned = await call("sn2_invoke", {"handle": target["handle"], "function": method, "arguments": {}})
            if (returned.get("executed") is not True or type(returned.get("result")) is not int
                    or not counts[field] <= returned["result"] <= 2**31 - 1):
                raise ProbeError("probe_counter_mismatch", "The reader method decreased the counter or returned an invalid type")
            if returned["result"] != counts[field]:
                # Callback may run between the snapshot Tick and the reader Tick.
                changed = await call("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": list(config["fields"])}]})
                latest = snapshot_counts(changed, target, session_id, config["fields"])
                if any(latest[name] < counts[name] for name in counts) or latest[field] < returned["result"]:
                    raise ProbeError("probe_counter_mismatch", "The repeated snapshot did not confirm a counter increase")
                counts.update(latest)
        prior = prior_counts.get(identity)
        if prior is not None and any(counts[name] < prior[name] for name in counts):
            raise ProbeError("probe_counter_decreased", "The owned instance counter decreased between checks")
        prior_counts[identity] = dict(counts)
        return counts

    async def collect(class_name):
        found = await call("sn2_find_objects", {"query": class_name, "class_query": class_name, "limit": 100})
        if found.get("truncated") is not False:
            raise ProbeError("probe_search_truncated", "The lifecycle instance search is incomplete")
        exact_cdo = [item for item in found.get("items", []) if item.get("name") == "Default__" + class_name]
        if len(exact_cdo) != 1:
            raise ProbeError("probe_cdo_absent", "The lifecycle fixture CDO was not uniquely found")
        cdo = check_target(exact_cdo[0], class_name, cdo=True)
        cdo_counts = await read_target(cdo, class_name)
        if any(cdo_counts.values()):
            raise ProbeError("probe_cdo_mutated", "CDO counters do not equal the original zero values")
        live = []
        for item in found.get("items", []):
            if item.get("name", "").startswith("Default__"):
                continue
            target = check_target(item, class_name, cdo=False)
            live.append({"path": target["path"], "counts": await read_target(target, class_name)})
        return {"cdo_counts": cdo_counts, "instances": live}

    deadline = clock() + wait_seconds
    samples = 0
    tool = None
    while True:
        tool = await collect(TOOL)
        samples += 1
        if any(item["counts"][positive_counter] > 0 for item in tool["instances"]):
            tool["status"] = "automatic_initialize_confirmed"
            break
        if clock() >= deadline:
            tool["status"] = "not_triggered" if tool["instances"] else "live_instance_not_observed"
            break
        await sleep(min(0.1, max(0, deadline - clock())))
    world = {"status": "not_tested", "reason": "world_callback_fixture_omitted_after_runtime_signature_rejection",
             "ready_for_play_confirmed": False}
    return {"status": "completed" if tool["status"] == "automatic_initialize_confirmed" else "not_triggered",
            "tool": tool, "world": world, "samples": samples, "game_launched": False, "dll_loaded": False,
            "world_loaded": False, "callbacks_invoked_manually": False,
            "limitations": ["IngameTool Initialize confirms only the tool lifecycle",
                            "WorldSubsystem lifecycle is not checked after the runtime compiler rejected the callback fixture"]}


async def run_probe(args):
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
        "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    with deferred_sigint():
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                initialized = await client.initialize()
                report = await exercise(_caller(client), wait_seconds=args.wait_seconds)
                report["server_version"] = initialized.server_info.version
                return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Observe the owned lifecycle without manually calling callbacks")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    parser.add_argument("--wait-seconds", type=float, default=3)
    args = parser.parse_args()
    try:
        report = asyncio.run(run_probe(args))
    except Exception as error:
        cause = _single_error(error)
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause))}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
