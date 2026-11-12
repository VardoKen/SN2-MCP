"""Accept two compiled standalone mod revisions through the actual MCP surface.

The runner starts/restarts/closes the configured game and loads workspace DLLs.
It does not install files into the game or alter configuration or saves. Inputs,
builds, packages and complete MCP receipts remain in a new acceptance directory.
Run only under applicable operator authority for these game actions.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import shutil
import sys
import time
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class AcceptanceError(Exception):
    """An acceptance criterion was not established by the recorded evidence."""


class ModStateFailure(AcceptanceError):
    def __init__(self, receipt: dict):
        state = mod_status(receipt).get("state")
        super().__init__(f"The modification reached terminal state {state}; inspect its complete runtime receipt")
        self.receipt = receipt


class ToolFailure(AcceptanceError):
    def __init__(self, name: str, receipt: dict, sequence: int):
        super().__init__(f"{name} failed; inspect MCP receipt {sequence:04d}")
        self.name = name
        self.receipt = receipt


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def error_payload(result) -> dict:
    """Retain the complete tool error and extract its structured failure if present."""
    messages = [block.text for block in result.content if block.type == "text"]
    for message in messages:
        start = message.find("{")
        if start >= 0:
            try:
                value = json.loads(message[start:])
            except (ValueError, RecursionError):
                continue
            if isinstance(value, dict):
                return value
    return {"message": "\n".join(messages)}


def mod_status(receipt: dict) -> dict:
    return receipt.get("mod_receipt", {}).get("status", {})


def require_mod_progress(receipt: dict) -> None:
    """Stop at established faults instead of waiting for a known failed state."""
    state = mod_status(receipt).get("state")
    if state in {"faulted", "worker_failed", "effect_unknown", "disabled_unrestored", "restore_outcome_unknown"}:
        raise ModStateFailure(receipt)


def observed_values(receipt: dict) -> list[float]:
    values = []
    for event in receipt.get("events", []):
        if event.get("kind") not in ("baseline", "changed"):
            continue
        for obj in event.get("snapshot", {}).get("objects", []):
            for prop in obj.get("properties", []):
                if prop.get("name") == "RenderOpacity" and isinstance(prop.get("value"), (int, float)):
                    values.append(prop["value"])
    return values


def close_number(actual, expected: float) -> bool:
    return (not isinstance(actual, bool) and isinstance(actual, (int, float))
            and math.isfinite(actual) and math.isclose(actual, expected, abs_tol=0.000001))


def controlled_source(text: str) -> str:
    return text + "\n#error SN2_MCP_ACCEPTANCE_CONTROLLED_ERROR\n"


def revision_b_source(text: str) -> str:
    old = "constexpr float Opacity = 0.5f;"
    require(text.count(old) == 1, "The editable template no longer has one known revision-A opacity")
    return text.replace(old, "constexpr float Opacity = 0.75f;")


async def run(args) -> int:
    runtime = args.runtime_root.resolve(strict=True)
    game = args.game_root.resolve(strict=True)
    acceptance_parent = runtime / "artifacts" / "acceptance"
    acceptance_parent.mkdir(parents=True, exist_ok=True)
    output = (args.output.resolve() if args.output is not None else
              acceptance_parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]))
    require(output.is_relative_to(acceptance_parent), "Acceptance output must be within runtime artifacts/acceptance")
    require(not output.exists(), "Choose a new acceptance directory")
    output.mkdir()
    workspace = output / "workspace"
    workspace.mkdir()
    summary = {"kind": "compiled_widget_opacity_mod_acceptance", "state": "running",
               "started_utc": datetime.now(timezone.utc).isoformat(), "runtime_root": str(runtime),
               "workspace_root": str(workspace), "game_root": str(game), "automatic_mutation_retries": False,
               "installed_game_files_changed": False, "revisions": [], "cleanup": [],
               "remaining_processes_confirmed": False, "connection_closed": False}
    sequence = 0
    process = None
    current_package = None
    current_mod = None
    disable_attempted = False
    watch = None
    failure = None
    launch_attempted = False
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "sn2_mcp.server",
        "--workspace-root", str(workspace), "--runtime-root", str(runtime), "--game-root", str(game)],
        cwd=str(runtime))

    async with stdio_client(parameters) as (incoming, outgoing), ClientSession(incoming, outgoing) as client:
        await client.initialize()

        async def tool(name: str, arguments: dict, *, rejection: str | None = None) -> dict:
            nonlocal sequence
            sequence += 1
            value = await client.call_tool(name, arguments, read_timeout_seconds=args.build_timeout_seconds + 120)
            receipt = {"request": {"tool": name, "arguments": arguments}, "response": value.model_dump(mode="json")}
            write_json(output / f"{sequence:04d}.json", receipt)
            if rejection is not None:
                require(value.is_error, f"The intentional {name} validation failure was accepted")
                error = error_payload(value)
                require(error.get("code") == rejection or rejection in json.dumps(error, ensure_ascii=False),
                        f"The failure was not attributable to {rejection}")
                return error
            if value.is_error:
                raise ToolFailure(name, error_payload(value), sequence)
            require(isinstance(value.structured_content, dict), f"{name} did not return structured evidence")
            return value.structured_content

        async def wait_native() -> dict:
            deadline = time.monotonic() + args.readiness_timeout_seconds
            while True:
                value = await tool("sn2_native_status", {"target_process": process})
                if value.get("ready") is True:
                    require(value.get("executor_ready") is True and value.get("pump", {}).get("state") == "recent_tick",
                            "The bridge did not confirm its own recent Tick")
                    return value
                require(time.monotonic() < deadline, "The bridge Tick/reflection checkpoint did not become ready")
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        async def wait_native_transport() -> dict:
            """Read until async DLL bootstrap opens transport; do not replay loading."""
            deadline = time.monotonic() + args.readiness_timeout_seconds
            while True:
                try:
                    return await tool("sn2_native_status", {"target_process": process})
                except ToolFailure as error:
                    require(error.receipt.get("code") == "native_unavailable"
                            and time.monotonic() < deadline,
                            "The loaded bridge did not establish its native transport")
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        async def wait_mod(command: dict, *, disabled: bool = False) -> dict:
            deadline = time.monotonic() + args.readiness_timeout_seconds
            request_id = command.get("request_id")
            while True:
                value = await tool("sn2_mod_status", {"target_process": process,
                    "package_path": current_package["relative_path"],
                    "expected_manifest_sha256": current_package["manifest_sha256"],
                    "request_id": request_id, "timeout_seconds": args.control_timeout_seconds})
                status = mod_status(value)
                require_mod_progress(value)
                command_state = value.get("mod_receipt", {}).get("command", {}).get("state")
                require(command_state not in ("failed", "outcome_unknown"),
                        "The mod command failed or has an unresolved effect; no mutation is replayed")
                ready = (status.get("state") == "disabled" and status.get("restoration_confirmed") is True
                         if disabled else status.get("initialized") is True and status.get("active") is True)
                if ready:
                    require(status.get("source_revision") == current_package["source_revision"],
                            "The initialized mod revision differs from the verified package")
                    require(status.get("process_id") == process["pid"]
                            and str(status.get("process_creation_filetime")) == process["creation_time"],
                            "The modification belongs to another process")
                    return value
                require(time.monotonic() < deadline, "The mod did not reach the requested observable state")
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        async def select_widget() -> tuple[dict, dict]:
            deadline = time.monotonic() + args.readiness_timeout_seconds
            while True:
                candidates = []
                offset = 0
                while True:
                    found = await tool("sn2_find_objects", {"query": "TextLabel", "class_query": "TextBlock",
                                                              "limit": 200, "offset": offset})
                    candidates.extend(item for item in found["items"]
                        if "/Engine/Transient." in item.get("path", "")
                        and "WBP_StartScreen_PressStart_C_" in item["path"] and ".ContinueBtn." in item["path"])
                    if not found.get("truncated", False):
                        break
                    next_offset = found.get("next_offset")
                    require(isinstance(next_offset, int) and next_offset > offset, "The object scan did not advance")
                    offset = next_offset
                require(len(candidates) <= 1, "The live start-screen Continue label is ambiguous")
                if candidates:
                    target = candidates[0]
                    functions = await tool("sn2_object_functions", {"handle": target["handle"],
                        "function_query": "RenderOpacity", "limit": 100})
                    signatures = {item["name"]: item for item in functions["items"]}
                    for name in ("GetRenderOpacity", "SetRenderOpacity"):
                        require(signatures.get(name, {}).get("signature_status") == "complete",
                                "The current widget does not expose the required complete signature")
                    return target, signatures
                require(time.monotonic() < deadline, "No unique live start-screen Continue label became available")
                await asyncio.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

        async def getter(target: dict, signatures: dict) -> float:
            value = await tool("sn2_invoke", {"handle": target["handle"], "function": "GetRenderOpacity",
                "arguments": {}, "expected_signature_token": signatures["GetRenderOpacity"]["signature_token"]})
            require(value.get("executed") is True and value.get("process_event_returned") is True,
                    "The independent bridge getter did not return")
            require(isinstance(value.get("result"), (int, float)), "The getter returned no numerical opacity")
            return value["result"]

        async def snapshot(target: dict) -> dict:
            return await tool("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": ["RenderOpacity"]}]})

        async def capture_watch() -> dict:
            events = []
            offset = 0
            while True:
                value = await tool("sn2_watch_read", {"watch_id": watch["watch_id"], "after_seq": offset, "limit": 100})
                events.extend(value.get("events", []))
                if not value.get("truncated", False):
                    return {**value, "events": events}
                next_seq = value.get("cursor")
                require(isinstance(next_seq, int) and next_seq > offset, "The watch page did not advance")
                offset = next_seq

        async def disable_and_observe(target: dict, signatures: dict, before: dict, baseline: float) -> dict:
            nonlocal disable_attempted, current_mod, watch
            disable_attempted = True
            command = await tool("sn2_mod_disable", {"target_process": process,
                "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"],
                "timeout_seconds": args.control_timeout_seconds})
            disabled = await wait_mod(command, disabled=True)
            require(close_number(await getter(target, signatures), baseline), "The independent getter did not confirm restoration")
            after = await snapshot(target)
            comparison = await tool("sn2_snapshot_compare", {"before": before, "after": after})
            require(comparison.get("comparable") is True and comparison.get("value_changes") == 1
                    and comparison.get("unknown_count") == 0, "The snapshot did not establish restoration")
            await asyncio.sleep(0.1)
            events = await capture_watch()
            values = observed_values(events)
            require(values and close_number(values[-1], baseline), "The Tick watch did not observe restoration")
            await tool("sn2_watch_stop", {"watch_id": watch["watch_id"]})
            await tool("sn2_watch_forget", {"watch_id": watch["watch_id"]})
            watch = None
            current_mod = None
            return {"disable": disabled, "restored_snapshot": after, "snapshot_comparison": comparison,
                    "watch": events, "restoration_confirmed": True}

        try:
            summary["frontend"] = await tool("sn2_status", {})
            summary["catalog"] = await tool("sn2_capabilities", {})
            initial = await tool("sn2_target_processes", {})
            summary["initial_processes"] = initial
            expected_pid = getattr(args, "expected_pid", None)
            expected_created = getattr(args, "expected_creation_filetime", None)
            require((expected_pid is None) == (expected_created is None),
                    "Expected PID and creation FILETIME must be supplied together")
            if expected_pid is not None:
                selected = [item for item in initial["processes"]
                            if item["pid"] == expected_pid and item["creation_time"] == expected_created
                            and Path(item["executable"]).name.casefold() == "subnautica2-win64-shipping.exe"]
                require(len(selected) == 1, "The explicitly selected existing Shipping process identity changed")
                process = selected[0]
                launch_attempted = True
                summary["game_already_running"] = True
                summary["selected_existing_process"] = process
            else:
                require(not initial["processes"], "Select the exact existing game identity or finish it before acceptance")
                summary["game_already_running"] = False
            if args.bridge_build_id:
                bridge_build = await tool("sn2_build_status", {"build_id": args.bridge_build_id})
            else:
                summary["bridge_recipe"] = await tool("sn2_build_recipe", {"project_path": "native", "run_tests": True})
                bridge_build = await tool("sn2_build", {"project_path": "native", "run_tests": True,
                    "timeout_seconds": args.build_timeout_seconds, "parallel": args.parallel})
            require(bridge_build.get("state") == "succeeded" and bridge_build.get("exact_source_compiled") is True,
                    "The bridge build did not establish exact-source successful compilation")
            bridge_artifacts = [item for item in bridge_build.get("artifacts", [])
                                if Path(item.get("path", "")).name == "SN2_MCP.dll"]
            require(len(bridge_artifacts) == 1, "The build does not identify one native bridge DLL")
            bridge = bridge_artifacts[0]
            summary["bridge_build"] = bridge_build
            project_path = "mods/compiled_opacity"
            source_path = project_path + "/src/mod.cpp"
            summary["project"] = await tool("sn2_create_mod_project", {"project_path": project_path, "mod_id": "compiled_opacity"})
            validation = await tool("sn2_validate_mod_project", {"project_path": project_path})
            require(validation.get("valid") is True, "The generated project did not validate")
            source_a = await tool("sn2_read_source_exact", {"file_path": source_path})
            revision_b_source(source_a["text"])
            summary["revision_a_source"] = source_a
            recipe_a = await tool("sn2_build_recipe", {"project_path": project_path, "target": "sn2_mod", "run_tests": False})
            build_a = await tool("sn2_build", {"project_path": project_path, "target": "sn2_mod", "run_tests": False,
                "timeout_seconds": args.build_timeout_seconds, "parallel": args.parallel})
            require(build_a.get("state") == "succeeded" and build_a.get("exact_source_compiled") is True,
                    "Revision A did not compile its captured source bytes")
            summary["revision_a_build_status"] = await tool("sn2_build_status", {"build_id": build_a["build_id"]})
            package_a = await tool("sn2_package_artifact", {"build_receipt_path": build_a["receipt_path"],
                                                             "destination": "artifacts/revision-a"})
            current_package = {**await tool("sn2_verify_artifact_package", {"package_path": "artifacts/revision-a"}),
                               "relative_path": "artifacts/revision-a"}
            require(current_package.get("compiled") is True and current_package.get("valid") is True,
                    "Revision A is not a verified compiled package")
            corrupt = workspace / "artifacts" / "corrupted-revision-a"
            shutil.copytree(Path(current_package["package_root"]), corrupt)
            corrupt_dll = corrupt / current_package["artifact"]["package_path"]
            with corrupt_dll.open("ab") as stream:
                stream.write(b"SN2_MCP_ACCEPTANCE_CORRUPTION")
            summary["package_corruption_rejection"] = await tool("sn2_verify_artifact_package",
                {"package_path": "artifacts/corrupted-revision-a"}, rejection="package_changed")
            summary["corruption_fixture"] = {"path": str(corrupt), "purpose": "retained negative package-integrity evidence"}
            if process is None:
                launch_attempted = True
                started = await tool("sn2_development_start", {"discovery_timeout_seconds": args.readiness_timeout_seconds})
                process = started["process"]
                summary["start"] = started
            summary["bridge_load"] = await tool("sn2_native_load", {"target_process": process,
                "dll_path": bridge["path"], "expected_dll_sha256": bridge["sha256"]})
            native_initial = await wait_native_transport()
            await tool("sn2_native_enable", {"target_process": process,
                                              "expected_native_session_id": native_initial["session_id"]})
            summary["bridge_ready_a"] = await wait_native()
            target_a, signatures_a = await select_widget()
            baseline_a = await getter(target_a, signatures_a)
            require(close_number(baseline_a, 1.0), "The acceptance widget baseline differs from the selected neutral start-screen contract")
            before_a = await snapshot(target_a)
            watch = await tool("sn2_watch_start", {"handle": target_a["handle"], "properties": ["RenderOpacity"],
                "duration_ms": math.ceil((args.build_timeout_seconds * 3 + args.readiness_timeout_seconds * 4) * 1000),
                "interval_ms": 20, "capacity": 64})
            current_mod = {"attempted": True}
            load_a = await tool("sn2_mod_load", {"target_process": process, "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
            current_mod = load_a
            active_a = await wait_mod(load_a)
            require(close_number(await getter(target_a, signatures_a), 0.5), "The independent bridge getter did not observe compiled revision A")
            after_a = await snapshot(target_a)
            compare_a = await tool("sn2_snapshot_compare", {"before": before_a, "after": after_a})
            require(compare_a.get("comparable") is True and compare_a.get("value_changes") == 1
                    and compare_a.get("unknown_count") == 0, "The snapshot did not establish compiled revision A's effect")
            summary["revisions"].append({"revision": "A", "opacity": 0.5, "source": source_a, "recipe": recipe_a,
                "build": build_a, "package": package_a, "process": process, "active": active_a,
                "target": target_a, "snapshot_comparison": compare_a})

            broken = await tool("sn2_write_source", {"file_path": source_path, "content": controlled_source(source_a["text"]),
                                                      "expected_sha256": source_a["sha256"]})
            failed_build = await tool("sn2_build", {"project_path": project_path, "target": "sn2_mod", "run_tests": False,
                "timeout_seconds": args.build_timeout_seconds, "parallel": args.parallel})
            require(failed_build.get("state") != "succeeded" and failed_build.get("exact_source_compiled") is False,
                    "The controlled C++ error was represented as successful exact-source compilation")
            diagnostics = await tool("sn2_compile_diagnostics", {"build_id": failed_build["build_id"]})
            attributed = [item for report in diagnostics.get("reports", []) for item in report.get("diagnostics", [])
                if item.get("severity") == "error" and item.get("source_sha256") == broken["sha256"]
                and item.get("source_matched") is True and "SN2_MCP_ACCEPTANCE_CONTROLLED_ERROR" in item.get("message", "")]
            require(attributed, "The actual C++ compiler error was not correlated to the exact broken source bytes")
            summary["controlled_build_failure"] = {"source": broken, "build": failed_build, "diagnostics": diagnostics,
                "package_rejection": await tool("sn2_package_artifact", {"build_receipt_path": failed_build["receipt_path"],
                    "destination": "artifacts/failed-revision"}, rejection="build_not_succeeded")}
            require(close_number(await getter(target_a, signatures_a), 0.5), "The failed build replaced or changed the active revision")
            repaired = await tool("sn2_write_source", {"file_path": source_path, "content": revision_b_source(source_a["text"]),
                                                       "expected_sha256": broken["sha256"]})
            exact_b = await tool("sn2_read_source_exact", {"file_path": source_path})
            require(exact_b["sha256"] == repaired["sha256"] and exact_b["sha256"] != source_a["sha256"],
                    "Revision B is not a distinct exact authored revision")
            build_b = await tool("sn2_build", {"project_path": project_path, "target": "sn2_mod", "run_tests": False,
                "timeout_seconds": args.build_timeout_seconds, "parallel": args.parallel})
            require(build_b.get("state") == "succeeded" and build_b.get("exact_source_compiled") is True,
                    "The repaired revision B did not compile its captured source bytes")
            package_b = await tool("sn2_package_artifact", {"build_receipt_path": build_b["receipt_path"],
                                                             "destination": "artifacts/revision-b"})
            verified_b = await tool("sn2_verify_artifact_package", {"package_path": "artifacts/revision-b"})
            require(package_a["artifact"]["sha256"] != package_b["artifact"]["sha256"]
                    and package_a["source_revision"] != package_b["source_revision"],
                    "The compiled revision identities are not distinct")
            summary["revisions"][0]["cleanup"] = await disable_and_observe(target_a, signatures_a, after_a, baseline_a)
            current_package = {**verified_b, "relative_path": "artifacts/revision-b"}
            disable_attempted = False
            previous_process = process
            reload = await tool("sn2_mod_reload", {"target_process": process,
                "package_path": current_package["relative_path"], "expected_manifest_sha256": current_package["manifest_sha256"],
                "bridge_dll_path": bridge["path"], "expected_bridge_sha256": bridge["sha256"],
                "discovery_timeout_seconds": args.readiness_timeout_seconds,
                "readiness_timeout_seconds": args.readiness_timeout_seconds, "timeout_seconds": args.control_timeout_seconds})
            process = reload["process"]
            require(process != previous_process and process["creation_time"] != previous_process["creation_time"],
                    "Revision iteration did not establish a fresh process identity")
            require(reload.get("hot_reload") is False and reload.get("iteration") == "controlled_process_restart",
                    "The lifecycle receipt does not identify the supported restart iteration")
            current_mod = reload["mod"]
            active_b = await wait_mod(current_mod)
            summary["bridge_ready_b"] = await wait_native()
            target_b, signatures_b = await select_widget()
            require(close_number(await getter(target_b, signatures_b), 0.75), "The independent bridge getter did not observe compiled revision B")
            baseline_b = mod_status(active_b).get("baseline_opacity")
            require(close_number(baseline_b, baseline_a), "The fresh process modification did not capture the same neutral baseline")
            after_b = await snapshot(target_b)
            watch = await tool("sn2_watch_start", {"handle": target_b["handle"], "properties": ["RenderOpacity"],
                "duration_ms": math.ceil(args.readiness_timeout_seconds * 1000 * 4), "interval_ms": 20, "capacity": 64})
            summary["revisions"].append({"revision": "B", "opacity": 0.75, "source": exact_b,
                "build": build_b, "package": package_b, "process": process, "active": active_b,
                "target": target_b, "restart_iteration": reload})
            summary["revisions"][1]["cleanup"] = await disable_and_observe(target_b, signatures_b, after_b, baseline_b)
            require(observed_values(summary["revisions"][0]["cleanup"]["watch"])[:2] == [baseline_a, 0.5],
                    "The Tick watch did not independently observe compiled revision A activation")
            require(close_number(observed_values(summary["revisions"][1]["cleanup"]["watch"])[0], 0.75),
                    "The fresh Tick watch did not observe compiled revision B")
            summary["state"] = "passed"
        except BaseException as error:
            failure = error
            summary["state"] = "failed"
            summary["failure"] = {"type": type(error).__name__, "message": str(error)}
            if isinstance(error, ToolFailure):
                summary["failure"]["tool_error"] = error.receipt
            elif isinstance(error, ModStateFailure):
                summary["failure"]["mod_state_receipt"] = error.receipt
        finally:
            if current_mod is not None and current_package is not None and process is not None and not disable_attempted:
                try:
                    observed = await tool("sn2_mod_status", {"target_process": process,
                        "package_path": current_package["relative_path"],
                        "expected_manifest_sha256": current_package["manifest_sha256"],
                        "request_id": current_mod.get("request_id"), "timeout_seconds": args.control_timeout_seconds})
                    summary["cleanup"].append({"mod_state_before_cleanup": observed})
                    disable_attempted = True
                    command = await tool("sn2_mod_disable", {"target_process": process,
                        "package_path": current_package["relative_path"],
                        "expected_manifest_sha256": current_package["manifest_sha256"],
                        "timeout_seconds": args.control_timeout_seconds})
                    summary["cleanup"].append({"mod_disable": await wait_mod(command, disabled=True)})
                except BaseException as error:
                    summary["cleanup"].append({"mod_disable_unconfirmed": str(error)})
            if watch is not None:
                for name in ("sn2_watch_stop", "sn2_watch_forget"):
                    try:
                        summary["cleanup"].append({name: await tool(name, {"watch_id": watch["watch_id"]})})
                    except BaseException as error:
                        summary["cleanup"].append({name + "_unconfirmed": str(error)})
            if launch_attempted:
                try:
                    actual = await tool("sn2_target_processes", {})
                    inner = [item for item in actual["processes"]
                             if Path(item["executable"]).name.casefold() == "subnautica2-win64-shipping.exe"]
                    require(len(inner) <= 1, "Cleanup found ambiguous Shipping processes; exact termination was not selected")
                    bootstrap = [item for item in actual["processes"]
                                 if Path(item["executable"]).name.casefold() == "subnautica2.exe"]
                    require(len(bootstrap) <= 1, "Cleanup found ambiguous bootstrap processes; exact termination was not selected")
                    selected_cleanup = inner[0] if inner else bootstrap[0] if bootstrap else None
                    if selected_cleanup is not None:
                        summary["cleanup"].append({"game_stop": await tool("sn2_development_stop", {"target_process": selected_cleanup})})
                except BaseException as error:
                    summary["cleanup"].append({"game_stop_unconfirmed": str(error)})
                    if isinstance(error, ToolFailure):
                        summary["cleanup"][-1]["tool_error"] = error.receipt
                    summary["state"] = "failed"
                finally:
                    # A failed helper response does not establish whether the
                    # selected process survived. Observe it without replaying
                    # termination and retain both the error and actual state.
                    try:
                        final = await tool("sn2_target_processes", {})
                        summary["final_processes"] = final
                        summary["remaining_processes_confirmed"] = not final["processes"]
                        require(summary["remaining_processes_confirmed"], "The test game session did not fully exit")
                    except BaseException as error:
                        summary["cleanup"].append({"final_process_state_unconfirmed": str(error)})
                        summary["state"] = "failed"
            summary["receipt_count"] = sequence
            summary["finished_utc"] = datetime.now(timezone.utc).isoformat()
            write_json(output / "summary.json", summary)
    summary["connection_closed"] = True
    write_json(output / "summary.json", summary)
    print(json.dumps({"state": summary["state"], "output": str(output), "receipts": sequence,
                      "game_exit_confirmed": summary["remaining_processes_confirmed"]}, ensure_ascii=False))
    if isinstance(failure, (KeyboardInterrupt, asyncio.CancelledError)):
        raise failure
    return 0 if summary["state"] == "passed" else 1


def positive(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("Timeout must be a positive finite number")
    return number


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="New directory within runtime artifacts/acceptance")
    parser.add_argument("--bridge-build-id", help="Reuse this actual successful MCP bridge-build receipt")
    parser.add_argument("--expected-pid", type=int, help="Explicitly reuse this existing Shipping PID")
    parser.add_argument("--expected-creation-filetime", help="Exact creation FILETIME of the existing selected PID")
    parser.add_argument("--build-timeout-seconds", type=positive, default=900)
    parser.add_argument("--readiness-timeout-seconds", type=positive, default=180)
    parser.add_argument("--control-timeout-seconds", type=positive, default=30)
    parser.add_argument("--parallel", type=int, default=4)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
