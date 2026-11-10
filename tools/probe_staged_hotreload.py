"""Check getter body changes in the active staging copy without controlling the game."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time
import uuid

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_development_helpers_contract import HOST, check_signature
from probe_function_contract_mcp import make_caller
from probe_object_mcp import ProbeError, _single_error
from probe_scalar_out_contract import UNKNOWN_GAME

SOURCE_PACKAGE = "development/helpers/G1RMCPDevelopmentHelpers"
FILE = "Script/G1RMCPDevelopmentHelpers.as"
VERSION_BODY = re.compile(r'(FString\s+GetHelperVersion\s*\(\s*\)\s*\{\s*return\s+")([^"\\\r\n]*)("\s*;\s*\})')


def checked_text(response, package):
    if (not isinstance(response, dict) or response.get("text_mode") != "exact" or response.get("truncated") is not False
            or response.get("package_path") != package or response.get("file_path") != FILE
            or not isinstance(response.get("text"), str) or not isinstance(response.get("sha256"), str)):
        raise ProbeError("hotreload_file_invalid", "The exact staging/source file text was not confirmed")
    raw = response["text"].encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != response["sha256"] or response.get("bytes") != len(raw):
        raise ProbeError("hotreload_file_invalid", "The SHA/length did not match the exact UTF-8 bytes")
    return response["text"], response["sha256"]


def checked_launch(state):
    observed = state.get("launch_observation") if isinstance(state, dict) else None
    if (not isinstance(observed, dict) or observed.get("stage") != "process_running"
            or observed.get("outcome_unknown") is not False or not isinstance(observed.get("launch_id"), str)
            or not observed["launch_id"] or not isinstance(observed.get("process"), dict)
            or not isinstance(state.get("processes"), list) or observed["process"] not in state["processes"]):
        raise ProbeError("hotreload_lease_invalid", "The current owned development process was not confirmed")
    process = observed["process"]
    if (set(process) != {"pid", "creation_time", "executable"} or type(process["pid"]) is not int
            or process["pid"] < 1 or not isinstance(process["creation_time"], str) or not process["creation_time"]
            or not isinstance(process["executable"], str) or not process["executable"]):
        raise ProbeError("hotreload_lease_invalid", "The process lease lacks an exact PID/creation/executable")
    stage = observed.get("development_staging")
    if (not isinstance(stage, dict) or stage.get("edit_target") != "staged_copy_only"
            or not isinstance(stage.get("relative_directory"), str) or not isinstance(stage.get("packages"), list)):
        raise ProbeError("hotreload_stage_invalid", "The launch receipt did not confirm the active staging copy")
    matches = [item for item in stage["packages"] if isinstance(item, dict) and item.get("role") == "companion"
               and item.get("package_name") == HOST and item.get("source_package_path") == SOURCE_PACKAGE]
    if len(matches) != 1: raise ProbeError("hotreload_stage_invalid", "The companion helper was not uniquely found in active staging")
    package = matches[0].get("relative_package")
    if not isinstance(package, str): raise ProbeError("hotreload_stage_invalid", "The staged package path is missing")
    root, path = PurePosixPath(stage["relative_directory"]), PurePosixPath(package)
    if (root.is_absolute() or path.is_absolute() or ".." in root.parts or ".." in path.parts
            or len(root.parts) != 3 or root.parts[:2] != ("artifacts", "development-sessions")
            or path != root / "packages" / HOST):
        raise ProbeError("hotreload_stage_invalid", "The active staged helper path is outside the dedicated session packages")
    return {"launch_id": observed["launch_id"], "process": process, "package": package,
            "stage_directory": stage["relative_directory"]}


def select_helper(found):
    for item in found.get("items", []):
        if (item.get("class") == HOST and item.get("name", "").startswith(HOST)
                and item.get("path", "").startswith("/Game/")
                and item.get("handle")):
            return {key: item[key] for key in ("handle", "class", "name", "path")}
    raise ProbeError("hotreload_helper_invalid", "A live helper has not been found yet")


async def exercise(call, *, wait_seconds=10, clock=time.monotonic, sleep=asyncio.sleep):
    report = {"checks": [], "observations": [], "cleanup": [], "game_started": False, "game_stopped": False,
              "runtime_compiler_diagnostics": "not_confirmed"}
    touched, failure, restore_attempted = False, None, False
    lease, native_owner, helper, original, original_sha, changed_sha = None, None, None, None, None, None
    try:
        lease = checked_launch(await call("g1r_development_processes", {}))
        native = await call("g1r_native_status", {})
        if (native.get("ready") is not True or native.get("process_id") != lease["process"]["pid"]
                or not isinstance(native.get("session_id"), str) or not native["session_id"]):
            raise ProbeError("hotreload_native_invalid", "The native owner did not match the active process lease")
        native_owner = {"session_id": native["session_id"], "process_id": native["process_id"]}
        helper = select_helper(await call("g1r_find_objects", {"query": HOST, "class_query": HOST, "limit": 100}))
        source_text, source_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": SOURCE_PACKAGE, "file_path": FILE}), SOURCE_PACKAGE)
        original, original_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": lease["package"], "file_path": FILE}), lease["package"])
        if original_sha != source_sha: raise ProbeError("hotreload_source_changed", "The staging helper already differs from the canonical source before the probe")
        matches = list(VERSION_BODY.finditer(original))
        if len(matches) != 1: raise ProbeError("hotreload_body_invalid", "The version getter was not uniquely found in the expected simple form")
        match, marker = matches[0], "1.0.0-dev.26-hotreload-" + uuid.uuid4().hex
        baseline = match.group(2)
        modified = original[:match.start(2)] + marker + original[match.end(2):]
        changed_sha = hashlib.sha256(modified.encode("utf-8")).hexdigest()
        report.update(lease=lease, native_owner=native_owner, helper=helper, source_sha256=source_sha,
                      staging_before_sha256=original_sha, staging_marker_sha256=changed_sha, baseline=baseline, temporary_marker=marker)

        async def assert_game():
            current = checked_launch(await call("g1r_development_processes", {}))
            if current["process"] != lease["process"]:
                raise ProbeError("hotreload_owner_changed", "The active game process changed")

        async def version_value():
            await assert_game()
            helper = select_helper(await call("g1r_find_objects", {"query": HOST, "class_query": HOST, "limit": 100}))
            listing = await call("g1r_object_functions", {"handle": helper["handle"], "function_query": "GetHelperVersion", "limit": 10})
            _, token, _ = check_signature(listing, "GetHelperVersion")
            result = await call("g1r_invoke", {"handle": helper["handle"], "function": "GetHelperVersion", "arguments": {}, "expected_signature_token": token})
            if result.get("cleanup_confirmed") is not True:
                raise ProbeError("hotreload_cleanup_unknown", "The FString getter did not confirm cleanup; calls were stopped")
            if result.get("executed") is not True or result.get("return_type") != "StrProperty" or not isinstance(result.get("result"), str):
                raise ProbeError("hotreload_version_invalid", "The getter did not confirm an FString response")
            report["observations"].append({"result": result["result"], "cleanup_confirmed": True, "signature_token": token,
                                       "reflected_functions": listing, "helper": helper})
            return result["result"]

        async def poll(expected):
            deadline = clock() + wait_seconds
            while True:
                value = await version_value()
                if value == expected: return
                if clock() >= deadline: raise ProbeError("hotreload_not_observed", "The expected new getter body was not observed")
                await sleep(.2)

        if await version_value() != baseline: raise ProbeError("hotreload_baseline_mismatch", "The runtime getter did not match the original staging body")
        await assert_game()
        touched = True
        response = await call("g1r_write_package_file", {"relative_path": lease["package"], "file_path": FILE,
            "content": modified, "expected_sha256": original_sha})
        if response.get("written") is not True or response.get("sha256") != changed_sha:
            raise ProbeError("hotreload_write_unconfirmed", "The CAS marker write was not confirmed")
        _, check_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": lease["package"], "file_path": FILE}), lease["package"])
        if check_sha != changed_sha: raise ProbeError("hotreload_write_unconfirmed", "The staging readback did not match markerSHA")
        await poll(marker)
        _, runtime_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": lease["package"], "file_path": FILE}), lease["package"])
        report["staging_runtime_sha256"] = runtime_sha
        if runtime_sha != changed_sha: raise ProbeError("hotreload_staging_changed", "Staging changed while the runtime marker was being observed")
        report["checks"].append("function_body_hotreload_observed_in_same_game")
        return report
    except BaseException as error:
        failure = error
        report["failure_signal"] = {"code": getattr(error, "code", "probe_failed"), "message": str(error)}
        raise
    finally:
        if touched:
            async def restore():
                nonlocal restore_attempted
                # Restore is a separate CAS operation, not a retry of the original write/getter.
                await assert_game()
                text, current_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": lease["package"], "file_path": FILE}), lease["package"])
                if current_sha == original_sha:
                    report["cleanup"].append("original_file_already_present_no_restore_write")
                else:
                    if current_sha != changed_sha: raise ProbeError("hotreload_restore_conflict", "Staging changed after the marker; bytes written by others are not overwritten")
                    restore_attempted = True
                    result = await call("g1r_write_package_file", {"relative_path": lease["package"], "file_path": FILE,
                        "content": original, "expected_sha256": changed_sha})
                    if result.get("written") is not True or result.get("sha256") != original_sha:
                        raise ProbeError("hotreload_restore_unconfirmed", "The single staging restoration was not confirmed")
                    _, restored_sha = checked_text(await call("g1r_read_package_file_exact", {"relative_path": lease["package"], "file_path": FILE}), lease["package"])
                    if restored_sha != original_sha: raise ProbeError("hotreload_restore_unconfirmed", "The restored bytes did not match the original SHA")
                    report["cleanup"].append("original_staging_bytes_restored_once")
                _, after_source = checked_text(await call("g1r_read_package_file_exact", {"relative_path": SOURCE_PACKAGE, "file_path": FILE}), SOURCE_PACKAGE)
                if after_source != source_sha: raise ProbeError("hotreload_source_changed", "The canonical helper source changed during the probe")
                report["source_after_sha256"] = after_source
                if getattr(failure, "code", None) in UNKNOWN_GAME or getattr(failure, "code", None) in {"game_result_error", "hotreload_cleanup_unknown"}:
                    report["cleanup"].append("runtime_restore_verification_skipped_unknown_call_root_stops")
                else:
                    await poll(baseline)
                    report["cleanup"].append("baseline_getter_observed_after_restore")
            try: await settle_task(asyncio.create_task(restore()), timeout=min(60, wait_seconds + 20), label="staging hotreload restore")
            except BaseException as error:
                report["restore_attempted"] = restore_attempted
                if failure is None:
                    error.add_note("Staged hotreload report: " + json.dumps(report, ensure_ascii=False))
                    raise
                failure.add_note("Staging restoration was not confirmed: " + str(error))
            report["restore_attempted"] = restore_attempted
        if failure is not None: failure.add_note("Staged hotreload report: " + json.dumps(report, ensure_ascii=False))


async def run(args):
    async with streamable_http_client(args.mcp_url) as connection:
        async with ClientSession(connection[0], connection[1]) as session:
            await session.initialize()
            return await exercise(make_caller(session), wait_seconds=args.wait_seconds)


def main():
    parser = argparse.ArgumentParser(description="Check getter body hot reload in active staging without starting/stopping the game")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:27124/mcp"); parser.add_argument("--wait-seconds", type=float, default=10); args = parser.parse_args()
    async def protected():
        with deferred_sigint(): return await run(args)
    try: print(json.dumps(asyncio.run(protected()), indent=2, ensure_ascii=False)); return 0
    except BaseException as error:
        error = _single_error(error); print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": str(error), "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr); return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8"); sys.exit(main())
