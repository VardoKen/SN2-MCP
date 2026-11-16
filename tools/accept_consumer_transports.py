"""Check installed consumer MCP transports without connecting to a game.

Run with the isolated installed-wheel Python and a supplied runtime folder.
The check owns two frontend processes and a bound, non-listening native port.
It initializes stdio and localhost HTTP, validates the exact public catalog,
reads local status/capabilities, and verifies native_unavailable without an
effective native request. Only the check's own frontend processes are closed.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import sysconfig
from typing import Any


EXPECTED_TOOLS = frozenset("""
sn2_status sn2_capabilities sn2_target_processes sn2_native_load
sn2_native_status sn2_native_enable sn2_find_objects sn2_object_properties
sn2_read_array sn2_read_set sn2_read_map sn2_read_struct sn2_object_functions
sn2_snapshot sn2_snapshot_compare sn2_watch_start sn2_watch_read sn2_watch_stop
sn2_watch_forget sn2_watch_list sn2_watch_resume sn2_invoke sn2_load_asset
sn2_load_class sn2_blueprint_mod_enable sn2_blueprint_mod_status sn2_blueprint_mod_disable
sn2_source_identity sn2_list_source_files sn2_find_source_files sn2_search_source
sn2_read_source sn2_read_source_exact sn2_write_source sn2_create_source_directory
sn2_development_recipe sn2_export_sources sn2_verify_source_export
sn2_build_recipe sn2_build sn2_build_status sn2_compile_diagnostics
sn2_development_start sn2_development_stop sn2_development_restart
sn2_create_mod_project sn2_validate_mod_project sn2_package_artifact
sn2_verify_artifact_package sn2_mod_load sn2_mod_status sn2_mod_disable sn2_mod_reload
sn2_content_environment sn2_cook_recipe sn2_cook_mod sn2_cook_status sn2_cook_report
sn2_mod_install sn2_mod_uninstall sn2_mod_deployment_status sn2_authoring_recipe
sn2_author_mod sn2_authoring_status sn2_authoring_report sn2_runtime_authoring_recipe
sn2_runtime_compile_blueprint sn2_runtime_save_package sn2_runtime_release_blueprint
sn2_runtime_authoring_status sn2_runtime_authoring_report sn2_runtime_build_status
sn2_package_runtime_artifact sn2_service_status sn2_service_enable sn2_service_disable
sn2_service_call
""".split())


class AcceptanceError(RuntimeError):
    pass


def require(value: bool, message: str) -> None:
    if not value:
        raise AcceptanceError(message)


def now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")


def file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def installed_identity() -> dict:
    import sn2_mcp
    from sn2_mcp import server, tool_catalog
    roots = [Path(sysconfig.get_path(key)).resolve(strict=True) for key in ("purelib", "platlib")]
    modules = [Path(module.__file__).resolve(strict=True) for module in (sn2_mcp, server, tool_catalog)]
    require(all(any(path.is_relative_to(root) for root in roots) for path in modules),
            "The check requires installed site-packages, without a source fallback")
    version = importlib.metadata.version("sn2-mcp")
    require(sn2_mcp.__version__ == version, "Installed metadata and imported frontend versions differ")
    return {"version": version, "python_executable": sys.executable,
            "python_version": sys.version, "isolated_mode": sys.flags.isolated == 1,
            "modules": [{"path": str(path), "sha256": file_hash(path)} for path in modules]}


def own_process_identity() -> dict:
    from sn2_mcp.process_observer import _Kernel32, _timestamp, PROCESS_OBSERVER_ACCESS
    kernel = _Kernel32()
    handle = kernel.api.OpenProcess(PROCESS_OBSERVER_ACCESS, False, os.getpid())
    require(bool(handle), "The frontend process could not capture its own identity")
    try:
        pid, created, executable = kernel.identity(handle)
        return {"pid": pid, "creation_time": _timestamp(created), "executable": executable,
                "parent_pid": os.getppid()}
    finally:
        require(bool(kernel.api.CloseHandle(handle)), "The frontend identity HANDLE did not close")


def frontend_child(identity_path: str, server_args: list[str]) -> int:
    identity = installed_identity()
    path = Path(identity_path)
    pending = path.with_name(path.name + ".pending")
    write_json(pending, {"process": own_process_identity(), "frontend": identity})
    pending.rename(path)
    from sn2_mcp.server import main
    return main(server_args)


def error_value(response: Any) -> dict:
    if isinstance(response.structured_content, dict):
        return response.structured_content
    for item in response.content:
        if item.type == "text":
            try:
                value = json.loads(item.text[item.text.index("{"):])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict):
                return value
    raise AcceptanceError("The error response has no attributable structured payload")


async def check_session(session: Any, transport: str, output: Path, version: str, expected: dict) -> dict:
    initialized = await session.initialize()
    write_json(output / "initialize.json", initialized.model_dump(mode="json", by_alias=True))
    require(initialized.server_info.name == "Subnautica 2 MCP", "Unexpected MCP server identity")
    require(initialized.server_info.version == version, "MCP initialized with a different frontend version")
    require(initialized.capabilities.tools is not None, "MCP tools capability is absent")
    catalog = await session.list_tools()
    raw_catalog = catalog.model_dump(mode="json", by_alias=True)
    write_json(output / "catalog.json", raw_catalog)
    require(catalog.next_cursor is None, "The public catalog is unexpectedly incomplete")
    require(len(catalog.tools) == len(EXPECTED_TOOLS), "The advertised tool count differs")
    require({tool.name for tool in catalog.tools} == EXPECTED_TOOLS, "The exact public catalog differs")
    require(len({tool.name for tool in catalog.tools}) == len(catalog.tools), "The tool catalog has duplicates")
    for tool in catalog.tools:
        require(tool.input_schema.get("type") == "object", f"Invalid input schema for {tool.name}")
        require(isinstance(tool.output_schema, dict) and tool.output_schema.get("type") == "object",
                f"Invalid structured output schema for {tool.name}")
        require(tool.annotations is not None, f"Missing effect annotations for {tool.name}")
    descriptions = {tool.name: tool for tool in catalog.tools}
    for name, required in (("sn2_runtime_compile_blueprint", {"project_path", "spec_path"}),
                           ("sn2_runtime_save_package", {"authoring_id"}),
                           ("sn2_runtime_release_blueprint", {"authoring_id"})):
        require(set(descriptions[name].input_schema.get("required", [])) == required,
                f"Required operation identities differ for {name}")
    statuses = {}
    for name in ("sn2_status", "sn2_capabilities", "sn2_runtime_authoring_status"):
        response = await session.call_tool(name, {})
        write_json(output / (name + ".json"), response.model_dump(mode="json", by_alias=True))
        require(not response.is_error and isinstance(response.structured_content, dict),
                f"Local {name} did not succeed with a structured response")
        statuses[name] = response.structured_content
    status = statuses["sn2_status"]
    require(status["server"]["transport"] == transport and status["server"]["version"] == version,
            "Local frontend status differs from selected transport/version")
    require(status["server"]["ready"] is True and status["native"]["connection_checked"] is False,
            "Local readiness was confused with native availability")
    require(status["native"]["endpoint"] == expected["native_endpoint"],
            "The frontend did not select the owned non-listening native endpoint")
    require(status["workspace"]["root"] == expected["workspace_root"]
            and status["runtime_root"] == expected["runtime_root"] and status["game_root"] == expected["game_root"],
            "The frontend did not select the supplied consumer roots")
    require(status["authoring_dependencies"]["modkit_root"] == expected["modkit_root"]
            and status["authoring_dependencies"]["engine_root"] == expected["engine_root"],
            "The frontend did not select the absent authoring dependencies")
    require(status["game_ready"] is False and status["game_ready_known"] is False,
            "Local status fabricated target readiness")
    capabilities = statuses["sn2_capabilities"]
    require(capabilities["catalog_revision"] == version and capabilities["native_status"] is None,
            "Local capability catalog unexpectedly probed a native target")
    require(capabilities["automatic_retries"] is False,
            "Capability metadata unexpectedly enabled automatic retries")
    contracts = capabilities.get("tool_contracts")
    require(isinstance(contracts, list) and len(contracts) == len(EXPECTED_TOOLS)
            and {item.get("name") for item in contracts} == EXPECTED_TOOLS,
            "The capability contracts differ from the registered public tools")
    require(statuses["sn2_runtime_authoring_status"]["state"] == "not_compiled",
            "The neutral workspace has an unexpected authoring revision")
    unavailable = await session.call_tool("sn2_native_status", {})
    write_json(output / "sn2_native_status.json", unavailable.model_dump(mode="json", by_alias=True))
    native_error = error_value(unavailable)
    require(unavailable.is_error is True and native_error.get("code") == "native_unavailable",
            "A non-listening native endpoint did not give native_unavailable")
    require(native_error.get("may_have_executed") is False,
            "The refused native connection was reported as possibly effective")
    return {"initialized": True, "tool_count": len(catalog.tools),
            "catalog_sha256": file_hash(output / "catalog.json"), "native": native_error,
            "transport_ready": True, "target_executor_ready": False,
            "target_executor_readiness_known": False, "game_requests_effective": False}


async def wait_identity(path: Path, process: Any | None = None) -> dict:
    async with asyncio.timeout(15):
        while not path.exists():
            if process is not None and process.returncode is not None:
                raise AcceptanceError("The frontend exited before producing its process identity")
            await asyncio.sleep(0.02)
    return json.loads(path.read_text(encoding="utf-8"))


class OwnedHttpProcess:
    """Assign the suspended launcher to an owned Job before it can spawn children."""

    def __init__(self, command: list[str], cwd: Path, outlog: Any, errlog: Any):
        import msvcrt
        import subprocess
        import win32api
        import win32con
        import win32job
        import win32process
        self.job = self.launcher = self.thread = None
        self.assigned = False
        inherited = []
        try:
            self.job = win32job.CreateJobObject(None, "")
            limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
            limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
            startup = win32process.STARTUPINFO()
            startup.dwFlags = win32con.STARTF_USESTDHANDLES
            with open(os.devnull, "rb") as stdin:
                current = win32api.GetCurrentProcess()
                for stream in (stdin, outlog, errlog):
                    inherited.append(win32api.DuplicateHandle(
                        current, msvcrt.get_osfhandle(stream.fileno()), current, 0, True,
                        win32con.DUPLICATE_SAME_ACCESS))
                startup.hStdInput, startup.hStdOutput, startup.hStdError = inherited
                self.launcher, self.thread, self.pid, _ = win32process.CreateProcess(
                    command[0], subprocess.list2cmdline(command), None, None, True,
                    win32con.CREATE_SUSPENDED | subprocess.CREATE_NO_WINDOW, None, str(cwd), startup)
            win32job.AssignProcessToJobObject(self.job, self.launcher)
            self.assigned = True
            win32process.ResumeThread(self.thread)
        except BaseException as error:
            self.abort(error)
            raise
        finally:
            errors = []
            for handle in inherited:
                try:
                    win32api.CloseHandle(handle)
                except Exception as error:
                    errors.append(str(error))
            if self.thread is not None:
                try:
                    win32api.CloseHandle(self.thread)
                    self.thread = None
                except Exception as error:
                    errors.append(str(error))
            if errors:
                error = AcceptanceError("HTTP launch HANDLE closure failed: " + "; ".join(errors))
                self.abort(error)
                raise error

    def abort(self, original: BaseException) -> None:
        import win32api
        import win32event
        import win32job
        if self.launcher is not None:
            try:
                if self.assigned:
                    win32job.TerminateJobObject(self.job, 1)
                else:
                    win32api.TerminateProcess(self.launcher, 1)
            except BaseException as error:
                original.add_note("HTTP launch rollback termination: " + str(error))
            try:
                require(win32event.WaitForSingleObject(self.launcher, 15000) == win32event.WAIT_OBJECT_0,
                        "HTTP launcher exit during launch rollback is unconfirmed")
            except BaseException as error:
                original.add_note("HTTP launch rollback wait: " + str(error))
        try:
            self.close()
        except BaseException as error:
            original.add_note("HTTP launch rollback HANDLE closure: " + str(error))

    @property
    def returncode(self) -> int | None:
        import win32event
        import win32process
        state = win32event.WaitForSingleObject(self.launcher, 0)
        if state == win32event.WAIT_TIMEOUT:
            return None
        require(state == win32event.WAIT_OBJECT_0, "HTTP launcher state is unknown")
        return win32process.GetExitCodeProcess(self.launcher)

    def check_member(self, identity: dict) -> None:
        import win32job
        from sn2_mcp.process_observer import capture_checked_process
        require(identity["pid"] == self.pid or identity.get("parent_pid") == self.pid,
                "The HTTP frontend is not the owned launcher or its direct child")
        with capture_checked_process(identity) as lease:
            require(win32job.IsProcessInJob(lease._handle, self.job),
                    "The actual HTTP frontend is outside the owned Job")

    def accounting(self) -> dict:
        import win32job
        counts = win32job.QueryInformationJobObject(self.job, win32job.JobObjectBasicAccountingInformation)
        return {"active_processes": counts["ActiveProcesses"], "total_processes": counts["TotalProcesses"],
                "total_terminated_processes": counts["TotalTerminatedProcesses"],
                "source": "win32_owned_job", "assigned_before_resume": self.assigned,
                "launcher_pid": self.pid}

    def terminate(self) -> bool:
        import win32job
        if self.accounting()["active_processes"] == 0:
            return False
        win32job.TerminateJobObject(self.job, 0)
        return True

    async def wait(self) -> dict:
        async with asyncio.timeout(15):
            while self.returncode is None or self.accounting()["active_processes"] != 0:
                await asyncio.sleep(0.02)
        return {**self.accounting(), "launcher_exit_code": self.returncode,
                "all_job_processes_exited": True}

    def close(self) -> None:
        import win32api
        errors = []
        for name in ("job", "thread", "launcher"):
            handle = getattr(self, name)
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                    setattr(self, name, None)
                except Exception as error:
                    errors.append(f"{name}: {error}")
        if errors:
            raise AcceptanceError("Owned HTTP HANDLE closure failed: " + "; ".join(errors))


async def run(args: argparse.Namespace) -> dict:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp.client.streamable_http import streamable_http_client
    import httpx2
    from sn2_mcp.process_observer import capture_checked_process

    installed = installed_identity()
    require(installed["isolated_mode"], "Run this check with the installed Python -I -B")
    runtime = args.runtime_root.resolve(strict=True)
    game = args.game_root.resolve(strict=True)
    require(runtime.is_dir() and game.is_dir(), "Runtime and game roots must be directories")
    require(not (runtime / "src/sn2_mcp").exists() and not (runtime / "native").exists(),
            "The consumer runtime must not require a bridge source checkout")
    workspace = args.workspace_root.absolute()
    output = args.output.absolute()
    require(not workspace.exists() and not output.exists(), "Workspace and evidence directories must both be new")
    require(workspace != output and not workspace.is_relative_to(output) and not output.is_relative_to(workspace),
            "Workspace and evidence must be separate directories")
    workspace.mkdir(parents=True, exist_ok=False)
    output.mkdir(parents=True, exist_ok=False)
    unavailable_modkit = workspace / "UnavailableConsumerModkit"
    unavailable_engine = workspace / "UnavailableConsumerEngine"
    report = {"kind": "sn2_consumer_transport_acceptance", "schema": 1, "state": "running",
              "started_at": now(), "frontend": installed, "runtime_root": str(runtime),
              "workspace_root": str(workspace), "game_root": str(game),
              "authoring_environment": {"modkit": str(unavailable_modkit), "engine": str(unavailable_engine),
                                         "both_absent": True, "used": False},
              "game_launched": False, "game_files_changed": False,
              "native_game_connected": False, "checks": {}, "processes": {},
              "cleanup": {}, "errors": []}
    script = Path(__file__).resolve(strict=True)
    native_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    native_socket.bind(("127.0.0.1", 0))
    native_port = native_socket.getsockname()[1]
    report["native_endpoint"] = {"host": "127.0.0.1", "port": native_port,
                                 "owned_bound_socket": True, "listening": False}
    common = ["--workspace-root", str(workspace), "--runtime-root", str(runtime),
              "--game-root", str(game), "--native-port", str(native_port),
              "--modkit-root", str(unavailable_modkit), "--engine-root", str(unavailable_engine)]
    expected = {"workspace_root": str(workspace), "runtime_root": str(runtime), "game_root": str(game),
                "modkit_root": str(unavailable_modkit), "engine_root": str(unavailable_engine),
                "native_endpoint": f"127.0.0.1:{native_port}"}
    http_owner = None
    http_lease = None
    stdio_lease = None
    try:
        stdio_output = output / "stdio"
        stdio_output.mkdir()
        stdio_identity_path = stdio_output / "identity.json"
        stdio_command = ["-I", "-B", str(script), "--frontend-child", str(stdio_identity_path),
                         "--", *common, "--transport", "stdio"]
        report["processes"]["stdio"] = {"command": [sys.executable, *stdio_command], "cwd": str(runtime)}
        with (stdio_output / "stderr.log").open("w", encoding="utf-8") as errlog:
            async with stdio_client(StdioServerParameters(command=sys.executable, args=stdio_command,
                                                         cwd=str(runtime)), errlog=errlog) as streams:
                identity = await wait_identity(stdio_identity_path)
                require(identity["frontend"]["version"] == installed["version"], "Child frontend version differs")
                stdio_lease = capture_checked_process(identity["process"])
                report["processes"]["stdio"].update(identity)
                async with ClientSession(streams[0], streams[1], read_timeout_seconds=15) as session:
                    report["checks"]["stdio"] = await check_session(session, "stdio", stdio_output, installed["version"], expected)
        report["cleanup"]["stdio"] = stdio_lease.poll()
        require(report["cleanup"]["stdio"]["state"] == "exited", "Owned stdio frontend exit is unconfirmed")

        http_output = output / "http"
        http_output.mkdir()
        http_identity_path = http_output / "identity.json"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            candidate.bind(("127.0.0.1", 0))
            http_port = candidate.getsockname()[1]
        http_command = ["-I", "-B", str(script), "--frontend-child", str(http_identity_path),
                        "--", *common, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", str(http_port)]
        report["processes"]["http"] = {"command": [sys.executable, *http_command], "cwd": str(runtime)}
        with (http_output / "stdout.log").open("w", encoding="utf-8") as outlog, \
                (http_output / "stderr.log").open("w", encoding="utf-8") as errlog:
            http_owner = OwnedHttpProcess([sys.executable, *http_command], runtime, outlog, errlog)
            report["processes"]["http"]["job"] = http_owner.accounting()
            identity = await wait_identity(http_identity_path, http_owner)
            report["processes"]["http"].update(identity)
            http_owner.check_member(identity["process"])
            http_lease = capture_checked_process(identity["process"])
            url = f"http://127.0.0.1:{http_port}/mcp"
            async with asyncio.timeout(15):
                while True:
                    require(http_owner.returncode is None, "HTTP frontend exited before listening")
                    try:
                        reader, writer = await asyncio.open_connection("127.0.0.1", http_port)
                    except OSError:
                        await asyncio.sleep(0.02)
                        continue
                    writer.close()
                    await writer.wait_closed()
                    break
            async with httpx2.AsyncClient(timeout=15) as http:
                async with streamable_http_client(url, http_client=http, max_sse_event_size=None) as streams:
                    async with ClientSession(streams[0], streams[1], read_timeout_seconds=15) as session:
                        report["checks"]["http"] = await check_session(session, "streamable-http", http_output, installed["version"], expected)
            report["cleanup"]["http_termination_requested"] = http_owner.terminate()
            report["cleanup"]["http_job"] = await http_owner.wait()
        report["cleanup"]["http"] = http_lease.poll()
        require(report["cleanup"]["http"]["state"] == "exited", "Owned HTTP frontend exit is unconfirmed")
        require(report["checks"]["stdio"]["catalog_sha256"] == report["checks"]["http"]["catalog_sha256"],
                "The advertised catalog or schemas differ between transports")
        require(not unavailable_modkit.exists() and not unavailable_engine.exists(), "The check created an authoring environment")
        report["state"] = "passed"
        return report
    except BaseException as error:
        report["state"] = "failed"
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        def cleanup_error(operation: str, error: BaseException) -> None:
            report["state"] = "failed"
            report["errors"].append({"phase": "cleanup", "operation": operation,
                                     "type": type(error).__name__, "message": str(error)})

        if http_owner is not None:
            try:
                requested = http_owner.terminate()
                report["cleanup"]["http_termination_requested"] = (
                    requested or report["cleanup"].get("http_termination_requested", False))
            except BaseException as error:
                cleanup_error("http_job_terminate", error)
            try:
                report["cleanup"]["http_job"] = await http_owner.wait()
            except BaseException as error:
                cleanup_error("http_job_wait", error)
        for name, lease in (("stdio", stdio_lease), ("http", http_lease)):
            if lease is not None:
                try:
                    report["cleanup"][name] = lease.poll()
                    require(report["cleanup"][name]["state"] == "exited", f"Owned {name} exit is unconfirmed")
                except BaseException as error:
                    cleanup_error(name + "_observe", error)
                try:
                    lease.close()
                except BaseException as error:
                    cleanup_error(name + "_handle_close", error)
        if http_owner is not None:
            try:
                http_owner.close()
                report["cleanup"]["http_job_handles_closed"] = True
            except BaseException as error:
                cleanup_error("http_job_handles_close", error)
        try:
            native_socket.close()
            report["cleanup"]["native_bound_socket_closed"] = True
        except BaseException as error:
            cleanup_error("native_bound_socket_close", error)
        report["cleanup"]["all_owned_processes_exited"] = (len(report["processes"]) == 2
            and report["cleanup"].get("stdio", {}).get("state") == "exited"
            and report["cleanup"].get("http_job", {}).get("all_job_processes_exited") is True)
        report["finished_at"] = now()
        write_json(output / "summary.json", report)


def main(argv: list[str] | None = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if values and values[0] == "--frontend-child":
        require(len(values) > 3 and values[2] == "--", "Invalid frontend child arguments")
        return frontend_child(values[1], values[3:])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, required=True, help="New neutral workspace directory")
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    args = parser.parse_args(values)
    try:
        report = asyncio.run(run(args))
        print(json.dumps({"state": report["state"], "summary": str(args.output.absolute() / "summary.json"),
                          "tool_count": len(EXPECTED_TOOLS), "cleanup": report["cleanup"]}))
        return 0 if report["state"] == "passed" else 2
    except Exception as error:
        print(json.dumps({"code": "consumer_transport_acceptance_failed", "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
