"""Bounded development launch workflow using fixed tools.

Read recipes and process snapshots without starting the game. Start/stop/load perform
one explicit operation; Steam and loader responses do not prove compilation,
Tick readiness or loading of the selected package.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import threading
import uuid
from .window_input import WindowInputError, queue_key
from typing import Callable

from .packages import PackageError, validate_package
from .workspace import (PackageWorkspace, WorkspaceError, _checked, _destination_lock,
                        _guard_directories, _read_bytes)

MAX_FILE_BYTES = None
PROCESS_TIMEOUT_SECONDS = 30
_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{7}Z$")


class DevelopmentError(Exception):
    """Operation error with an explicit indication of uncertain external effects."""
    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False,
                 receipt: dict | None = None):
        super().__init__(message)
        self.code = code
        self.outcome_unknown = outcome_unknown
        self.receipt = receipt


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str = ""


Executor = Callable[[list[str], int], CommandResult]


def execute_command(arguments: list[str], timeout: int) -> CommandResult:
    """Run one fixed tool without a shell or retries."""
    process = None
    started = False
    try:
        process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        started = True
        chunks = [bytearray(), bytearray()]

        def drain(stream, output: bytearray) -> None:
            try:
                while True:
                    fragment = stream.read(4096)
                    if not fragment:
                        break
                    output.extend(fragment)
            finally:
                stream.close()

        threads = [threading.Thread(target=drain, args=(stream, output), daemon=True)
                   for stream, output in zip((process.stdout, process.stderr), chunks)]
        for thread in threads:
            thread.start()
        returncode = process.wait(timeout=timeout)
        for thread in threads:
            thread.join(1)
        if any(thread.is_alive() for thread in threads):
            raise DevelopmentError("tool_channel_incomplete", "The tool output channel did not finish",
                                   outcome_unknown=True)
        return CommandResult(returncode, chunks[0].decode("utf-8"), chunks[1].decode("utf-8"))
    except subprocess.TimeoutExpired:
        if process is not None:
            process.kill()
            process.wait(timeout=2)
        raise DevelopmentError("operation_timeout", "The tool did not finish within 30 seconds; the external operation outcome is unknown",
                               outcome_unknown=True) from None
    except (OSError, UnicodeError):
        raise DevelopmentError("tool_unavailable", "Could not start or read the fixed tool",
                               outcome_unknown=started) from None
    except DevelopmentError:
        raise
    except BaseException as error:
        # KeyboardInterrupt/SystemExit must not leave a local helper running.
        # The external effect (Steam/LoadLibrary) remains uncertain and is not retried.
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except BaseException:
                pass
        if started:
            error.add_note("The local helper was stopped; the external operation outcome is unknown and retries are prohibited")
        raise


def _unique_json(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _identity(value: object) -> dict:
    if not isinstance(value, dict) or type(value.get("pid")) is not int or not 1 <= value["pid"] <= 0xFFFFFFFF:
        raise DevelopmentError("invalid_process_identity", "The verified process PID, creation_time, and executable are required")
    if not isinstance(value.get("creation_time"), str) or not _TIMESTAMP.fullmatch(value["creation_time"]):
        raise DevelopmentError("invalid_process_identity", "creation_time must be the exact UTC timestamp from the development recipe")
    if not isinstance(value.get("executable"), str) or not value["executable"]:
        raise DevelopmentError("invalid_process_identity", "The exact process executable is missing")
    return {"pid": value["pid"], "creation_time": value["creation_time"], "executable": value["executable"]}


class DevelopmentService:
    def __init__(self, *, repository_root: str | Path, workspace_root: str | Path,
                 modkit_root: str | Path, game_root: str | Path,
                 executor: Executor = execute_command, powershell: str | None = None,
                 process_lease_factory: Callable[[dict], object] | None = None):
        try:
            self.repository = _checked(Path(repository_root), directory=True)
            self.game = _checked(Path(game_root), directory=True)
            self.modkit = _checked(Path(modkit_root), directory=True)
            self.workspace = PackageWorkspace(workspace_root, protected_roots=(self.modkit, self.game))
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        self.executor = executor
        self.powershell = powershell or str(Path(os.environ.get("WINDIR", r"C:\Windows")) /
                                          "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")
        self.inner_executable = self.game / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe"
        self.bootstrap_executable = self.game / "G1R-Win64-Shipping.exe"
        self._observation_lock = threading.RLock()
        self._launch_observation: dict | None = None
        self._process_lease = None
        self._process_lease_factory = process_lease_factory
        self._closed = False

    def close(self) -> None:
        """Release only the retained read-only HANDLE without terminating the game."""
        with self._observation_lock:
            if self._closed:
                return
            self._closed = True
            try:
                self._close_process_lease()
            finally:
                if self._launch_observation is not None:
                    self._launch_observation["observer_closed"] = True
                    if self._launch_observation["stage"] != "process_exited":
                        self._launch_observation.update(stage="observation_closed", outcome_unknown=True)

    def _close_process_lease(self) -> None:
        lease, self._process_lease = self._process_lease, None
        if lease is not None:
            lease.close()

    def _observe_launch(self, processes: list[dict]) -> dict | None:
        record = self._launch_observation
        if record is None:
            return None
        if self._closed or record.get("stage") == "process_exited":
            return copy.deepcopy(record)
        if self._process_lease is None and record["process"] is None and not record.get("capture_attempted"):
            inner = [item for item in processes if os.path.normcase(item["executable"]) == os.path.normcase(str(self.inner_executable))]
            if not inner:
                record.update(stage="launch_unobserved", outcome_unknown=True)
                return copy.deepcopy(record)
            identity = inner[0]
            # Do not assign a preexisting process to a late claim.
            created = datetime.fromisoformat(identity["creation_time"].replace("Z", "+00:00"))
            requested = datetime.fromisoformat(record["launch_request_started_at"])
            if created < requested:
                record.update(stage="capture_unavailable", capture_attempted=True,
                              observation_error_code="process_predates_launch", outcome_unknown=True)
                return copy.deepcopy(record)
            record.update(capture_attempted=True, process=dict(identity), process_observed=True,
                          process_correlation="first_inner_snapshot_after_launch")
            try:
                factory = self._process_lease_factory
                if factory is None:
                    from .process_observer import capture_checked_process
                    factory = capture_checked_process
                self._process_lease = factory(identity)
            except Exception as error:
                record.update(stage="capture_unavailable", outcome_unknown=True,
                              observation_error_code=getattr(error, "code", "process_observer_unavailable"))
                return copy.deepcopy(record)
        if self._process_lease is not None:
            try:
                observed = self._process_lease.poll()
                if not isinstance(observed, dict) or observed.get("process") != record["process"]:
                    raise ValueError("observer identity mismatch")
                state = observed.get("state")
                if state == "running":
                    record.update(stage="process_running", outcome_unknown=False)
                elif state == "exited":
                    exit_code = observed.get("exit_code")
                    exit_time = observed.get("exit_time")
                    if type(exit_code) is not int or not 0 <= exit_code <= 0xFFFFFFFF or not isinstance(exit_time, str):
                        raise ValueError("invalid observer exit result")
                    record.update(stage="process_exited", exit_code=exit_code, exit_time=exit_time,
                                  outcome_unknown=False, termination_cause="unknown",
                                  process_failed=exit_code != 0)
                    try:
                        self._close_process_lease()
                    except Exception as error:
                        # Preserve an already read exitcode if releasing the HANDLE fails.
                        record["observer_close_error"] = getattr(error, "code", "process_handle_close_failed")
                else:
                    record.update(stage="observation_unknown", outcome_unknown=True,
                                  observation_error_code=observed.get("error_code", "process_observation_failed"))
            except Exception as error:
                record.update(stage="observation_unknown", outcome_unknown=True,
                              observation_error_code=getattr(error, "code", "process_observation_failed"))
        return copy.deepcopy(record)

    def _command(self, script: str, arguments: list[str], *, mutating: bool) -> dict:
        try:
            script_path = _checked(self.repository / "tools" / script, self.repository)
            if not script_path.is_file():
                raise WorkspaceError("invalid_path", "The fixed tool must be a regular file")
        except WorkspaceError as error:
            raise DevelopmentError("tool_unavailable", str(error)) from None
        result = self.executor([self.powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                "-File", str(script_path), *arguments, "-Json"], PROCESS_TIMEOUT_SECONDS)
        if result.returncode != 0:
            raise DevelopmentError("tool_failed", "The fixed tool failed; there is no automatic retry",
                                   outcome_unknown=mutating)
        try:
            value = json.loads(result.stdout.lstrip("\ufeff"), object_pairs_hook=_unique_json,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        except (ValueError, RecursionError):
            raise DevelopmentError("tool_protocol_error", "The tool did not return valid structured JSON",
                                   outcome_unknown=mutating) from None
        if not isinstance(value, dict):
            raise DevelopmentError("tool_protocol_error", "The tool response root must be an object",
                                   outcome_unknown=mutating)
        return value

    def process_state(self) -> dict:
        with self._observation_lock:
            return self._process_state()

    def _process_state(self) -> dict:
        value = self._command("game_process_state.ps1", ["-GameRoot", str(self.game)], mutating=False)
        processes = value.get("processes")
        if not isinstance(processes, list) or len(processes) > 2:
            raise DevelopmentError("tool_protocol_error", "Invalid game process list")
        checked = []
        allowed = {os.path.normcase(str(self.inner_executable)), os.path.normcase(str(self.bootstrap_executable))}
        for item in processes:
            identity = _identity(item)
            if os.path.normcase(identity["executable"]) not in allowed:
                raise DevelopmentError("process_path_mismatch", "The game process has an unverified executable")
            checked.append(identity)
        if len({item["pid"] for item in checked}) != len(checked):
            raise DevelopmentError("tool_protocol_error", "The process list contains a duplicate PID")
        if len({os.path.normcase(item["executable"]) for item in checked}) != len(checked):
            raise DevelopmentError("ambiguous_process", "Multiple instances of the same game EXE were found")
        return {"processes": checked, "running": bool(checked), "source": "windows_process_snapshot",
                "launch_observation": self._observe_launch(checked)}

    def _manifest(self, relative_package: str, *, enable_ingame_tools: bool = False,
                  show_script_errors: bool = False) -> dict:
        if type(enable_ingame_tools) is not bool:
            raise DevelopmentError("invalid_arguments", "enable_ingame_tools must be a bool")
        if type(show_script_errors) is not bool:
            raise DevelopmentError("invalid_arguments", "show_script_errors must be a bool")
        try:
            package, normalized = self.workspace._package(relative_package)
            if normalized == ".":
                raise DevelopmentError("invalid_package", "Launching requires a separate official package directory")
            validation = validate_package(self.workspace.root, normalized)
            if not validation["valid_structure"]:
                raise DevelopmentError("invalid_package", "The official package failed structural validation")
            name = validation["metadata"]["name"]
            if not re.fullmatch(r"[A-Za-z0-9_-]+", name) or package.name != name:
                raise DevelopmentError("invalid_package_name", "The package name must exactly match the directory name")
            entries = []
            total = 0
            file_stamps = {}

            def walk_failure(error: OSError) -> None:
                raise DevelopmentError("access_error", "Could not enumerate all package files")

            with _guard_directories(package, self.workspace.root):
                for directory, directories, files in os.walk(package, followlinks=False, onerror=walk_failure):
                    parent = _checked(Path(directory), package, directory=True)
                    for child in directories:
                        _checked(parent / child, package, directory=True)
                    for child in files:
                        file = _checked(parent / child, package)
                        if not file.is_file():
                            raise DevelopmentError("unsafe_package_file", "The package contains a nonregular file")
                        before = file.stat()
                        data = _read_bytes(file, package, None)
                        total += len(data)
                        entries.append({"path": file.relative_to(package).as_posix(), "bytes": len(data),
                                        "sha256": hashlib.sha256(data).hexdigest()})
                        info = file.stat()
                        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                            raise DevelopmentError("package_changed", "A package file changed while the manifest was being read")
                        file_stamps[file.relative_to(package).as_posix()] = (info.st_dev, info.st_ino,
                                                                           info.st_size, info.st_mtime_ns)
                after_paths = set()
                for directory, directories, files in os.walk(package, followlinks=False, onerror=walk_failure):
                    parent = _checked(Path(directory), package, directory=True)
                    for child in directories:
                        _checked(parent / child, package, directory=True)
                    for child in files:
                        file = _checked(parent / child, package)
                        relative = file.relative_to(package).as_posix()
                        after_paths.add(relative)
                        info = file.stat()
                        if file_stamps.get(relative) != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                            raise DevelopmentError("package_changed", "The package changed while the manifest was being read")
                if after_paths != set(file_stamps):
                    raise DevelopmentError("package_changed", "Package contents changed while the manifest was being read")
            entries.sort(key=lambda item: item["path"])
            # The validator must describe the exact descriptor bytes included in the hash.
            final_validation = validate_package(self.workspace.root, normalized)
            descriptor_entry = next((entry for entry in entries if entry["path"] == "GothicMod.json"), None)
            descriptor_hash = hashlib.sha256(_read_bytes(package / "GothicMod.json", package, None)).hexdigest()
            if (not final_validation["valid_structure"] or final_validation["metadata"] != validation["metadata"]
                    or final_validation["script_count"] != validation["script_count"]
                    or final_validation.get("patch_count", 0) != validation.get("patch_count", 0)
                    or descriptor_entry is None or descriptor_entry["sha256"] != descriptor_hash):
                raise DevelopmentError("package_changed", "The descriptor or package structure changed during manifest validation")
            payload = {"package_path": normalized, "package_name": name,
                       "workspace_root": str(self.workspace.root), "modkit_root": str(self.modkit),
                       "game_root": str(self.game), "files": entries,
                       "launch_options": {"enable_ingame_tools": enable_ingame_tools}}
            if show_script_errors:
                payload["launch_options"]["show_script_errors"] = True
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            digest = hashlib.sha256(serialized).hexdigest()
            return {**payload, "manifest_sha256": digest, "total_bytes": total,
                    "script_count": validation["script_count"], "patch_count": validation.get("patch_count", 0),
                    "compiled": False, "runtime_validation": "not_performed"}
        except (PackageError, WorkspaceError) as error:
            raise DevelopmentError(error.code, str(error)) from None
        except OSError:
            raise DevelopmentError("access_error", "Could not read the complete package for launch") from None

    def _composition_manifest(self, relative_package: str, companion_packages: list[str] | None,
                              *, enable_ingame_tools: bool = False, show_script_errors: bool = False) -> dict:
        if companion_packages is not None and (not isinstance(companion_packages, list)
                or any(not isinstance(item, str) or not item for item in companion_packages)):
            raise DevelopmentError("invalid_arguments", "companion_packages must be a list of package paths")
        if not companion_packages:
            return self._manifest(relative_package, enable_ingame_tools=enable_ingame_tools,
                                  show_script_errors=show_script_errors)
        packages = [self._manifest(path, enable_ingame_tools=enable_ingame_tools,
                                   show_script_errors=show_script_errors)
                    for path in [relative_package, *companion_packages]]
        names = [item["package_name"] for item in packages]
        if len({name.casefold() for name in names}) != len(names):
            raise DevelopmentError("duplicate_package", "Selected package names must be unique regardless of case")
        source_paths = [os.path.normcase(str(self.workspace.root / item["package_path"])) for item in packages]
        if len(set(source_paths)) != len(source_paths):
            raise DevelopmentError("duplicate_package", "The same package cannot be selected more than once")
        payload = {"composition": "explicit_companion_packages", "package_path": packages[0]["package_path"],
                   "package_name": names[0], "workspace_root": str(self.workspace.root),
                   "modkit_root": str(self.modkit), "game_root": str(self.game),
                   "packages": [{"role": "main" if index == 0 else "companion", **item}
                                for index, item in enumerate(packages)],
                   "selected_mods": names, "launch_options": {"enable_ingame_tools": enable_ingame_tools}}
        if show_script_errors:
            payload["launch_options"]["show_script_errors"] = True
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return {**payload, "manifest_sha256": hashlib.sha256(serialized).hexdigest(),
                "total_bytes": sum(item["total_bytes"] for item in packages),
                "compiled": False, "runtime_validation": "not_performed"}

    def _stage_composition(self, manifest: dict) -> dict:
        """Copy verified bytes into a new persistent working development session."""
        sessions = self.workspace.root / "artifacts" / "development-sessions"
        stage = sessions / uuid.uuid4().hex
        package_root = stage / "packages"
        try:
            with _destination_lock(self.workspace.root), _guard_directories(self.workspace.root, self.workspace.root):
                for parent in (sessions.parent, sessions):
                    with _guard_directories(parent.parent, self.workspace.root):
                        if not os.path.lexists(parent):
                            parent.mkdir()
                        self.workspace._unprotected(_checked(parent, self.workspace.root, directory=True))
                with _guard_directories(sessions, self.workspace.root):
                    if os.path.lexists(stage):
                        raise DevelopmentError("staging_conflict", "The development session already exists")
                    stage.mkdir()
                    self.workspace._unprotected(_checked(stage, self.workspace.root, directory=True))
                    with _guard_directories(stage, self.workspace.root):
                        package_root.mkdir()
                        self.workspace._unprotected(_checked(package_root, self.workspace.root, directory=True))
            copied = []
            for source in manifest["packages"]:
                package_name = source["package_name"]
                target = package_root / package_name
                with _destination_lock(package_root), _guard_directories(package_root, self.workspace.root):
                    target.mkdir()
                    self.workspace._unprotected(_checked(target, self.workspace.root, directory=True))
                    with _guard_directories(target, self.workspace.root):
                        (target / "Script").mkdir()
                source_root, _ = self.workspace._package(source["package_path"])
                for entry in source["files"]:
                    data = _read_bytes(source_root / entry["path"], source_root, None)
                    if len(data) != entry["bytes"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
                        raise DevelopmentError("package_changed", "The source package changed while the development session was being prepared")
                    destination = target / entry["path"]
                    with _destination_lock(target), _guard_directories(target, self.workspace.root):
                        current = target
                        for component in Path(entry["path"]).parts[:-1]:
                            with _guard_directories(current, self.workspace.root):
                                current = current / component
                                if not os.path.lexists(current):
                                    current.mkdir()
                                self.workspace._unprotected(_checked(current, self.workspace.root, directory=True))
                        with _guard_directories(destination.parent, self.workspace.root):
                            with destination.open("xb") as stream:
                                stream.write(data)
                relative = target.relative_to(self.workspace.root).as_posix()
                staged = self._manifest(relative, enable_ingame_tools=manifest["launch_options"]["enable_ingame_tools"],
                                        show_script_errors=manifest["launch_options"].get("show_script_errors", False))
                for key in ("package_name", "files", "total_bytes", "script_count", "patch_count"):
                    if staged[key] != source[key]:
                        raise DevelopmentError("staging_changed", "The working copy does not match the verified package bytes")
                copied.append({"role": source["role"], "source_package_path": source["package_path"],
                               "source_manifest_sha256": source["manifest_sha256"],
                               "relative_package": relative, "package_name": package_name,
                               "manifest_sha256_at_launch": staged["manifest_sha256"], "manifest": staged})
            stage_info = {"relative_directory": stage.relative_to(self.workspace.root).as_posix(),
                          "mods_directory": str(package_root), "packages": copied,
                          "source_manifest_sha256": manifest["manifest_sha256"],
                          "edit_target": "staged_copy_only", "retention": "retain_for_review",
                          "hot_reload": "function_bodies_file_watch"}
            with _guard_directories(stage, self.workspace.root):
                with (stage / "session.json").open("x", encoding="utf-8") as stream:
                    json.dump(stage_info, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self._verify_staged_composition(stage_info)
            return stage_info
        except (OSError, WorkspaceError) as error:
            raise DevelopmentError("staging_error", "Could not prepare a separate working development session") from error

    def _verify_staged_composition(self, stage: dict) -> None:
        root = self.workspace._unprotected(_checked(Path(stage["mods_directory"]), self.workspace.root, directory=True))
        expected = {item["package_name"] for item in stage["packages"]}
        with _guard_directories(root, self.workspace.root):
            if {item.name for item in root.iterdir()} != expected:
                raise DevelopmentError("staging_changed", "The working development session contents changed")
            for item in stage["packages"]:
                current = self._manifest(item["relative_package"],
                    enable_ingame_tools=item["manifest"]["launch_options"]["enable_ingame_tools"],
                    show_script_errors=item["manifest"]["launch_options"].get("show_script_errors", False))
                if current["manifest_sha256"] != item["manifest_sha256_at_launch"]:
                    raise DevelopmentError("staging_changed", "The working package changed before game launch")

    def native_artifact(self) -> dict:
        """Get the built DLL SHA-256 for a subsequent explicit load."""
        try:
            dll = _checked(self.repository / "build" / "native" / "Release" / "G1R_MCP.dll", self.repository)
            raw = _read_bytes(dll, self.repository, MAX_FILE_BYTES)
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        return {"path": str(dll), "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw),
                "loaded": False, "runtime_validation": "not_performed"}

    def recipe(self, relative_package: str, *, companion_packages: list[str] | None = None,
               enable_ingame_tools: bool = False, show_script_errors: bool = False) -> dict:
        manifest = self._composition_manifest(relative_package, companion_packages,
                                              enable_ingame_tools=enable_ingame_tools,
                                              show_script_errors=show_script_errors)
        try:
            artifact = {"available": True, **self.native_artifact()}
        except DevelopmentError as error:
            artifact = {"available": False, "reason_code": error.code}
        return {"manifest": manifest, "process_state": self.process_state(), "native_artifact": artifact,
                "launch": {"mods_directory": None,
                           "mods": manifest.get("selected_mods", [manifest["package_name"]]),
                           "enable_ingame_tools": enable_ingame_tools,
                           "show_script_errors": show_script_errors,
                           "compiler_exit_on_error": not show_script_errors,
                           "effects": "Replace the package selection only in the game session being launched",
                           "compilation_trigger": "game_startup", "hot_reload": "function_bodies_file_watch",
                           "staging_created_by": "development_start", "edit_target": "staged_copy_only",
                           "staging_retention": "retain_for_review", "source_packages_modified": False},
                "diagnostics_available": False, "dedicated_compile_command": False,
                "compiled": False, "runtime_validation": "not_performed"}

    def _expected_process(self, expected_process: dict, state: dict, *, allow_bootstrap: bool = False) -> dict:
        expected = _identity(expected_process)
        allowed = {os.path.normcase(str(self.inner_executable))}
        if allow_bootstrap:
            allowed.add(os.path.normcase(str(self.bootstrap_executable)))
        if os.path.normcase(expected["executable"]) not in allowed:
            raise DevelopmentError("process_path_mismatch", "The expected process has an unverified EXE for the selected game")
        matches = [item for item in state["processes"] if item == expected]
        if len(matches) != 1:
            raise DevelopmentError("process_changed", "The PID, creation time, or game path changed")
        return expected

    def start(self, relative_package: str, expected_manifest_sha256: str, *, restart: bool = False,
              expected_process: dict | None = None, companion_packages: list[str] | None = None,
              enable_ingame_tools: bool = False, show_script_errors: bool = False) -> dict:
        with self._observation_lock:
            if self._closed:
                raise DevelopmentError("service_closed", "The observation workflow is already closed")
            return self._start(relative_package, expected_manifest_sha256, restart=restart,
                               expected_process=expected_process, companion_packages=companion_packages,
                               enable_ingame_tools=enable_ingame_tools, show_script_errors=show_script_errors)

    def _start(self, relative_package: str, expected_manifest_sha256: str, *, restart: bool = False,
               expected_process: dict | None = None, companion_packages: list[str] | None = None,
               enable_ingame_tools: bool = False, show_script_errors: bool = False) -> dict:
        if not isinstance(expected_manifest_sha256, str) or not _SHA.fullmatch(expected_manifest_sha256):
            raise DevelopmentError("invalid_manifest_hash", "The SHA-256 of the full package from the development recipe is required")
        if type(restart) is not bool:
            raise DevelopmentError("invalid_arguments", "restart must be a bool")
        manifest = self._composition_manifest(relative_package, companion_packages,
                                              enable_ingame_tools=enable_ingame_tools,
                                              show_script_errors=show_script_errors)
        if manifest["manifest_sha256"] != expected_manifest_sha256.lower():
            raise DevelopmentError("package_changed", "The package changed after the recipe was prepared")
        state = self.process_state()
        selected_mods = manifest.get("selected_mods", [manifest["package_name"]])
        arguments = ["-GameRoot", str(self.game)]
        if enable_ingame_tools:
            arguments.append("-EnableIngameTools")
        if show_script_errors:
            arguments.append("-ShowScriptErrors")
        if restart:
            expected = self._expected_process(expected_process, state)
            arguments += ["-Restart", "-ExpectedPid", str(expected["pid"]),
                          "-ExpectedCreationTime", expected["creation_time"]]
        elif state["running"]:
            raise DevelopmentError("game_running", "A running game requires restart and an exact process identity")
        elif expected_process is not None:
            raise DevelopmentError("invalid_arguments", "expected_process is used only with restart")
        stage_manifest = manifest if "packages" in manifest else {
            **manifest, "packages": [{"role": "main", **manifest}], "selected_mods": selected_mods}
        stage = self._stage_composition(stage_manifest)
        fresh = self._composition_manifest(relative_package, companion_packages,
                                           enable_ingame_tools=enable_ingame_tools,
                                           show_script_errors=show_script_errors)
        if fresh["manifest_sha256"] != manifest["manifest_sha256"]:
            raise DevelopmentError("package_changed", "Source packages changed while the development session was being prepared")
        self._verify_staged_composition(stage)
        arguments.extend(["-ModsDirectory", stage["mods_directory"], "-ModsCsv", ",".join(selected_mods)])
        try:
            self._close_process_lease()
        except Exception:
            raise DevelopmentError("observer_close_failed", "The old HANDLE was not released; no new launch was performed") from None
        if self._launch_observation is not None and self._launch_observation["stage"] != "process_exited":
            self._launch_observation.update(stage="observation_closed", observer_closed=True, outcome_unknown=True)
        request_started_at = datetime.now(timezone.utc).isoformat()
        response = self._command("start_g1r_development.ps1", arguments, mutating=True)
        receipt_root = response.get("mods_directory")
        receipt_matches = (response.get("mods") == selected_mods and isinstance(receipt_root, str)
            and os.path.normcase(receipt_root) == os.path.normcase(stage["mods_directory"]))
        if response.get("state") != "launch_requested" or not receipt_matches:
            raise DevelopmentError("tool_protocol_error", "The launch response did not confirm that the selected package was passed to Steam",
                                   outcome_unknown=True)
        self._launch_observation = {"launch_id": str(uuid.uuid4()),
            "manifest_sha256": manifest["manifest_sha256"], "package_name": manifest["package_name"],
            "relative_package": manifest["package_path"], "enable_ingame_tools": enable_ingame_tools,
            "show_script_errors": show_script_errors,
            "launch_request_started_at": request_started_at,
            "launch_requested_at": datetime.now(timezone.utc).isoformat(),
            "stage": "launch_unobserved", "process": None, "process_observed": False,
            "capture_attempted": False, "exit_code": None, "exit_time": None,
            "compiler_exit_on_error": not show_script_errors, "compiler_failure_confirmed": False,
            "compiled_confirmed": False, "runtime_validation": "not_performed",
            "manifest_provenance": "source_packages_verified_staged_bytes_at_launch", "outcome_unknown": True,
            "selected_mods": selected_mods, "development_staging": stage}
        return {"state": "launch_requested", "manifest_sha256": manifest["manifest_sha256"],
                "package_name": manifest["package_name"], "tool_result": response,
                "enable_ingame_tools": enable_ingame_tools,
                "show_script_errors": show_script_errors,
                "compiler_exit_on_error": not show_script_errors,
                "compiled": False, "runtime_validation": "not_performed",
                "game_ready": False, "outcome_unknown": False,
                "launch_observation": copy.deepcopy(self._launch_observation),
                "development_staging": copy.deepcopy(stage),
                "active_staging_packages": [{key: item[key] for key in
                        ("role", "source_package_path", "relative_package", "package_name", "manifest_sha256_at_launch")}
                    for item in stage["packages"]], "source_packages_modified": False,
                "edit_target": "staged_copy_only", "hot_reload": "function_bodies_file_watch",
                "staging_retention": "retain_for_review"}

    def apply_staged_file(self, relative_package: str, file_path: str,
                          expected_source_sha256: str, expected_staged_sha256: str,
                          expected_process: dict) -> dict:
        with self._observation_lock:
            if self._closed:
                raise DevelopmentError("service_closed", "The development service is already closed")
            state = self._process_state()
            expected = self._expected_process(expected_process, state)
            record = state["launch_observation"]
            if not record or record.get("stage") != "process_running" or record.get("process") != expected:
                raise DevelopmentError("process_changed", "The active development launch was not confirmed")
            staging = record.get("development_staging")
            if not staging:
                raise DevelopmentError("staging_unavailable", "The active launch has no staging composition")
            for value in (expected_source_sha256, expected_staged_sha256):
                if not isinstance(value, str) or not _SHA.fullmatch(value):
                    raise DevelopmentError("invalid_arguments", "Expected source and staging SHA-256 values are required")
            try:
                parts = self.workspace._file_parts(file_path)
                if parts[0] != "Script" or Path(parts[-1]).suffix.lower() != ".as":
                    raise DevelopmentError("unsupported_file", "Staged application accepts only Script/**/*.as files")
                with self.workspace._lock:
                    source = self.workspace.read_package_file_exact(relative_package, file_path)
                    matches = [item for item in staging["packages"]
                               if item["source_package_path"] == source["package_path"]]
                    if len(matches) != 1:
                        raise DevelopmentError("package_not_active", "The source package is not in the active composition")
                    item = matches[0]
                    if source["sha256"] != expected_source_sha256.lower():
                        raise DevelopmentError("package_changed", "The source file changed before application")
                    result = self.workspace.write_package_file(
                        item["relative_package"], source["file_path"], source["text"], expected_staged_sha256)
            except WorkspaceError as error:
                raise DevelopmentError(error.code, str(error)) from error
            return {**result, "source_package_path": source["package_path"],
                    "staged_package_path": item["relative_package"], "source_sha256": source["sha256"],
                    "staged_before_sha256": expected_staged_sha256.lower(),
                    "process": expected, "launch_id": record["launch_id"], "source_modified": False,
                    "hot_reload": "function_bodies_file_watch", "compiled": False,
                    "runtime_validation": "not_performed"}

    def queue_key(self, expected_process: dict, virtual_key: int,
                  expected_window: int | None = None) -> dict:
        with self._observation_lock:
            if self._closed:
                raise DevelopmentError("service_closed", "The development service is already closed")
            expected = self._expected_process(expected_process, self.process_state())
            try:
                return queue_key(expected, virtual_key, expected_window=expected_window)
            except WindowInputError as error:
                raise DevelopmentError(error.code, str(error),
                                       outcome_unknown=error.outcome_unknown,
                                       receipt=error.receipt) from error

    def stop(self, expected_process: dict) -> dict:
        with self._observation_lock:
            if self._closed:
                raise DevelopmentError("service_closed", "The observation workflow is already closed")
            return self._stop(expected_process)

    def _stop(self, expected_process: dict) -> dict:
        expected = self._expected_process(expected_process, self.process_state(), allow_bootstrap=True)
        response = self._command("stop_g1r_development.ps1", ["-GameRoot", str(self.game),
                                 "-ExpectedPid", str(expected["pid"]),
                                 "-ExpectedCreationTime", expected["creation_time"]], mutating=True)
        if response.get("state") != "stopped" or response.get("process") != expected:
            raise DevelopmentError("tool_protocol_error", "The stop response did not confirm termination of the original process",
                                   outcome_unknown=True)
        return {"state": "stop_result", "process": expected, "tool_result": response,
                "outcome_unknown": False}

    def load_native(self, expected_process: dict, expected_dll_sha256: str) -> dict:
        with self._observation_lock:
            if self._closed:
                raise DevelopmentError("service_closed", "The observation workflow is already closed")
            return self._load_native(expected_process, expected_dll_sha256)

    def _load_native(self, expected_process: dict, expected_dll_sha256: str) -> dict:
        if not isinstance(expected_dll_sha256, str) or not _SHA.fullmatch(expected_dll_sha256):
            raise DevelopmentError("invalid_dll_hash", "The verified DLL SHA-256 is required")
        expected = self._expected_process(expected_process, self.process_state())
        try:
            dll = _checked(self.repository / "build" / "native" / "Release" / "G1R_MCP.dll", self.repository)
            data = _read_bytes(dll, self.repository, MAX_FILE_BYTES)
            actual = hashlib.sha256(data).hexdigest()
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        if actual != expected_dll_sha256.lower():
            raise DevelopmentError("dll_changed", "The DLL changed after its SHA-256 was obtained")
        response = self._command("load_native.ps1", ["-GameRoot", str(self.game),
                                 "-ExpectedPid", str(expected["pid"]),
                                 "-ExpectedCreationTime", expected["creation_time"],
                                 "-ExpectedDllSha256", actual], mutating=True)
        if response.get("state") != "loaded" or response.get("process") != expected or response.get("dll_sha256") != actual:
            raise DevelopmentError("tool_protocol_error", "The loader response did not confirm the selected process and DLL",
                                   outcome_unknown=True)
        return {"state": "load_result", "process": expected, "dll_sha256": actual,
                "tool_result": response, "game_ready": False, "outcome_unknown": False}
