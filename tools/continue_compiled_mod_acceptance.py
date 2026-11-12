"""Continue a verified compiled-mod acceptance stopped by pre-entry Confirm binding.

Reuse immutable source/build/package evidence from the selected prior run. Check
the exact existing game and disabled revision A, explicitly reactivate A, restore
it, then perform revision-B restart iteration and close the checked test session.
This tool never treats an unknown previous mutation as permission to replay it.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import uuid

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from accept_compiled_mod import AcceptanceError, ModStateFailure, ToolFailure, close_number, error_payload, mod_status, observed_values, positive, require, require_mod_progress, write_json


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def checked_file(path: Path, boundary: Path) -> bytes:
    selected = path.resolve(strict=True)
    require(selected.is_relative_to(boundary), "Prior evidence file leaves its checked workspace")
    return selected.read_bytes()


def receipt_inventory(root: Path) -> list[dict]:
    return [{"path": file.name, "bytes": len(raw), "sha256": digest(raw)}
            for file in sorted(root.glob("[0-9][0-9][0-9][0-9].json"))
            for raw in [checked_file(file, root)]]


def validate_confirm_failure(summary: dict) -> None:
    require(summary.get("kind") == "compiled_widget_opacity_mod_acceptance" and summary.get("state") == "failed",
            "The prior run is not a failed compiled-mod acceptance")
    failure = summary.get("failure", {})
    require(failure.get("type") == "ToolFailure" and "sn2_mod_reload" in failure.get("message", ""),
            "This continuation only accepts the recorded pre-entry reload failure")
    error = failure.get("tool_error", {})
    require(error.get("code") == "tool_output_decode_failed", "The previous failure requires separate investigation")
    receipt = error.get("receipt", {})
    require(receipt.get("helper_completed") is True and receipt.get("output_complete") is True,
            "The previous helper completion is unconfirmed")
    try:
        stderr = base64.b64decode(receipt.get("stderr_base64", ""), validate=True)
    except ValueError:
        raise AcceptanceError("The prior raw helper error is invalid") from None
    require(b"Confirm" in stderr and b"CannotConvertArgument" in stderr and b"SwitchParameter" in stderr
            and b"stop_sn2_development.ps1" in stderr,
            "The prior error does not establish the selected pre-parameter-binding failure")


def load_prior(runtime: Path, prior: Path) -> dict:
    """Verify actual retained inputs, outputs and report bytes before carrying evidence."""
    prior = prior.resolve(strict=True)
    require(prior.is_relative_to(runtime / "artifacts/acceptance"), "Select prior evidence within runtime acceptance artifacts")
    raw = checked_file(prior / "summary.json", prior)
    summary = json.loads(raw.decode("utf-8"))
    validate_confirm_failure(summary)
    workspace = Path(summary["workspace_root"]).resolve(strict=True)
    require(workspace.is_relative_to(prior), "The prior source workspace leaves its acceptance run")
    from sn2_mcp.mod_projects import verify_artifact_package
    packages = {}
    for revision, relative in (("A", "artifacts/revision-a"), ("B", "artifacts/revision-b")):
        packages[revision] = {**verify_artifact_package(workspace / relative), "relative_path": relative}
    require(packages["A"]["source_revision"] != packages["B"]["source_revision"]
            and packages["A"]["artifact"]["sha256"] != packages["B"]["artifact"]["sha256"],
            "The prior compiled packages are not distinct revisions")
    revisions = summary.get("revisions", [])
    require(len(revisions) == 1 and revisions[0].get("revision") == "A"
            and revisions[0].get("cleanup", {}).get("restoration_confirmed") is True,
            "Prior revision A did not confirm restoration before the failed restart")
    source_a = revisions[0]["source"]
    require(digest(source_a["text"].encode("utf-8")) == source_a["sha256"], "Prior revision-A source receipt bytes changed")
    source_b = json.loads(checked_file(prior / "0034.json", prior).decode("utf-8"))["response"]["structured_content"]
    require(digest(source_b["text"].encode("utf-8")) == source_b["sha256"] and source_a["sha256"] != source_b["sha256"],
            "Prior exact authored revisions are not valid distinct bytes")
    builds = {}
    for revision in ("A", "B"):
        package = packages[revision]
        package_receipt = json.loads(checked_file(Path(package["package_root"]) / "build-receipt.json", workspace).decode("utf-8"))
        path = Path(package_receipt["receipt_path"])
        current = json.loads(checked_file(path, runtime).decode("utf-8"))
        require(current == package_receipt, "The actual build receipt differs from the immutable packaged receipt")
        require(current.get("state") == "succeeded" and current.get("exact_source_compiled") is True,
                "The prior mod build does not establish exact-source success")
        staged = Path(current["staged_source_root"]).resolve(strict=True)
        for item in current["recipe"]["inputs"]:
            content = checked_file(staged / item["path"], runtime)
            require(len(content) == item["bytes"] and digest(content) == item["sha256"], "A prior staged build input changed")
        for artifact in current.get("artifacts", []):
            if artifact.get("available") is False:
                continue
            content = checked_file(Path(artifact["path"]), runtime)
            require(len(content) == artifact["bytes"] and digest(content) == artifact["sha256"], "A prior build artifact changed")
        builds[revision] = current
        exact_source = next(item for item in current["recipe"]["inputs"] if item["path"] == "project/src/mod.cpp")
        require(exact_source["sha256"] == (source_a if revision == "A" else source_b)["sha256"],
                "The compiled project source differs from the authored revision")
    controlled = summary["controlled_build_failure"]
    broken = controlled["build"]
    current_broken = json.loads(checked_file(Path(broken["receipt_path"]), runtime).decode("utf-8"))
    require(current_broken == broken and broken.get("state") != "succeeded" and broken.get("exact_source_compiled") is False,
            "The prior controlled compiler failure changed or became a successful build")
    broken_source = controlled["source"]
    broken_staged = Path(broken["staged_source_root"]).resolve(strict=True)
    exact_broken = next(item for item in broken["recipe"]["inputs"] if item["path"] == "project/src/mod.cpp")
    broken_bytes = checked_file(broken_staged / exact_broken["path"], runtime)
    require(len(broken_bytes) == exact_broken["bytes"] and digest(broken_bytes) == exact_broken["sha256"]
            and exact_broken["sha256"] == broken_source["sha256"]
            and b"#error SN2_MCP_ACCEPTANCE_CONTROLLED_ERROR" in broken_bytes,
            "The actual controlled-error source no longer matches its compiler evidence")
    errors = [item for report in controlled["diagnostics"]["reports"] for item in report.get("diagnostics", [])
              if item.get("code") == "C1189" and item.get("source_matched") is True
              and item.get("source_sha256") == broken_source["sha256"]
              and "SN2_MCP_ACCEPTANCE_CONTROLLED_ERROR" in item.get("message", "")]
    require(errors and controlled["package_rejection"].get("code") == "build_not_succeeded",
            "The controlled failure did not establish attributable diagnostics and packaging refusal")
    for build in [*builds.values(), broken]:
        for stage in build.get("stages", {}).values():
            for channel in ("stdout", "stderr"):
                if stage.get(channel):
                    content = checked_file(Path(stage[channel]["path"]), runtime)
                    require(len(content) == stage[channel]["bytes"] and digest(content) == stage[channel]["sha256"],
                            "A prior complete compiler report changed")
    bridge_build = summary["bridge_build"]
    bridge = next(item for item in bridge_build["artifacts"] if Path(item.get("path", "")).name == "SN2_MCP.dll")
    content = checked_file(Path(bridge["path"]), runtime)
    require(digest(content) == bridge["sha256"], "The selected prior bridge artifact changed")
    return {"summary": summary, "summary_path": str(prior / "summary.json"), "summary_sha256": digest(raw),
            "receipt_inventory": receipt_inventory(prior), "workspace": workspace, "packages": packages,
            "builds": builds, "source_a": source_a, "source_b": source_b, "bridge": bridge,
            "controlled_build_failure": controlled, "prior_root": str(prior)}


async def run(args) -> int:
    runtime = args.runtime_root.resolve(strict=True)
    sys.path.insert(0, str(runtime / "src"))
    game = args.game_root.resolve(strict=True)
    prior = load_prior(runtime, args.prior_run)
    parent = runtime / "artifacts/acceptance"
    output = args.output.resolve() if args.output else parent / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-continue-" + uuid.uuid4().hex[:8])
    require(output.is_relative_to(parent) and not output.exists(), "Choose a new continuation directory within acceptance artifacts")
    output.mkdir()
    summary = {"kind": "continued_compiled_widget_opacity_mod_acceptance", "state": "running",
               "started_utc": datetime.now(timezone.utc).isoformat(), "runtime_root": str(runtime),
               "workspace_root": str(prior["workspace"]), "game_root": str(game),
               "prior_evidence": {key: prior[key] for key in ("summary_path", "summary_sha256", "receipt_inventory")},
               "prior_failure_boundary": "stop_helper_pre_parameter_binding_Confirm_switch",
               "automatic_mutation_retries": False, "installed_game_files_changed": False,
               "controlled_build_failure": prior["controlled_build_failure"], "revisions": [], "cleanup": [],
               "remaining_processes_confirmed": False, "connection_closed": False}
    write_json(output / "prior-evidence.json", {"prior_root": prior["prior_root"], **summary["prior_evidence"],
        "source_a": prior["source_a"], "source_b": prior["source_b"], "builds": prior["builds"]})
    sequence = 0
    process = None
    owned_session = False
    current_package = prior["packages"]["A"]
    current_mod = None
    disable_attempted = False
    watch = None
    failure = None
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "sn2_mcp.server",
        "--workspace-root", str(prior["workspace"]), "--runtime-root", str(runtime), "--game-root", str(game)], cwd=str(runtime))
    async with stdio_client(parameters) as (incoming, outgoing), ClientSession(incoming, outgoing) as client:
        await client.initialize()

        async def tool(name, arguments):
            nonlocal sequence
            sequence += 1
            result = await client.call_tool(name, arguments, read_timeout_seconds=args.readiness_timeout_seconds + 120)
            write_json(output / f"{sequence:04d}.json", {"request": {"tool": name, "arguments": arguments},
                                                       "response": result.model_dump(mode="json")})
            if result.is_error:
                raise ToolFailure(name, error_payload(result), sequence)
            require(isinstance(result.structured_content, dict), "The MCP tool returned no structured evidence")
            return result.structured_content

        async def status_mod(command=None, disabled=False):
            deadline = time.monotonic() + args.readiness_timeout_seconds
            while True:
                value = await tool("sn2_mod_status", {"target_process": process, "package_path": current_package["relative_path"],
                    "expected_manifest_sha256": current_package["manifest_sha256"], "request_id": (command or {}).get("request_id"),
                    "timeout_seconds": args.control_timeout_seconds})
                state = mod_status(value)
                require_mod_progress(value)
                require(state.get("source_revision") == current_package["source_revision"], "The actual module has another compiled source revision")
                ready = state.get("state") == "disabled" and state.get("restoration_confirmed") is True if disabled else state.get("active") is True and state.get("initialized") is True
                if ready:
                    return value
                require(value.get("mod_receipt", {}).get("command", {}).get("state") not in ("failed", "outcome_unknown"),
                        "The explicit mod command failed or remains uncertain")
                require(time.monotonic() < deadline, "The modification did not reach its requested observable state")
                await asyncio.sleep(0.25)

        async def widget():
            found = []
            offset = 0
            while True:
                value = await tool("sn2_find_objects", {"query": "TextLabel", "class_query": "TextBlock", "limit": 200, "offset": offset})
                found.extend(item for item in value["items"] if "/Engine/Transient." in item.get("path", "")
                    and "WBP_StartScreen_PressStart_C_" in item["path"] and ".ContinueBtn." in item["path"])
                if not value.get("truncated"):
                    break
                require(value["next_offset"] > offset, "The current object scan did not advance")
                offset = value["next_offset"]
            require(len(found) == 1, "No unique current start-screen Continue label exists")
            value = await tool("sn2_object_functions", {"handle": found[0]["handle"], "function_query": "RenderOpacity", "limit": 100})
            signatures = {item["name"]: item for item in value["items"]}
            require(signatures.get("GetRenderOpacity", {}).get("signature_status") == "complete", "The getter contract is unavailable")
            return found[0], signatures

        async def getter(target, signatures):
            value = await tool("sn2_invoke", {"handle": target["handle"], "function": "GetRenderOpacity", "arguments": {},
                "expected_signature_token": signatures["GetRenderOpacity"]["signature_token"]})
            require(value.get("executed") is True and value.get("process_event_returned") is True, "The independent getter did not return")
            return value["result"]

        async def snapshot(target):
            return await tool("sn2_snapshot", {"targets": [{"handle": target["handle"], "properties": ["RenderOpacity"]}]})

        async def disable(target, signatures, before, baseline):
            nonlocal disable_attempted, current_mod, watch
            disable_attempted = True
            command = await tool("sn2_mod_disable", {"target_process": process, "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
            disabled = await status_mod(command, disabled=True)
            require(close_number(await getter(target, signatures), baseline), "The current bridge getter did not confirm baseline restoration")
            after = await snapshot(target)
            comparison = await tool("sn2_snapshot_compare", {"before": before, "after": after})
            require(comparison.get("comparable") is True and comparison.get("value_changes") == 1 and comparison.get("unknown_count") == 0,
                    "The current snapshot did not establish restoration")
            await asyncio.sleep(0.1)
            events = await tool("sn2_watch_read", {"watch_id": watch["watch_id"], "limit": 100})
            values = observed_values(events)
            require(not events.get("gap") and not events.get("truncated") and values and close_number(values[-1], baseline),
                    "The current Tick watch did not establish restoration")
            await tool("sn2_watch_stop", {"watch_id": watch["watch_id"]})
            await tool("sn2_watch_forget", {"watch_id": watch["watch_id"]})
            watch = None
            current_mod = None
            return {"mod_disable": disabled, "snapshot_comparison": comparison, "watch": events, "restoration_confirmed": True}

        try:
            summary["frontend"] = await tool("sn2_status", {})
            initial = await tool("sn2_target_processes", {})
            selected = [item for item in initial["processes"] if item["pid"] == args.expected_pid
                        and item["creation_time"] == args.expected_creation_filetime]
            require(len(selected) == 1 and selected[0] == prior["summary"]["revisions"][0]["process"],
                    "The explicitly selected game is not the unchanged restored prior instance")
            process = selected[0]
            summary["initial_processes"] = initial
            observed_a = await tool("sn2_mod_status", {"target_process": process, "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
            state_a = mod_status(observed_a)
            require(state_a.get("state") == "disabled" and state_a.get("restoration_confirmed") is True and state_a.get("active") is False,
                    "The previous mod is not observably disabled and restored; no activation is replayed")
            owned_session = True
            for revision in ("A", "B"):
                verified = await tool("sn2_verify_artifact_package", {"package_path": prior["packages"][revision]["relative_path"]})
                require(verified["manifest_sha256"] == prior["packages"][revision]["manifest_sha256"], "A selected package changed after provenance checks")
                await tool("sn2_build_status", {"build_id": prior["builds"][revision]["build_id"]})
            native = await tool("sn2_native_status", {"target_process": process})
            require(native.get("ready") is True, "The current bridge did not confirm its own reflection and Tick readiness")
            target_a, signatures_a = await widget()
            baseline = await getter(target_a, signatures_a)
            require(close_number(baseline, 1.0), "The restored widget baseline is not the selected neutral value")
            before = await snapshot(target_a)
            watch = await tool("sn2_watch_start", {"handle": target_a["handle"], "properties": ["RenderOpacity"],
                "duration_ms": math.ceil(args.readiness_timeout_seconds * 4000), "interval_ms": 20, "capacity": 64})
            current_mod = {"attempted": True}
            load_a = await tool("sn2_mod_load", {"target_process": process, "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
            current_mod = load_a
            active_a = await status_mod(load_a)
            require(close_number(await getter(target_a, signatures_a), 0.5), "The reactivated compiled revision A did not produce its independent effect")
            after_a = await snapshot(target_a)
            change = await tool("sn2_snapshot_compare", {"before": before, "after": after_a})
            require(change.get("comparable") is True and change.get("value_changes") == 1 and change.get("unknown_count") == 0,
                    "The reactivated revision A change was not confirmed by snapshot")
            cleanup_a = await disable(target_a, signatures_a, after_a, baseline)
            require(observed_values(cleanup_a["watch"])[:2] == [baseline, 0.5], "The Tick watch did not observe the reactivation of revision A")
            summary["revisions"].append({"revision": "A", "source": prior["source_a"], "build": prior["builds"]["A"],
                "package": current_package, "process": process, "active": active_a, "target": target_a, "snapshot_comparison": change, "cleanup": cleanup_a})
            current_package = prior["packages"]["B"]
            disable_attempted = False
            previous = process
            reload = await tool("sn2_mod_reload", {"target_process": process, "package_path": current_package["relative_path"],
                "expected_manifest_sha256": current_package["manifest_sha256"], "bridge_dll_path": prior["bridge"]["path"],
                "expected_bridge_sha256": prior["bridge"]["sha256"], "discovery_timeout_seconds": args.readiness_timeout_seconds,
                "readiness_timeout_seconds": args.readiness_timeout_seconds, "timeout_seconds": args.control_timeout_seconds})
            process = reload["process"]
            require(process != previous and process["creation_time"] != previous["creation_time"] and reload.get("hot_reload") is False,
                    "Revision B did not establish controlled restart and a fresh process identity")
            current_mod = reload["mod"]
            active_b = await status_mod(current_mod)
            target_b, signatures_b = await widget()
            require(close_number(await getter(target_b, signatures_b), 0.75), "The compiled revision B effect was not independently observed")
            require(close_number(mod_status(active_b).get("baseline_opacity"), baseline), "The fresh mod captured another initial baseline")
            after_b = await snapshot(target_b)
            watch = await tool("sn2_watch_start", {"handle": target_b["handle"], "properties": ["RenderOpacity"],
                "duration_ms": math.ceil(args.readiness_timeout_seconds * 4000), "interval_ms": 20, "capacity": 64})
            cleanup_b = await disable(target_b, signatures_b, after_b, baseline)
            require(close_number(observed_values(cleanup_b["watch"])[0], 0.75), "The fresh Tick watch did not observe revision B")
            summary["revisions"].append({"revision": "B", "source": prior["source_b"], "build": prior["builds"]["B"],
                "package": current_package, "process": process, "active": active_b, "target": target_b, "restart_iteration": reload, "cleanup": cleanup_b})
            final_prior = Path(prior["summary_path"]).read_bytes()
            require(digest(final_prior) == prior["summary_sha256"] and receipt_inventory(Path(prior["prior_root"])) == prior["receipt_inventory"],
                    "The referenced prior evidence changed during continuation")
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
            if owned_session and current_mod is not None and not disable_attempted:
                try:
                    await tool("sn2_mod_status", {"target_process": process, "package_path": current_package["relative_path"],
                        "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
                    disable_attempted = True
                    command = await tool("sn2_mod_disable", {"target_process": process, "package_path": current_package["relative_path"],
                        "expected_manifest_sha256": current_package["manifest_sha256"], "timeout_seconds": args.control_timeout_seconds})
                    summary["cleanup"].append({"mod_disable": await status_mod(command, disabled=True)})
                except BaseException as error:
                    summary["cleanup"].append({"mod_disable_unconfirmed": str(error)})
            if watch is not None:
                for name in ("sn2_watch_stop", "sn2_watch_forget"):
                    try:
                        summary["cleanup"].append({name: await tool(name, {"watch_id": watch["watch_id"]})})
                    except BaseException as error:
                        summary["cleanup"].append({name + "_unconfirmed": str(error)})
            if owned_session:
                try:
                    actual = await tool("sn2_target_processes", {})
                    inner = [item for item in actual["processes"] if Path(item["executable"]).name.casefold() == "subnautica2-win64-shipping.exe"]
                    bootstrap = [item for item in actual["processes"] if Path(item["executable"]).name.casefold() == "subnautica2.exe"]
                    require(len(inner) <= 1 and len(bootstrap) <= 1, "Cleanup found ambiguous game instances")
                    selected = inner[0] if inner else bootstrap[0] if bootstrap else None
                    if selected is not None:
                        summary["cleanup"].append({"game_stop": await tool("sn2_development_stop", {"target_process": selected})})
                except BaseException as error:
                    summary["cleanup"].append({"game_stop_unconfirmed": str(error)})
                    summary["state"] = "failed"
                finally:
                    try:
                        actual = await tool("sn2_target_processes", {})
                        summary["final_processes"] = actual
                        summary["remaining_processes_confirmed"] = not actual["processes"]
                        require(summary["remaining_processes_confirmed"], "The continued game session did not exit")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--prior-run", type=Path, required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--expected-creation-filetime", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--readiness-timeout-seconds", type=positive, default=600)
    parser.add_argument("--control-timeout-seconds", type=positive, default=30)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
