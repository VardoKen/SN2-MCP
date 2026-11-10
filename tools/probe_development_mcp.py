"""Check a full development cycle through MCP and close the game it created.

Requires a prepared official compile-probe package. Refuses to run while the game
is already running. Mutating requests are sent exactly once; after the owned
process is identified precisely, stopping it is mandatory.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ntpath
from pathlib import Path
import re
import sys
import time
from typing import Any, Awaitable, Callable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from probe_live_mcp import ProbeError, check_signature
from probe_cleanup import settle_task, deferred_sigint

ROOT = Path(__file__).resolve().parents[1]
Call = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]
_SHA = re.compile(r"[0-9a-fA-F]{64}\Z")
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z\Z")


def checked_identity(value: object) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"pid", "creation_time", "executable"}
            or type(value["pid"]) is not int or not 1 <= value["pid"] <= 0xFFFFFFFF
            or not isinstance(value["creation_time"], str) or not _TIME.fullmatch(value["creation_time"])
            or not isinstance(value["executable"], str) or not value["executable"]):
        raise ProbeError("invalid_process_identity", "Process has no exact confirmed identity")
    return dict(value)


def select_process(state: object, game_root: str, *, bootstrap: bool = False) -> dict[str, Any] | None:
    if not isinstance(state, dict) or not isinstance(state.get("processes"), list):
        raise ProbeError("invalid_process_state", "Invalid game process list response")
    expected = ntpath.normcase(ntpath.join(game_root, *(('G1R-Win64-Shipping.exe',) if bootstrap else
                              ('G1R', 'Binaries', 'Win64', 'G1R-Win64-Shipping.exe'))))
    identities = [checked_identity(item) for item in state["processes"]]
    if len(identities) > 2:
        raise ProbeError("ambiguous_process", "Too many game processes")
    inner = [item for item in identities if ntpath.normcase(item["executable"]) == expected]
    if len(inner) > 1:
        raise ProbeError("ambiguous_process", "Inner game process was identified ambiguously")
    return inner[0] if inner else None


def select_inner(state: object, game_root: str) -> dict[str, Any] | None:
    return select_process(state, game_root)


async def development_cycle(call: Call, relative_package: str, expected_value: int, *,
                            startup_timeout: float = 45, ready_timeout: float = 45,
                            sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                            clock: Callable[[], float] = time.monotonic,
                            progress: Callable[[str], None] | None = None,
                            probe: Callable[[Call], Awaitable[dict[str, Any]]] | None = None,
                            enable_ingame_tools: bool = False,
                            companion_packages: list[str] | None = None,
                            show_script_errors: bool = False) -> dict[str, Any]:
    if startup_timeout <= 0 or ready_timeout <= 0:
        raise ProbeError("invalid_timeout", "Startup and Tick wait must be positive")
    if type(enable_ingame_tools) is not bool:
        raise ProbeError("invalid_arguments", "enable_ingame_tools must be bool")
    if (companion_packages is not None and (not isinstance(companion_packages, list)
            or any(not isinstance(package, str) or not package for package in companion_packages))):
        raise ProbeError("invalid_arguments", "companion_packages requires a list of nonempty package paths")
    report: dict[str, Any] = {"source": "real_stdio_development_cycle", "stages": []}

    def stage(name: str) -> None:
        report["stages"].append(name)
        if progress is not None:
            progress(name)

    recipe_arguments = {"relative_package": relative_package, "enable_ingame_tools": enable_ingame_tools,
                        "show_script_errors": show_script_errors}
    if companion_packages is not None:
        recipe_arguments["companion_packages"] = companion_packages
    recipe = await call("g1r_development_recipe", recipe_arguments)
    manifest = recipe.get("manifest")
    artifact = recipe.get("native_artifact")
    if (not isinstance(manifest, dict) or not isinstance(manifest.get("game_root"), str)
            or not isinstance(manifest.get("manifest_sha256"), str)
            or not _SHA.fullmatch(manifest["manifest_sha256"])
            or not isinstance(artifact, dict) or artifact.get("available") is not True
            or not isinstance(artifact.get("sha256"), str) or not _SHA.fullmatch(artifact["sha256"])):
        raise ProbeError("recipe_incomplete", "Recipe did not confirm the complete package and available DLL")
    initial = await call("g1r_development_processes", {})
    if not isinstance(initial.get("processes"), list) or initial["processes"]:
        raise ProbeError("game_already_running", "Probe requires a closed game; an existing process is not modified")
    stage("recipe_checked")
    owned: dict[str, Any] | None = None
    owned_bootstrap: dict[str, Any] | None = None
    primary: BaseException | None = None
    async def start_and_identify() -> None:
        nonlocal owned, owned_bootstrap
        try:
            launch_arguments = {
                "relative_package": relative_package,
                "expected_manifest_sha256": manifest["manifest_sha256"], "restart": False,
                "enable_ingame_tools": enable_ingame_tools, "show_script_errors": show_script_errors}
            if companion_packages is not None:
                launch_arguments["companion_packages"] = companion_packages
            launch = await call("g1r_development_start", launch_arguments)
            if launch.get("state") != "launch_requested" or launch.get("outcome_unknown") is not False:
                raise ProbeError("launch_unconfirmed", "Launch request is not confirmed; it is not retried automatically")
            stage("launch_requested")
        except BaseException as error:
            # Even an empty initial snapshot does not prove ownership of a new
            # process when the receipt is lost. An uncertain effect is not retried.
            if getattr(error, "outcome_unknown", False) is True:
                error.add_note("Launch receipt was not received; process ownership requires a separate check")
            raise
        deadline = clock() + startup_timeout
        while clock() < deadline:
            state = await call("g1r_development_processes", {})
            candidate_bootstrap = select_process(state, manifest["game_root"], bootstrap=True)
            if owned_bootstrap is None and candidate_bootstrap is not None:
                owned_bootstrap = candidate_bootstrap
                report["owned_bootstrap"] = dict(owned_bootstrap)
            elif candidate_bootstrap is not None and candidate_bootstrap != owned_bootstrap:
                raise ProbeError("bootstrap_process_changed", "Launch process identity changed")
            owned = select_inner(state, manifest["game_root"])
            if owned is not None:
                break
            await sleep(.5)
        if owned is None:
            raise ProbeError("game_start_timeout", "Inner game process did not appear before the deadline")
        report["owned_process"] = dict(owned)
        stage("process_identified")
    try:
        await settle_task(asyncio.create_task(start_and_identify()), min(115, 65 + startup_timeout), "development startup")
        # Rechecking the recipe ties loading to the current package and DLL.
        current = await call("g1r_development_recipe", recipe_arguments)
        if current.get("manifest", {}).get("manifest_sha256") != manifest["manifest_sha256"]:
            raise ProbeError("package_changed", "Package changed during startup")
        current_artifact = current.get("native_artifact", {})
        if current_artifact.get("available") is not True or current_artifact.get("sha256") != artifact["sha256"]:
            raise ProbeError("dll_changed", "DLL changed during startup")
        loaded = await call("g1r_native_load", {"expected_process": owned,
                          "expected_dll_sha256": artifact["sha256"]})
        if (loaded.get("state") != "load_result" or loaded.get("process") != owned
                or loaded.get("dll_sha256") != artifact["sha256"] or loaded.get("outcome_unknown") is not False):
            raise ProbeError("load_unconfirmed", "Response did not confirm a single load of the selected DLL")
        stage("dll_loaded")
        # LoadLibrary finishes before the DLL initialization thread; only status reads are retried.
        deadline = clock() + min(10, ready_timeout)
        while True:
            try:
                status = await call("g1r_native_status", {})
                break
            except ProbeError as error:
                if error.code not in {"native_unavailable", "native_timeout", "native_connection_failed"} or clock() >= deadline:
                    raise
                await sleep(.1)
        if status.get("process_id") != owned["pid"]:
            raise ProbeError("native_process_mismatch", "Bridge belongs to a different process")
        observations = [{key: status.get(key) for key in
                         ("process_id", "hook_installed", "ready", "tick_count", "game_thread_id", "startup_error")}]
        if not status.get("hook_installed"):
            enabled = await call("g1r_native_enable", {})
            if enabled.get("process_id") != owned["pid"]:
                raise ProbeError("native_process_mismatch", "Enable result refers to a different process")
            status = enabled
            observations.append({key: status.get(key) for key in
                                 ("process_id", "hook_installed", "ready", "tick_count", "game_thread_id", "startup_error")})
        deadline = clock() + ready_timeout
        while not status.get("ready") and clock() < deadline:
            await sleep(.1)
            status = await call("g1r_native_status", {})
            if status.get("process_id") != owned["pid"]:
                raise ProbeError("native_process_mismatch", "Native channel process changed")
            selected = {key: status.get(key) for key in
                        ("process_id", "hook_installed", "ready", "tick_count", "game_thread_id", "startup_error")}
            if selected != observations[-1] and len(observations) < 12:
                observations.append(selected)
        if status.get("ready") is not True:
            raise ProbeError("game_not_ready", "Native Tick queue did not start before the deadline: "
                             + json.dumps({"wait_seconds": ready_timeout, "observations": observations,
                                           "last_status": status}, ensure_ascii=False))
        report["readiness_observations"] = observations
        stage("tick_ready")
        if probe is not None:
            report["probe_result"] = await probe(call)
            stage("probe_verified")
            return report
        deadline = clock() + ready_timeout
        while True:
            found = await call("g1r_find_objects", {"query": "Default__G1RMCPCompileProbe",
                               "class_query": "G1RMCPCompileProbe", "limit": 20})
            matches = [item for item in found.get("items", [])
                       if item.get("name") == "Default__G1RMCPCompileProbe"
                       and item.get("class") == "G1RMCPCompileProbe"
                       and item.get("path") == "/Script/Angelscript.Default__G1RMCPCompileProbe"]
            if matches or clock() >= deadline:
                break
            await sleep(.2)
        if len(matches) != 1:
            raise ProbeError("probe_object_absent", "Exact probe package CDO was not found uniquely")
        target = matches[0]
        signature = check_signature(await call("g1r_object_functions", {"handle": target["handle"], "limit": 100}))
        stage("signature_verified")
        result = await call("g1r_invoke", {"handle": target["handle"], "function": "ReadProbeValue", "arguments": {}})
        if result.get("executed") is not True or type(result.get("result")) is not int or result["result"] != expected_value:
            raise ProbeError("probe_value_mismatch", "Probe method did not return the expected constant")
        report.update({"compiled_probe": True, "probe_value": result["result"],
                       "native_version": status.get("version"), "probe_signature": signature})
        stage("probe_verified")
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_process = owned if owned is not None else owned_bootstrap
        if cleanup_process is not None:
            async def stop_owned() -> None:
                try:
                    stopped = await call("g1r_development_stop", {"expected_process": cleanup_process})
                    if (stopped.get("state") != "stop_result" or stopped.get("process") != cleanup_process
                            or stopped.get("tool_result", {}).get("state") != "stopped"):
                        raise ProbeError("stop_unconfirmed", "Owned process stop is not confirmed")
                except Exception:
                    # The compiler may exit the game before stop. An empty snapshot
                    # confirms that the game is absent, but not that stop succeeded.
                    after_error = await call("g1r_development_processes", {})
                    if after_error.get("processes") != []:
                        raise
                    report["stop_confirmed"] = True
                    report["stop_outcome"] = "already_exited"
                    stage("game_already_exited")
                    return
                state = await call("g1r_development_processes", {})
                if state.get("processes") != []:
                    raise ProbeError("game_lingering", "Game processes remain after stop")
                report["stop_confirmed"] = True
                report["stop_outcome"] = "stopped"
                stage("game_stopped")
            try:
                await settle_task(asyncio.create_task(stop_owned()), 45, "development stop")
            except BaseException as cleanup:
                if primary is None:
                    raise
                raise ProbeError("cleanup_failed", f"Probe failed with {type(primary).__name__}; stop: {cleanup}") from primary
    return report


async def run(args: argparse.Namespace) -> dict[str, Any]:
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)]
             + (["--game-root", str(args.game_root)] if args.game_root else []),
        cwd=str(ROOT), env={"PYTHONUTF8": "1"})
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            tools = (await session.list_tools()).tools
            required = {"g1r_development_recipe", "g1r_development_processes", "g1r_development_start",
                        "g1r_development_stop", "g1r_native_load", "g1r_native_status", "g1r_native_enable"}
            if not required <= {tool.name for tool in tools}:
                raise ProbeError("development_tools_absent", "MCP does not provide the complete development cycle")

            async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                result = await session.call_tool(name, arguments)
                if result.is_error:
                    text = next((item.text for item in result.content if item.type == "text"), "MCP error")
                    try:
                        structured_error = json.loads(text)
                    except (ValueError, TypeError):
                        structured_error = {}
                    code = structured_error.get("code", "mcp_call_failed") if isinstance(structured_error, dict) else "mcp_call_failed"
                    error = ProbeError(code, f"{name}: {text}")
                    error.outcome_unknown = (isinstance(structured_error, dict)
                                             and structured_error.get("outcome_unknown") is True)
                    raise error
                if not isinstance(result.structured_content, dict):
                    raise ProbeError("mcp_result_invalid", f"{name}: structured object is missing")
                return result.structured_content

            def progress(stage: str) -> None:
                print(f"MCP development: {stage}", file=sys.stderr, flush=True)

            report = await development_cycle(call, args.relative_package, args.expected_value,
                    startup_timeout=args.startup_timeout, ready_timeout=args.ready_timeout, progress=progress,
                    enable_ingame_tools=args.enable_ingame_tools)
            return {"server_version": initialized.server_info.version, "tool_count": len(tools), **report}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check game startup, DLL loading, and stop through real MCP")
    parser.add_argument("--modkit-root", type=Path, default=ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=ROOT)
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--native-port", type=int, default=27123)
    parser.add_argument("--relative-package", default="artifacts/compile-probe-dev3/G1RMCPProbe")
    parser.add_argument("--expected-value", type=int, choices=(1, 2), default=2)
    parser.add_argument("--startup-timeout", type=float, default=45)
    parser.add_argument("--ready-timeout", type=float, default=45)
    parser.add_argument("--enable-ingame-tools", action="store_true",
                        help="Pass documented -ingametools only to this game session")
    args = parser.parse_args()
    try:
        async def protected_run():
            with deferred_sigint():
                return await run(args)
        print(json.dumps(asyncio.run(protected_run()), ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
            error = error.exceptions[0]
        print(json.dumps({"error": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "error_type": type(error).__name__}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
