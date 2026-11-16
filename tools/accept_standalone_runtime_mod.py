"""Prepare, author, or independently observe the standalone GameInstance example.

Run with an installed consumer frontend, a supplied runtime folder and a separate
writable workspace. ``author`` requires an already ready game executor; it reads
the actual GameEngine frame-rate baseline and uses registered source/authoring/package
tools for A/error/B. It never starts/stops the game or installs a package.

``observe`` only reads the already running game. Launch it after an ordinary
fresh start with the selected package, then attach an observer bridge separately.
The observer report proves the current effect and authored state; startup module
inventory, pre-attachment observation and natural shutdown evidence are separate
required acceptance records. No event is manually invoked by this tool.
The world-delta check requires an unpaused world with time dilation 1 and the
ordinary engine timestep. Record those conditions in the runtime acceptance;
GetWorldDeltaSeconds includes world time dilation.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import re
import sys
import sysconfig
from typing import Any


PARENT = "/Script/Subnautica2.SN2GameInstance"
KISMET = "/Script/Engine.KismetSystemLibrary"
GAMEPLAY_STATICS = "/Script/Engine.GameplayStatics"
ASSET_NAME = "BP_Startup"


class AcceptanceError(RuntimeError):
    pass


def json_text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n"


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json_text(value))


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while part := stream.read(1024 * 1024):
            digest.update(part)
    return digest.hexdigest()


def number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise AcceptanceError(f"{label} must be a finite number")
    return float(value)


def edge(source: str, output: str, target: str, input_: str) -> dict:
    return {"from": {"node": source, "pin": output}, "to": {"node": target, "pin": input_}}


def node(identifier: str, kind: str, **values: Any) -> dict:
    return {"id": identifier, "kind": kind, **values}


def parameter(name: str, type_: str, default: str | None = None) -> dict:
    return {"name": name, "type": type_, **({"default": default} if default is not None else {})}


def specification(plugin: str, baseline: float, baseline_enabled: bool, applied: float, revision: str,
                  *, invalid: bool = False) -> dict:
    """Return real synchronous override graphs, never a source replay script."""
    nodes = [
        node("init", "event", function="ReceiveInit"),
        node("init_parent", "call_parent"),
        node("init_guard", "branch"),
        node("init_initialized", "variable_get", variable="Initialized"),
        node("init_self", "self"),
        node("init_engine", "call_function", **{"class": KISMET}, function="GetOuterObject"),
        node("apply_rate", "call_function", **{"class": KISMET}, function="SetDoublePropertyByName",
             defaults={"PropertyName": "FixedFrameRate", "Value": repr(applied)}),
        node("enable_fixed_rate", "call_function", **{"class": KISMET}, function="SetBoolPropertyByName",
             defaults={"PropertyName": "bUseFixedFrameRate", "Value": "true"}),
        node("read_count", "variable_get", variable="InitializationCount"),
        node("increment", "call_function", **{"class": "/Script/Engine.KismetMathLibrary"},
             function="Add_DoubleDouble", defaults={"B": "1.0"}),
        node("write_count", "variable_set", variable="InitializationCount"),
        node("write_initialized", "variable_set", variable="Initialized", defaults={"Initialized": "true"}),
        node("write_applied", "variable_set", variable="LastAppliedFrameRate", defaults={"LastAppliedFrameRate": repr(applied)}),
        node("shutdown", "event", function="ReceiveShutdown"),
        node("shutdown_guard", "branch"),
        node("shutdown_initialized", "variable_get", variable="Initialized"),
        node("shutdown_self", "self"),
        node("shutdown_engine", "call_function", **{"class": KISMET}, function="GetOuterObject"),
        node("baseline_rate", "variable_get", variable="BaselineFrameRate"),
        node("baseline_enabled", "variable_get", variable="BaselineUseFixedFrameRate"),
        node("restore_rate", "call_function", **{"class": KISMET}, function="SetDoublePropertyByName",
             defaults={"PropertyName": "FixedFrameRate"}),
        node("restore_enabled", "call_function", **{"class": KISMET}, function="SetBoolPropertyByName",
             defaults={"PropertyName": "bUseFixedFrameRate"}),
        node("read_shutdown_count", "variable_get", variable="ShutdownCount"),
        node("increment_shutdown", "call_function", **{"class": "/Script/Engine.KismetMathLibrary"},
             function="Add_DoubleDouble", defaults={"B": "1.0"}),
        node("write_shutdown_count", "variable_set", variable="ShutdownCount"),
        node("clear_initialized", "variable_set", variable="Initialized", defaults={"Initialized": "false"}),
        node("shutdown_parent", "call_parent"),
    ]
    connections = [
        edge("init", "then", "init_parent", "execute"),
        edge("init_parent", "then", "init_guard", "execute"),
        edge("init_initialized", "Initialized", "init_guard", "Condition"),
        edge("init_guard", "else", "apply_rate", "execute"),
        edge("init_self", "self", "init_engine", "Object"),
        edge("init_engine", "ReturnValue", "apply_rate", "Object"),
        edge("init_engine", "ReturnValue", "enable_fixed_rate", "Object"),
        edge("apply_rate", "then", "enable_fixed_rate", "execute"),
        edge("enable_fixed_rate", "then", "write_count", "execute"),
        edge("read_count", "InitializationCount", "increment", "A"),
        edge("increment", "ReturnValue", "write_count", "InitializationCount"),
        edge("write_count", "then", "write_initialized", "execute"),
        edge("write_initialized", "then", "write_applied", "execute"),
        edge("shutdown", "then", "shutdown_guard", "execute"),
        edge("shutdown_initialized", "Initialized", "shutdown_guard", "Condition"),
        edge("shutdown_guard", "then", "restore_rate", "execute"),
        edge("shutdown_guard", "else", "shutdown_parent", "execute"),
        edge("shutdown_self", "self", "shutdown_engine", "Object"),
        edge("shutdown_engine", "ReturnValue", "restore_rate", "Object"),
        edge("shutdown_engine", "ReturnValue", "restore_enabled", "Object"),
        edge("baseline_rate", "BaselineFrameRate", "restore_rate", "Value"),
        edge("baseline_enabled", "BaselineUseFixedFrameRate", "restore_enabled", "Value"),
        edge("restore_rate", "then", "restore_enabled", "execute"),
        edge("restore_enabled", "then", "write_shutdown_count", "execute"),
        edge("read_shutdown_count", "ShutdownCount", "increment_shutdown", "A"),
        edge("increment_shutdown", "ReturnValue", "write_shutdown_count", "ShutdownCount"),
        edge("write_shutdown_count", "then", "clear_initialized", "execute"),
        edge("clear_initialized", "then", "shutdown_parent", "execute"),
    ]
    if invalid:
        # Actual setter accepts Value, not MissingFrameRate. The error is deliberate
        # and must be diagnosed before publishing a new class or working pointer.
        setter = next(item for item in nodes if item["id"] == "apply_rate")
        setter["defaults"]["MissingFrameRate"] = setter["defaults"].pop("Value")
    diagnostics = {
        "name": "ReadStartupState", "inputs": [],
        "outputs": [parameter("Initialized", "bool"), parameter("Count", "double"), parameter("Shutdowns", "double"),
                    parameter("BaselineFrameRate", "double"), parameter("BaselineEnabled", "bool"),
                    parameter("AppliedFrameRate", "double"), parameter("Revision", "string")],
        "nodes": [node("entry", "function_entry"), node("result", "function_result"),
                  node("initialized", "variable_get", variable="Initialized"),
                  node("count", "variable_get", variable="InitializationCount"),
                  node("shutdown_count", "variable_get", variable="ShutdownCount"),
                  node("baseline_rate", "variable_get", variable="BaselineFrameRate"),
                  node("baseline_enabled", "variable_get", variable="BaselineUseFixedFrameRate"),
                  node("applied", "variable_get", variable="LastAppliedFrameRate"),
                  node("revision", "variable_get", variable="Revision")],
        "connections": [edge("entry", "then", "result", "execute"),
                        edge("initialized", "Initialized", "result", "Initialized"),
                        edge("count", "InitializationCount", "result", "Count"),
                        edge("shutdown_count", "ShutdownCount", "result", "Shutdowns"),
                        edge("baseline_rate", "BaselineFrameRate", "result", "BaselineFrameRate"),
                        edge("baseline_enabled", "BaselineUseFixedFrameRate", "result", "BaselineEnabled"),
                        edge("applied", "LastAppliedFrameRate", "result", "AppliedFrameRate"),
                        edge("revision", "Revision", "result", "Revision")],
    }
    return {"schema": 1, "plugin_id": plugin, "assets": [{
        "kind": "blueprint", "path": f"/{plugin}/{ASSET_NAME}.{ASSET_NAME}", "parent_class": PARENT,
        "variables": [parameter("Initialized", "bool", "false"), parameter("InitializationCount", "double", "0.0"),
                      parameter("ShutdownCount", "double", "0.0"),
                      parameter("BaselineFrameRate", "double", repr(baseline)),
                      parameter("BaselineUseFixedFrameRate", "bool", "true" if baseline_enabled else "false"),
                      parameter("LastAppliedFrameRate", "double", "0.0"),
                      parameter("Revision", "string", revision)],
        "nodes": nodes, "connections": connections, "functions": [diagnostics],
    }]}


def engine_config(plugin: str) -> str:
    return f"[/Script/EngineSettings.GameMapsSettings]\nGameInstanceClass=/{plugin}/{ASSET_NAME}.{ASSET_NAME}_C\n"


def property_value(snapshot: dict, handle: str, name: str) -> Any:
    if snapshot.get("partial") or snapshot.get("truncated"):
        raise AcceptanceError("The selected snapshot is incomplete")
    objects = [item for item in snapshot.get("objects", []) if item.get("handle") == handle]
    if len(objects) != 1:
        raise AcceptanceError("The selected object was not fully snapshotted")
    properties = [item for item in objects[0].get("properties", [])
                  if item.get("name") == name and item.get("supported") is True]
    if len(properties) != 1:
        raise AcceptanceError(f"No exact supported snapshot property: {name}")
    return properties[0].get("value")


def package_inventory(package: dict) -> dict[str, str]:
    root = Path(package["package_root"])
    return {path.relative_to(root).as_posix(): file_hash(path) for path in root.rglob("*") if path.is_file()}


def error_object(texts: list[str]) -> dict:
    for text in texts:
        try:
            value = json.loads(text[text.index("{"):])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


async def run(args: argparse.Namespace) -> dict:
    if args.use_repository_frontend:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import sn2_mcp
    from sn2_mcp import server as server_module
    from mcp.server.mcpserver.exceptions import ToolError

    workspace = args.workspace_root.resolve(strict=True)
    runtime = args.runtime_root.resolve(strict=True)
    selected_target = json.loads(args.target_process.read_text(encoding="utf-8-sig"))
    if not isinstance(selected_target, dict):
        raise AcceptanceError("target-process must contain the exact current process descriptor")
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    imported = [Path(sn2_mcp.__file__).resolve(strict=True), Path(server_module.__file__).resolve(strict=True)]
    installed_roots = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
    if not args.use_repository_frontend and not all(any(path.is_relative_to(root) for root in installed_roots)
                                                 for path in imported):
        raise AcceptanceError("Consumer checks require installed site-packages origins")
    version = importlib.metadata.version("sn2-mcp")
    if version != sn2_mcp.__version__:
        raise AcceptanceError("Imported frontend and installed distribution versions differ")
    consumer_modkit, consumer_engine = workspace / "UnavailableConsumerModkit", workspace / "UnavailableConsumerEngine"
    if consumer_modkit.exists() or consumer_engine.exists():
        raise AcceptanceError("Configured consumer Modkit and Engine paths must be absent")
    server = server_module.create_server(workspace, args.native_port, args.game_root,
                                         runtime_root=runtime, modkit_root=consumer_modkit, engine_root=consumer_engine)
    report = {"kind": "sn2_standalone_runtime_example", "schema": 1, "stage": args.stage,
              "state": "running", "plugin_id": args.plugin_id, "frontend_version": version,
              "frontend_origins": [str(path) for path in imported],
              "selected_target": selected_target,
              "installed_frontend_required": not args.use_repository_frontend,
              "configured_authoring_environment": {"modkit": str(consumer_modkit), "engine": str(consumer_engine),
                                                    "both_absent": True, "tools_called": False},
              "game_launch_requested": False, "installation_requested": False, "event_invocation_requested": False,
              "editor_invoked": False, "checks": {}}
    sequence = 0

    async def call(tool: str, arguments: dict | None = None, *, expect_error: bool = False) -> dict:
        nonlocal sequence
        sequence += 1
        try:
            response = await server.call_tool(tool, arguments or {})
            texts = [item.text for item in response.content if item.type == "text"]
            value = response.structured_content if response.structured_content is not None else error_object(texts)
            is_error = response.is_error
        except ToolError as error:
            texts, is_error = [str(error)], True
            value = error_object(texts)
        write_json(output / f"mcp-{sequence:04d}.json", {"tool": tool, "arguments": arguments or {},
                                                        "is_error": is_error, "result": value, "text": texts})
        if not isinstance(value, dict):
            raise AcceptanceError(f"No structured response from {tool}")
        if bool(is_error) != expect_error:
            raise AcceptanceError(f"Unexpected result from {tool}: {value.get('code', 'inspect_receipt')}")
        return value

    async def exact_object(path: str) -> dict:
        result = await call("sn2_find_objects", {"exact_path": path, "limit": 8, "timeout_seconds": args.timeout_seconds})
        items = [item for item in result.get("items", []) if item.get("path") == path]
        if len(items) != 1 or not result.get("scan_complete"):
            raise AcceptanceError(f"No unique loaded object: {path}")
        return items[0]

    async def snapshot(object_: dict, names: list[str]) -> dict:
        return await call("sn2_snapshot", {"targets": [{"handle": object_["handle"], "properties": names}],
                                           "timeout_seconds": args.timeout_seconds})

    async def game_engine() -> dict:
        result = await call("sn2_find_objects", {"class_query": "GameEngine", "limit": 16,
                                                 "timeout_seconds": args.timeout_seconds})
        items = [item for item in result.get("items", [])
                 if item.get("class") == "GameEngine" and not (item.get("flags", 0) & 0x10)]
        if not result.get("scan_complete") or len(items) != 1:
            raise AcceptanceError("No unique live GameEngine owner")
        return items[0]

    async def function(object_: dict, name: str) -> dict:
        result = await call("sn2_object_functions", {"handle": object_["handle"], "function_query": name,
                                                     "limit": 64, "timeout_seconds": args.timeout_seconds})
        items = [item for item in result.get("items", []) if item.get("name") == name]
        if not result.get("scan_complete") or len(items) != 1 or items[0].get("signature_status") != "complete":
            raise AcceptanceError(f"No exact usable reflected function: {name}")
        return items[0]

    server_lifetimes = AsyncExitStack()
    await server_lifetimes.enter_async_context(server._lowlevel_server.lifespan(server._lowlevel_server))
    try:
        native = await call("sn2_native_status", {"target_process": selected_target})
        if native.get("ready") is not True:
            raise AcceptanceError("The already running native executor is not ready")
        report["checks"]["native"] = native
        engine = await game_engine()
        engine_before = await snapshot(engine, ["FixedFrameRate", "bUseFixedFrameRate"])
        baseline = number(property_value(engine_before, engine["handle"], "FixedFrameRate"), "FixedFrameRate")
        baseline_enabled = property_value(engine_before, engine["handle"], "bUseFixedFrameRate")
        if type(baseline_enabled) is not bool:
            raise AcceptanceError("bUseFixedFrameRate must be a reflected boolean")
        if args.stage == "author":
            if baseline_enabled and any(math.isclose(baseline, value, abs_tol=1e-6) for value in (args.frame_rate_a, args.frame_rate_b)):
                raise AcceptanceError("Choose A and B effects distinct from the active baseline frame rate")
            absent = await call("sn2_find_objects", {"exact_path": f"/{args.plugin_id}/{ASSET_NAME}.{ASSET_NAME}_C",
                                                      "limit": 8, "timeout_seconds": args.timeout_seconds})
            if not absent.get("scan_complete") or absent.get("items"):
                raise AcceptanceError("The example canonical class already exists before authoring")
            report["checks"]["baseline"] = {"object": engine, "snapshot": engine_before,
                                             "frame_rate": baseline, "enabled": baseline_enabled}
            project = f"projects/{args.plugin_id}"
            spec_path = f"{project}/authoring.json"
            await call("sn2_create_mod_project", {"project_path": project, "mod_id": args.plugin_id,
                                                   "title": "Standalone runtime startup example",
                                                   "entry_class": f"/{args.plugin_id}/{ASSET_NAME}.{ASSET_NAME}_C"})
            await call("sn2_create_source_directory", {"directory_path": project + "/Config"})
            await call("sn2_write_source", {"file_path": project + "/Config/Engine.ini", "content": engine_config(args.plugin_id)})
            records = {}
            preceding_package = None
            preceding_inventory = None
            for revision, value, invalid in (("A", args.frame_rate_a, False), ("error", args.frame_rate_b, True), ("B", args.frame_rate_b, False)):
                source = specification(args.plugin_id, baseline, baseline_enabled, value, revision, invalid=invalid)
                write_json(output / f"specification-{revision}.json", source)
                current_path = workspace / spec_path
                arguments = {"file_path": spec_path, "content": json_text(source)}
                if current_path.exists():
                    arguments["expected_sha256"] = file_hash(current_path)
                await call("sn2_write_source", arguments)
                compiled = await call("sn2_runtime_compile_blueprint", {"project_path": project, "spec_path": spec_path,
                                                                        "timeout_seconds": args.timeout_seconds}, expect_error=invalid)
                if invalid:
                    if compiled.get("code") != "pin_unavailable":
                        raise AcceptanceError("Deliberate invalid setter pin was not diagnosed precisely")
                    working = await call("sn2_runtime_authoring_status", {"working": True})
                    if working.get("authoring_id") != records["A"]["compile"]["authoring_id"]:
                        raise AcceptanceError("The invalid candidate replaced working revision A")
                    if package_inventory(preceding_package) != preceding_inventory:
                        raise AcceptanceError("The invalid candidate changed the preceding package")
                    records[revision] = {"rejection": compiled, "working": working, "preceding_package_unchanged": True}
                    continue
                if not compiled.get("compiled") or not compiled.get("runtime_published"):
                    raise AcceptanceError("The candidate class was not published")
                saved = await call("sn2_runtime_save_package", {"authoring_id": compiled["authoring_id"],
                                                                "timeout_seconds": args.timeout_seconds})
                if not saved.get("persisted") or not saved.get("containerized"):
                    raise AcceptanceError("No complete native package containers")
                package = await call("sn2_package_runtime_artifact", {"runtime_build_receipt": saved["receipt_path"],
                                                                      "destination": f"packages/{args.plugin_id}-{revision}"})
                configs = package.get("configs", [])
                if not any(item.get("path") == f"{args.plugin_id}/Config/Engine.ini" for item in configs):
                    raise AcceptanceError("The startup Engine.ini was not included in verified package")
                preceding_package, preceding_inventory = package, package_inventory(package)
                records[revision] = {"compile": compiled, "save": saved, "package": package,
                                     "package_inventory": preceding_inventory, "expected_frame_rate": value}
            engine_after = await snapshot(engine, ["FixedFrameRate", "bUseFixedFrameRate"])
            if (not math.isclose(number(property_value(engine_after, engine["handle"], "FixedFrameRate"), "FixedFrameRate"),
                                 baseline, abs_tol=1e-6)
                or property_value(engine_after, engine["handle"], "bUseFixedFrameRate") is not baseline_enabled):
                raise AcceptanceError("Compilation changed engine frame-rate settings before natural initialization")
            report["checks"]["revisions"] = records
            report["checks"]["authoring_has_no_startup_effect"] = engine_after
            report["state"] = "packages_prepared"
            report["remaining_acceptance"] = ["Ordinary fresh startup of A and B at authorized stopped-game install boundaries",
                                              "Effect observation before observer attachment and startup component inventory",
                                              "Natural ReceiveShutdown restoration and process exit", "Revision retirement and GC controls"]
        else:
            result = await call("sn2_find_objects", {"class_query": ASSET_NAME + "_C",
                                                     "limit": 16, "timeout_seconds": args.timeout_seconds})
            instances = [item for item in result.get("items", [])
                         if item.get("class") == ASSET_NAME + "_C"
                         and "Default__" not in item.get("path", "")]
            if not result.get("scan_complete") or len(instances) != 1:
                raise AcceptanceError("No unique live authored GameInstance")
            instance = instances[0]
            generated = await exact_object(f"/{args.plugin_id}/{ASSET_NAME}.{ASSET_NAME}_C")
            implementations = {}
            for event in ("ReceiveInit", "ReceiveShutdown"):
                implementations[event] = await exact_object(f"/{args.plugin_id}/{ASSET_NAME}.{ASSET_NAME}_C.{event}")
            owner = engine
            ownership = await snapshot(owner, ["GameInstance"])
            owned = property_value(ownership, owner["handle"], "GameInstance")
            if not isinstance(owned, dict) or owned.get("handle") != instance["handle"]:
                raise AcceptanceError("The authored object is not the engine-owned GameInstance")
            fields = ["Initialized", "InitializationCount", "ShutdownCount", "BaselineFrameRate",
                      "BaselineUseFixedFrameRate", "LastAppliedFrameRate", "Revision"]
            state = await snapshot(instance, fields)
            values = {name: property_value(state, instance["handle"], name) for name in fields}
            expected = args.frame_rate_a if args.revision == "A" else args.frame_rate_b
            if (values["Initialized"] is not True or number(values["InitializationCount"], "InitializationCount") != 1.0
                or number(values["ShutdownCount"], "ShutdownCount") != 0.0
                or values["Revision"] != args.revision
                or not math.isclose(number(values["LastAppliedFrameRate"], "LastAppliedFrameRate"), expected, abs_tol=1e-6)
                or not math.isclose(baseline, expected, abs_tol=1e-6) or baseline_enabled is not True):
                raise AcceptanceError("The naturally initialized state or actual game effect differs")
            gameplay = await exact_object("/Script/Engine.Default__GameplayStatics")
            delta_contract = await function(gameplay, "GetWorldDeltaSeconds")
            deltas = []
            for _ in range(3):
                delta = await call("sn2_invoke", {"handle": gameplay["handle"], "function": "GetWorldDeltaSeconds",
                                                   "arguments": {"WorldContextObject": {"handle": instance["handle"]}},
                                                   "expected_signature_token": delta_contract["signature_token"],
                                                   "timeout_seconds": args.timeout_seconds})
                if delta.get("executed") is not True or delta.get("process_event_returned") is not True:
                    raise AcceptanceError("Independent world delta getter did not return")
                measured = number(delta.get("result"), "GetWorldDeltaSeconds")
                if not math.isclose(measured, 1.0 / expected, rel_tol=1e-5, abs_tol=1e-6):
                    raise AcceptanceError("World delta differs from the authored fixed frame rate")
                deltas.append(delta)
            report["checks"]["effect"] = {"engine": engine, "snapshot": engine_before, "expected_frame_rate": expected,
                                           "world_delta_samples": deltas, "expected_delta_seconds": 1.0 / expected}
            report["checks"]["effect"]["delta_conditions"] = {
                "required": "Unpaused world, time dilation 1, ordinary engine timestep",
                "verified_by_tool": False,
                "method": "GameplayStatics.GetWorldDeltaSeconds with engine-owned GameInstance context"}
            report["checks"]["authored_instance"] = {"object": instance, "snapshot": state, "values": values}
            report["checks"]["game_owned_startup"] = {"class": generated, "implementations": implementations,
                                                       "engine": owner, "ownership": ownership}
            report["state"] = "effect_observed"
            report["observation_limit"] = "Read-only post-start observer; does not prove pre-attachment module absence or natural shutdown"
        return report
    except BaseException as error:
        report["state"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            await server_lifetimes.aclose()
        finally:
            write_json(output / "summary.json", report)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("author", "observe"))
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--target-process", type=Path, required=True, help="JSON exact current PID/creation/executable descriptor from sn2_target_processes")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plugin-id", default="SN2RuntimeStartup")
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--frame-rate-a", type=float, default=30.0)
    parser.add_argument("--frame-rate-b", type=float, default=45.0)
    parser.add_argument("--revision", choices=("A", "B"), default="B")
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--use-repository-frontend", action="store_true", help="Developer check only; consumer check requires installed origins")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.plugin_id):
        parser.error("plugin-id must be an ASCII plugin identifier")
    try:
        if number(args.frame_rate_a, "frame-rate-a") <= 0 or number(args.frame_rate_b, "frame-rate-b") <= 0:
            raise AcceptanceError("Frame rates must be positive")
        if args.frame_rate_a == args.frame_rate_b:
            raise AcceptanceError("A and B require distinct authored frame-rate literals")
        asyncio.run(run(args))
    except (AcceptanceError, OSError, ValueError) as error:
        print(f"Standalone runtime example failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
