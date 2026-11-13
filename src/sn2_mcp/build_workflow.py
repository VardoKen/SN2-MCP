"""Fixed native CMake builds with exact inputs, reports and process-tree receipts."""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
from typing import Callable
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sn2_mcp.workspace import WorkspaceError, _checked, _destination_lock, _guard_directories, _read_bytes
else:
    from .workspace import WorkspaceError, _checked, _destination_lock, _guard_directories, _read_bytes


class BuildError(Exception):
    """A predictable workspace/build error; a receipt may identify partial effects."""

    def __init__(self, code: str, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.code = code
        self.receipt = receipt


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _positive(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise BuildError("invalid_argument", f"{name} must be a finite positive number")
    return float(value)


class _BasicLimits(ctypes.Structure):
    _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t),
                ("maximum", ctypes.c_size_t), ("process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                ("scheduling", wintypes.DWORD)]


class _IOCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in
                ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", _IOCounters),
                ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]


class _Accounting(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int64) for name in
                ("user", "kernel", "period_user", "period_kernel")] + [
                (name, wintypes.DWORD) for name in
                ("page_faults", "total_processes", "active_processes", "terminated_processes")]


class _WindowsJob:
    """Anonymous, non-inheritable job; ownership never depends on a reused PID."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            ("QueryInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
        ):
            function = getattr(self.kernel, name)
            function.argtypes, function.restype = args, result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(error)

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def active(self) -> int:
        information = _Accounting()
        if not self.kernel.QueryInformationJobObject(self.handle, 1, ctypes.byref(information), ctypes.sizeof(information), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return int(information.active_processes)

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


# The wrapper cannot create build children until its parent assigns it to the job.
_GATE = """import sys,json,subprocess
from pathlib import Path
payload=json.load(sys.stdin)
result=subprocess.run(payload['argv'],cwd=payload['cwd'],env=payload['env'],stdin=subprocess.DEVNULL,shell=False)
Path(payload['exit_path']).write_text(str(result.returncode),encoding='ascii')
sys.exit(result.returncode)
"""


def run_build_command(arguments: list[str], cwd: Path, timeout_seconds: float,
                      stdout_path: Path, stderr_path: Path) -> dict:
    """Run one declared command; retain full bytes and confirm its entire job exited."""
    _positive(timeout_seconds, "timeout_seconds")
    started = time.monotonic()
    receipt = {"arguments": arguments, "cwd": str(cwd), "started_at": _now(),
               "state": "starting", "exit_code": None, "timed_out": False,
               "interrupted": False, "containment_assigned": False,
               "termination_requested": False, "tree_exit_confirmed": False,
               "output_complete": False}
    if os.name != "nt":
        raise BuildError("unsupported_environment", "Native build process containment requires Windows")
    job = None
    process = None
    threads = []
    drain_errors = []
    interrupt = None
    streams = []
    exit_path = stdout_path.with_name(stdout_path.name + ".command-exit")
    try:
        job = _WindowsJob()
        streams.append(stdout_path.open("wb"))
        streams.append(stderr_path.open("wb"))
        # Avoid the Windows venv redirector creating an interpreter child before assignment.
        interpreter = getattr(sys, "_base_executable", sys.executable)
        process = subprocess.Popen([interpreter, "-I", "-S", "-c", _GATE],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False, creationflags=subprocess.CREATE_NO_WINDOW)
        job.assign(process)
        receipt["containment_assigned"] = True

        def drain(source, destination):
            try:
                while raw := source.read(65536):
                    destination.write(raw)
                destination.flush()
            except OSError as error:
                drain_errors.append({"errno": error.errno, "winerror": getattr(error, "winerror", None)})
            finally:
                source.close()

        for source, destination in zip((process.stdout, process.stderr), streams):
            thread = threading.Thread(target=drain, args=(source, destination), daemon=True)
            thread.start()
            threads.append(thread)
        environment = os.environ.copy()
        environment.update(MSBUILDDISABLENODEREUSE="1", VSLANG="1033")
        process.stdin.write(json.dumps({"argv": arguments, "cwd": str(cwd), "env": environment,
                                       "exit_path": str(exit_path)}).encode("utf-8"))
        process.stdin.close()
        try:
            process.wait(timeout=max(0.001, timeout_seconds - (time.monotonic() - started)))
            receipt["state"] = "succeeded" if process.returncode == 0 else "failed"
        except subprocess.TimeoutExpired:
            receipt.update(state="timed_out", timed_out=True)
        except BaseException as error:
            receipt.update(state="interrupted", interrupted=True)
            interrupt = error
    except OSError as error:
        receipt.update(state="execution_error", error={"errno": error.errno, "winerror": getattr(error, "winerror", None)})
    except BaseException as error:
        receipt.update(state="interrupted", interrupted=True)
        interrupt = error
    finally:
        if process is not None:
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                if receipt["containment_assigned"]:
                    active = job.active()
                    if active:
                        receipt["termination_requested"] = True
                        receipt["remaining_workers_after_root_exit"] = active if process.poll() is not None else None
                        job.terminate()
                    process.wait(timeout=10)
                    end = time.monotonic() + 10
                    active = job.active()
                    while active and time.monotonic() < end:
                        time.sleep(0.01)
                        active = job.active()
                    receipt["job_active_processes_after"] = active
                    receipt["tree_exit_confirmed"] = active == 0
                else:
                    # The startup gate has not been released; no build child exists.
                    process.terminate()
                    process.wait(timeout=10)
                    receipt["tree_exit_confirmed"] = True
            except (OSError, subprocess.TimeoutExpired) as error:
                receipt["cleanup_error"] = {"errno": getattr(error, "errno", None), "winerror": getattr(error, "winerror", None)}
            receipt["root_exit_code"] = process.poll()
            if exit_path.exists():
                try:
                    receipt["exit_code"] = int(exit_path.read_text(encoding="ascii"))
                    receipt["command_completed"] = True
                except (OSError, ValueError):
                    receipt["command_completed"] = False
            else:
                receipt["command_completed"] = False
                if receipt["state"] == "succeeded":
                    receipt["state"] = "command_exit_unconfirmed"
        if job is not None:
            job.close()
        for thread in threads:
            thread.join(timeout=10)
        for stream in streams:
            if not any(thread.is_alive() for thread in threads):
                stream.close()
        receipt["output_complete"] = bool(streams) and not drain_errors and not any(thread.is_alive() for thread in threads)
        if drain_errors:
            receipt["output_errors"] = drain_errors
        receipt["elapsed_seconds"] = time.monotonic() - started
        receipt["finished_at"] = _now()
        for key, path in (("stdout", stdout_path), ("stderr", stderr_path)):
            if path.is_file() and receipt["output_complete"]:
                raw = path.read_bytes()
                receipt[key] = {"path": str(path), "bytes": len(raw), "sha256": _sha(raw)}
        if not receipt["tree_exit_confirmed"] or not receipt["output_complete"]:
            receipt["state"] = "cleanup_unconfirmed"
    if interrupt is not None:
        raise BuildError("interrupted", "Build command interrupted; inspect its cleanup receipt", receipt) from interrupt
    return receipt


BuildRunner = Callable[[list[str], Path, float, Path, Path], dict]


@contextmanager
def _guard_input_files(paths: list[Path]):
    """Keep staged input bytes immutable to writers/renames for the compilation interval."""
    handles = []
    try:
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
            create.restype = wintypes.HANDLE
            close = kernel.CloseHandle
            close.argtypes, close.restype = [wintypes.HANDLE], wintypes.BOOL
            for path in paths:
                handle = create(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise BuildError("input_lock_failed", "Could not retain a read-only staged build input")
                handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            close(handle)


class BuildWorkflow:
    """Build only the repository's declared native subproject, without a game dependency."""

    def __init__(self, repository_root: Path, *, workspace_root: Path | None = None,
                 runner: BuildRunner = run_build_command, diagnostics_parser=None):
        self.root = _checked(Path(repository_root).absolute(), directory=True)
        self.workspace_root = _checked(Path(workspace_root).absolute(), directory=True) if workspace_root is not None else self.root
        self.runner = runner
        self.diagnostics_parser = diagnostics_parser
        if not (self.root / "native" / "CMakeLists.txt").is_file():
            raise BuildError("missing_build_project", "The selected repository has no native CMake subproject")

    def _project(self, project_path: str) -> Path:
        if not isinstance(project_path, str) or not project_path or "\\" in project_path:
            raise BuildError("invalid_project", "project_path must be a relative repository path using forward slashes")
        parts = project_path.split("/")
        if any(part in {"", ".", ".."} or ":" in part for part in parts):
            raise BuildError("invalid_project", "project_path must identify a repository CMake project")
        if project_path != "native":
            raise BuildError("unsupported_project", "Native builds support the repository native bridge project; use sn2_cook_mod for Content/Blueprint mods")
        project = _checked(self.root.joinpath(*parts), self.root, directory=True)
        _checked(project / "CMakeLists.txt", project)
        return project

    def _inputs(self, project_path: str = "native") -> tuple[list[dict], dict[str, bytes]]:
        if project_path != "native":
            raise BuildError("unsupported_project", "Native builds support the repository native bridge project; use sn2_cook_mod for Content/Blueprint mods")
        entries = {}
        locations = {}
        native = _checked(self.root / "native", self.root, directory=True)
        roots = [(native, self.root, "native", "repository")]
        for selected, boundary, prefix, origin in roots:
            with _guard_directories(selected, boundary):
                def fail(error):
                    raise BuildError("input_access_error", "Could not enumerate every native build input") from error
                for directory, children, names in os.walk(selected, followlinks=False, onerror=fail):
                    parent = _checked(Path(directory), selected, directory=True)
                    children[:] = sorted(name for name in children if name not in
                                         {".git", ".venv", "artifacts", "build", "__pycache__", "nppBackup"})
                    for child in children:
                        _checked(parent / child, selected, directory=True)
                    for name in sorted(names):
                        path = _checked(parent / name, selected)
                        relative = prefix + "/" + path.relative_to(selected).as_posix()
                        entries[relative] = _read_bytes(path, boundary, None)
                        locations[relative] = {"origin": origin, "source_path": str(path)}
        for name in ("pyproject.toml", "uv.lock", "tools/build_native.ps1", "src/sn2_mcp/build_workflow.py"):
            path = self.root / name
            if path.exists():
                entries[name] = _read_bytes(path, self.root, None)
                locations[name] = {"origin": "repository", "source_path": str(path)}
        inventory = [{"path": path, "sha256": _sha(raw), "bytes": len(raw),
                      "role": "native_input" if path.startswith("native/") else
                              "build_orchestration_provenance" if path.startswith(("tools/", "src/")) else "package_provenance",
                      **locations[path]}
                     for path, raw in sorted(entries.items())]
        return inventory, entries

    def _git(self) -> dict:
        def read(*arguments):
            try:
                result = subprocess.run(["git", "-C", str(self.root), *arguments],
                                        capture_output=True, timeout=30, shell=False,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode == 0:
                    return result.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
            except (OSError, subprocess.TimeoutExpired):
                pass
            return None
        return {"head": read("rev-parse", "HEAD"), "status_porcelain": read("status", "--porcelain=v1"),
                "source": "git_command", "compiled_revision": False}

    def recipe(self, target: str = "all", configuration: str = "Release", run_tests: bool = True,
               parallel: int | None = None, generator: str = "Visual Studio 17 2022", project_path: str = "native") -> dict:
        if configuration not in {"Release", "Debug"} or not isinstance(run_tests, bool):
            raise BuildError("invalid_argument", "Configuration must be Release or Debug and run_tests must be Boolean")
        if generator != "Visual Studio 17 2022":
            raise BuildError("unsupported_generator", "The inherited native build uses Visual Studio 17 2022")
        if parallel is not None and (isinstance(parallel, bool) or not isinstance(parallel, int) or parallel <= 0):
            raise BuildError("invalid_argument", "parallel must be a positive integer")
        self._project(project_path)
        inputs, files = self._inputs(project_path)
        staged_project_path = "native"
        cmake = files[staged_project_path + "/CMakeLists.txt"].decode("utf-8")
        targets = sorted(set(re.findall(r"add_(?:library|executable)\s*\(\s*([A-Za-z0-9_]+)", cmake, re.I)))
        if target != "all" and target not in targets:
            raise BuildError("unknown_target", "The selected target is not declared by the selected CMake project")
        tools = {}
        for name in ("cmake", "ctest"):
            found = shutil.which(name)
            tools[name] = {"path": found, "available": found is not None, "version": None}
            if found:
                raw = Path(found).read_bytes()
                tools[name].update(bytes=len(raw), sha256=_sha(raw))
                try:
                    result = subprocess.run([found, "--version"], capture_output=True, shell=False, timeout=30,
                                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    tools[name]["version"] = result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None
                except (OSError, subprocess.TimeoutExpired):
                    tools[name]["version"] = None
        dependencies = [{"name": name, "repository": url, "declared_revision": revision}
                        for name, url, revision in re.findall(
                            r"FetchContent_Declare\(\s*(\w+)\s+GIT_REPOSITORY\s+([^\s)]+)\s+GIT_TAG\s+([^\s)]+)",
                            cmake + "\n" + files["native/CMakeLists.txt"].decode("utf-8"))]
        dependencies = list({item["name"]: item for item in dependencies}.values())
        content_manifest = [{key: item[key] for key in ("path", "sha256", "bytes", "role")} for item in inputs]
        digest = _sha(json.dumps(content_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        test_targets = sorted(set(re.findall(r"add_test\(NAME\s+\S+\s+COMMAND\s+(\w+)", cmake)))
        return {"source": "native_build_recipe", "workspace_root": str(self.workspace_root),
                "repository_root": str(self.root), "project_path": project_path, "staged_project_path": staged_project_path,
                "target": target, "declared_targets": targets, "configuration": configuration,
                "generator": generator, "architecture": "x64", "run_tests": run_tests,
                "test_targets": test_targets, "parallel": parallel,
                "inputs": inputs, "input_manifest_sha256": digest, "git": self._git(),
                "toolchain": tools, "dependencies": dependencies, "compiled": False,
                "runtime_validation": "not_performed"}

    def _directory(self, path: Path) -> Path:
        parent = path
        missing = []
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        _checked(parent, self.root, directory=True)
        for item in reversed(missing):
            item.mkdir()
            _checked(item, self.root, directory=True)
        return _checked(path, self.root, directory=True)

    def _stage(self, recipe: dict, build_id: str) -> tuple[Path, Path]:
        # MSVC FileTracker still uses MAX_PATH for some generated tlog paths.
        # A shortened location is verified against the complete content manifest.
        directory = self.root / "build" / "mcp" / recipe["input_manifest_sha256"][:16]
        source, output = directory / "s", directory / ("b" + build_id[:8])
        self._directory(directory)
        manifest = directory / "manifest.json"
        if manifest.exists():
            value = json.loads(_read_bytes(manifest, self.root, None))
            if value["input_manifest_sha256"] != recipe["input_manifest_sha256"]:
                raise BuildError("build_path_collision", "The build location belongs to another complete input manifest")
        else:
            _write_json(manifest, {"input_manifest_sha256": recipe["input_manifest_sha256"]})
        self._directory(source)
        if output.exists():
            raise BuildError("build_path_collision", "The operation build directory already exists")
        self._directory(output)
        inventory, files = self._inputs(recipe["project_path"])
        if inventory != recipe["inputs"]:
            raise BuildError("source_changed", "Source inputs changed before build staging; request a fresh recipe")
        for relative, raw in files.items():
            destination = source / relative
            self._directory(destination.parent)
            if destination.exists():
                if _read_bytes(destination, source, None) != raw:
                    raise BuildError("staging_changed", "A previously staged input differs from its manifest")
            else:
                with destination.open("xb") as stream:
                    stream.write(raw)
        if not self._staged_unchanged(recipe, source):
            raise BuildError("staging_changed", "The staged build tree contains missing, changed or additional inputs")
        return source, output

    def _staged_unchanged(self, recipe: dict, source: Path) -> bool:
        try:
            found = set()
            for directory, children, names in os.walk(source, followlinks=False):
                parent = _checked(Path(directory), source, directory=True)
                for child in children:
                    _checked(parent / child, source, directory=True)
                for name in names:
                    path = _checked(parent / name, source)
                    found.add(path.relative_to(source).as_posix())
            if found != {item["path"] for item in recipe["inputs"]}:
                return False
            return all(_sha(_read_bytes(source / item["path"], source, None)) == item["sha256"]
                       for item in recipe["inputs"])
        except WorkspaceError:
            return False

    def _dependencies(self, output: Path) -> list[dict]:
        result = []
        root = output / "_deps"
        if not root.exists():
            return result
        for checkout in sorted(root.glob("*-src")):
            checked = _checked(checkout, self.root, directory=True)
            inventory = []
            for directory, children, names in os.walk(checked, followlinks=False):
                parent = _checked(Path(directory), checked, directory=True)
                children[:] = sorted(name for name in children if name != ".git")
                for child in children:
                    _checked(parent / child, checked, directory=True)
                for name in sorted(names):
                    if name == ".git":
                        continue
                    path = _checked(parent / name, checked)
                    raw = _read_bytes(path, checked, None)
                    inventory.append({"path": path.relative_to(checked).as_posix(), "sha256": _sha(raw), "bytes": len(raw)})
            result.append({"name": checkout.name, "path": str(checked), "files": inventory,
                           "manifest_sha256": _sha(json.dumps(inventory, sort_keys=True).encode("utf-8"))})
        return result

    def _dependency_checkout(self, path: Path, declared: dict) -> tuple[dict, dict[str, bytes]] | None:
        """Admit an existing checkout only when origin, declared commit and clean HEAD agree."""
        if not path.is_dir():
            return None
        checked = _checked(path, self.root, directory=True)
        def git(*arguments):
            try:
                result = subprocess.run(["git", "-C", str(checked), *arguments], capture_output=True,
                                        shell=False, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return result.stdout.decode("utf-8", errors="replace").strip() if result.returncode == 0 else None
            except (OSError, subprocess.TimeoutExpired):
                return None
        origin, head = git("remote", "get-url", "origin"), git("rev-parse", "HEAD")
        resolved = git("rev-parse", declared["declared_revision"] + "^{commit}")
        status = git("status", "--porcelain=v1", "--untracked-files=all")
        if origin != declared["repository"] or not head or head != resolved or status != "":
            return None
        files = {}
        def fail(error):
            raise BuildError("dependency_access_error", "Could not enumerate a reused dependency") from error
        for directory, children, names in os.walk(checked, followlinks=False, onerror=fail):
            parent = _checked(Path(directory), checked, directory=True)
            children[:] = sorted(name for name in children if name not in {".git", "nppBackup", "__pycache__"})
            for child in children:
                _checked(parent / child, checked, directory=True)
            for name in sorted(names):
                if name == ".git":
                    continue
                entry = _checked(parent / name, checked)
                files[entry.relative_to(checked).as_posix()] = _read_bytes(entry, checked, None)
        inventory = [{"path": relative, "sha256": _sha(raw), "bytes": len(raw)} for relative, raw in sorted(files.items())]
        return {"name": declared["name"], "source_checkout": str(checked), "repository": origin,
                "declared_revision": declared["declared_revision"], "resolved_revision": resolved,
                "head": head, "status_porcelain": status, "files": inventory,
                "manifest_sha256": _sha(json.dumps(inventory, sort_keys=True).encode("utf-8")),
                "method": "existing_declared_git_checkout"}, files

    def _cached_dependencies(self, recipe: dict) -> list[dict]:
        """Reuse exact local inputs; omit unavailable cache entries and let CMake fetch normally."""
        reused = []
        cache_root = self.root / "build" / "dc"
        for declared in recipe["dependencies"]:
            existing = []
            if cache_root.exists():
                for manifest in sorted(cache_root.glob(declared["name"] + "-*/*/manifest.json")):
                    cached = json.loads(_read_bytes(manifest, self.root, None))
                    if (cached.get("repository") != declared["repository"] or cached.get("declared_revision") != declared["declared_revision"]
                            or cached.get("head") != cached.get("resolved_revision") or cached.get("status_porcelain") != ""):
                        continue
                    source = _checked(manifest.parent / "s", self.root, directory=True)
                    expected = {item["path"]: item for item in cached["files"]}
                    actual = set()
                    for directory, children, names in os.walk(source, followlinks=False):
                        parent = _checked(Path(directory), source, directory=True)
                        for child in children:
                            _checked(parent / child, source, directory=True)
                        for name in names:
                            path = _checked(parent / name, source)
                            actual.add(path.relative_to(source).as_posix())
                    if actual != set(expected):
                        raise BuildError("dependency_cache_changed", "The cached dependency contains additional or missing files")
                    for relative, entry in expected.items():
                        raw = _read_bytes(source / relative, source, None)
                        if _sha(raw) != entry["sha256"] or len(raw) != entry["bytes"]:
                            raise BuildError("dependency_cache_changed", "A cached dependency input differs from its manifest")
                    digest = _sha(json.dumps(cached["files"], sort_keys=True).encode("utf-8"))
                    if digest != cached["manifest_sha256"]:
                        raise BuildError("dependency_cache_changed", "The cached dependency file inventory differs from its identity")
                    existing.append({**cached, "path": str(source), "cache_manifest": str(manifest),
                                     "cmake_variable": "FETCHCONTENT_SOURCE_DIR_" + declared["name"].upper(),
                                     "method": "verified_existing_dependency_cache"})
            if existing:
                revisions = {(item["resolved_revision"], item["manifest_sha256"]) for item in existing}
                if len(revisions) != 1:
                    raise BuildError("dependency_cache_ambiguous", "The declared dependency has multiple different cached input revisions")
                reused.append(existing[0])
                continue
            admitted = None
            candidates = [self.root / "build" / "native" / "_deps" / (declared["name"] + "-src")]
            build_root = self.root / "build" / "mcp"
            if build_root.exists():
                candidates.extend(sorted(build_root.glob("*/b*/_deps/" + declared["name"] + "-src")))
            for candidate in candidates:
                admitted = self._dependency_checkout(candidate, declared)
                if admitted is not None:
                    break
            if admitted is None:
                continue
            fact, files = admitted
            location = cache_root / (declared["name"] + "-" + fact["resolved_revision"][:12]) / fact["manifest_sha256"][:12]
            self._directory(location)
            manifest = location / "manifest.json"
            if manifest.exists():
                cached = json.loads(_read_bytes(manifest, self.root, None))
                if cached["manifest_sha256"] != fact["manifest_sha256"] or cached["resolved_revision"] != fact["resolved_revision"]:
                    raise BuildError("dependency_cache_collision", "A dependency cache location belongs to different exact inputs")
            else:
                _write_json(manifest, fact)
            source = self._directory(location / "s")
            for relative, raw in files.items():
                destination = source / relative
                self._directory(destination.parent)
                if destination.exists():
                    if _read_bytes(destination, source, None) != raw:
                        raise BuildError("dependency_cache_changed", "A cached dependency input differs from its manifest")
                else:
                    with destination.open("xb") as stream:
                        stream.write(raw)
            found = {entry.relative_to(source).as_posix() for entry in source.rglob("*") if entry.is_file()}
            if found != set(files):
                raise BuildError("dependency_cache_changed", "The cached dependency contains additional or missing files")
            reused.append({**fact, "path": str(source), "cache_manifest": str(manifest),
                           "cmake_variable": "FETCHCONTENT_SOURCE_DIR_" + declared["name"].upper()})
        return reused

    def _actual_dependencies(self, output: Path, reused: list[dict]) -> list[dict]:
        dependencies = self._dependencies(output)
        for selected in reused:
            source = Path(selected["path"])
            inventory = []
            for expected in selected["files"]:
                raw = _read_bytes(source / expected["path"], source, None)
                inventory.append({"path": expected["path"], "sha256": _sha(raw), "bytes": len(raw)})
            dependencies.append({"name": selected["name"] + "-src", "path": str(source), "files": inventory,
                                 "manifest_sha256": _sha(json.dumps(inventory, sort_keys=True).encode("utf-8")),
                                 "resolved_revision": selected["resolved_revision"], "repository": selected["repository"],
                                 "method": "verified_local_dependency_cache"})
        return dependencies

    def _artifacts(self, recipe: dict, output: Path) -> list[dict]:
        reply = output / ".cmake" / "api" / "v1" / "reply"
        indexes = sorted(reply.glob("index-*.json"))
        if not indexes:
            raise BuildError("missing_artifact_manifest", "CMake did not provide its target artifact manifest")
        index = json.loads(_read_bytes(indexes[-1], self.root, None))
        code = index["reply"]["codemodel-v2"]["jsonFile"]
        model = json.loads(_read_bytes(reply / code, self.root, None))
        artifacts = []
        for configuration in model["configurations"]:
            if configuration["name"] != recipe["configuration"]:
                continue
            for target in configuration["targets"]:
                if recipe["target"] != "all" and target["name"] != recipe["target"]:
                    continue
                detail = json.loads(_read_bytes(reply / target["jsonFile"], self.root, None))
                for artifact in detail.get("artifacts", []):
                    # CMake lists optional MSVC PDBs even when /DEBUG is disabled.
                    if Path(artifact["path"]).suffix.lower() == ".pdb" and not (output / artifact["path"]).exists():
                        artifacts.append({"target": target["name"], "path": str(output / artifact["path"]),
                                          "available": False, "required": False, "role": "debug_symbols"})
                        continue
                    path = _checked(output / artifact["path"], output)
                    raw = _read_bytes(path, output, None)
                    pe = {"format": "not_pe", "machine": None, "kind": None}
                    if raw[:2] == b"MZ" and len(raw) >= 64:
                        offset = struct.unpack_from("<I", raw, 60)[0]
                        if offset + 24 <= len(raw) and raw[offset:offset + 4] == b"PE\0\0":
                            machine = struct.unpack_from("<H", raw, offset + 4)[0]
                            flags = struct.unpack_from("<H", raw, offset + 22)[0]
                            pe = {"format": "PE", "machine": machine, "architecture": "x64" if machine == 0x8664 else "other",
                                  "kind": "dll" if flags & 0x2000 else "executable"}
                    artifacts.append({"target": target["name"], "path": str(path), "bytes": len(raw), "available": True,
                                      "sha256": _sha(raw), "pe": pe, "authoritative_build_output": True})
        return artifacts

    def read_report(self, build_id: str, stage: str, stream: str = "stdout") -> dict:
        receipt = self.status(build_id)
        if stream not in {"stdout", "stderr"} or stage not in receipt.get("stages", {}):
            raise BuildError("unknown_report", "The selected build stage or stream has no report")
        location = receipt["stages"][stage].get(stream)
        if not location:
            raise BuildError("unknown_report", "The selected stream has no completed report")
        raw = _read_bytes(Path(location["path"]), self.root / "artifacts" / "builds", None)
        return {"path": location["path"], "sha256": _sha(raw), "receipt_sha256": location["sha256"],
                "receipt_matches": _sha(raw) == location["sha256"], "bytes": len(raw),
                "text": raw.decode("utf-8", errors="replace"), "text_encoding": "utf-8", "decode_errors": "replace",
                "raw_report_available": True, "truncated": False}

    def status(self, build_id: str | None = None) -> dict:
        reports = self.root / "artifacts" / "builds"
        if build_id is None:
            pointer = reports / "latest.json"
            if not pointer.exists():
                return {"source": "native_build_status", "state": "not_built", "resident_worker": False}
            build_id = json.loads(_read_bytes(pointer, self.root, None))["build_id"]
        if not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{32}", build_id):
            raise BuildError("invalid_build_id", "build_id must be the returned build identifier")
        path = reports / build_id / "receipt.json"
        if not path.exists():
            raise BuildError("unknown_build", "The selected build receipt does not exist")
        receipt = json.loads(_read_bytes(path, self.root, None))
        receipt["receipt_path"] = str(path)
        receipt["resident_worker"] = False
        return receipt

    def diagnostics(self, build_id: str | None = None) -> dict:
        receipt = self.status(build_id)
        reports = []
        for report in receipt.get("compiler_reports", []):
            current = self.read_report(receipt["build_id"], report["stage"], report["stream"])
            reports.append({**report, "raw_report_receipt_matches": current["receipt_matches"]})
        return {"source": "native_compile_diagnostics", "build_id": receipt.get("build_id"),
                "build_state": receipt["state"], "reports": reports,
                "exact_source_compiled": receipt.get("exact_source_compiled", False),
                "compiled": False, "runtime_validation": "not_performed"}

    def build(self, target: str = "all", configuration: str = "Release", run_tests: bool = True,
              timeout_seconds: float = 900, parallel: int | None = None,
              generator: str = "Visual Studio 17 2022", project_path: str = "native") -> dict:
        timeout_seconds = _positive(timeout_seconds, "timeout_seconds")
        recipe = self.recipe(target, configuration, run_tests, parallel, generator, project_path)
        if not all(tool["available"] for tool in recipe["toolchain"].values()):
            raise BuildError("missing_toolchain", "CMake and CTest must be available in the inherited build environment")
        started = time.monotonic()
        build_id = uuid.uuid4().hex
        directory = self._directory(self.root / "artifacts" / "builds" / build_id)
        path = directory / "receipt.json"
        receipt = {"source": "native_build", "build_id": build_id, "receipt_path": str(path),
                   "state": "preparing", "started_at": _now(), "timeout_seconds": timeout_seconds,
                   "recipe": recipe, "stages": {name: {"state": "not_started", "exit_code": None}
                                                for name in ("configure", "build", "test")}, "artifacts": [], "publication": [],
                   "compiled": False, "exact_source_compiled": False, "resident_worker": False,
                   "runtime_validation": "not_performed"}
        _write_json(path, receipt)
        _write_json(directory.parent / "latest.json", {"build_id": build_id})
        source = None
        try:
            with _destination_lock(self.root / "build" / "mcp"), ExitStack() as guards:
                source, output = self._stage(recipe, build_id)
                receipt.update(staged_source_root=str(source), build_directory=str(output))
                query = self._directory(output / ".cmake" / "api" / "v1" / "query")
                (query / "codemodel-v2").touch()
                cmake, ctest = recipe["toolchain"]["cmake"]["path"], recipe["toolchain"]["ctest"]["path"]
                def stage(name, arguments):
                    remaining = timeout_seconds - (time.monotonic() - started)
                    if remaining <= 0:
                        receipt["stages"][name] = {"state": "not_started", "reason": "build_deadline_exhausted"}
                        receipt["state"] = "timed_out"
                        return False
                    receipt["state"] = name
                    _write_json(path, receipt)
                    for tool in recipe["toolchain"].values():
                        if _sha(Path(tool["path"]).read_bytes()) != tool["sha256"]:
                            raise BuildError("toolchain_changed", "A selected build executable changed after the recipe was recorded")
                    try:
                        result = self.runner(arguments, self.root, remaining, directory / f"{name}.stdout.log", directory / f"{name}.stderr.log")
                    except BuildError as error:
                        if error.receipt:
                            receipt["stages"][name] = error.receipt
                        raise
                    receipt["stages"][name] = result
                    _write_json(path, receipt)
                    if result["state"] != "succeeded":
                        receipt["state"] = result["state"]
                        return False
                    return True
                if not stage("toolchain_cmake", [cmake, "--version"]):
                    return self._finish(receipt, path, source, started)
                if not stage("toolchain_ctest", [ctest, "--version"]):
                    return self._finish(receipt, path, source, started)
                guards.enter_context(_guard_input_files([source / item["path"] for item in recipe["inputs"]]))
                configure = [cmake, "-S", str(source / recipe["staged_project_path"]), "-B", str(output), "-G", generator, "-A", "x64"]
                reused = self._cached_dependencies(recipe)
                receipt["dependency_reuse"] = reused
                guards.enter_context(_guard_input_files([Path(dependency["path"]) / item["path"]
                                                       for dependency in reused for item in dependency["files"]]))
                configure.extend("-D" + dependency["cmake_variable"] + "=" + dependency["path"] for dependency in reused)
                if not stage("configure", configure):
                    return self._finish(receipt, path, source, started)
                receipt["dependencies_before_build"] = self._actual_dependencies(output, reused)
                guards.enter_context(_guard_input_files([Path(dependency["path"]) / item["path"]
                                                       for dependency in receipt["dependencies_before_build"]
                                                       for item in dependency["files"]]))
                receipt["input_files_write_protected_during_build"] = os.name == "nt"
                receipt["configured_toolchain"] = self._configured_toolchain(output)
                command = [cmake, "--build", str(output), "--config", configuration, "--parallel"]
                if parallel is not None:
                    command.append(str(parallel))
                if target != "all":
                    command.extend(["--target", *sorted(set([target, *(recipe["test_targets"] if run_tests else [])]))])
                command.extend(["--", "/nodeReuse:false"])
                if not stage("build", command):
                    return self._finish(receipt, path, source, started)
                if run_tests:
                    if not stage("test", [ctest, "--test-dir", str(output), "-C", configuration, "--output-on-failure"]):
                        return self._finish(receipt, path, source, started)
                else:
                    receipt["stages"]["test"] = {"state": "not_requested", "exit_code": None}
                receipt["dependencies_after_build"] = self._actual_dependencies(output, reused)
                receipt["dependencies_stable"] = receipt["dependencies_before_build"] == receipt["dependencies_after_build"]
                receipt["artifacts"] = self._artifacts(recipe, output)
                if not any(item.get("available") for item in receipt["artifacts"]):
                    raise BuildError("missing_build_artifact", "The selected build target produced no identified artifact")
                receipt["compiled"] = True
                receipt["exact_source_compiled"] = self._staged_unchanged(recipe, source) and receipt["dependencies_stable"]
                receipt["state"] = "succeeded" if receipt["exact_source_compiled"] else "inputs_changed"
                if receipt["state"] == "succeeded":
                    self._publish(receipt)
        except BuildError as error:
            receipt["state"] = error.code
            receipt["error"] = {"code": error.code, "message": str(error), "command_receipt": error.receipt}
        except (WorkspaceError, OSError, ValueError, KeyError) as error:
            receipt["state"] = "evidence_error"
            receipt["error"] = {"code": getattr(error, "code", "evidence_error"), "message": str(error) if isinstance(error, WorkspaceError) else "Could not establish complete native build evidence"}
        except BaseException:
            receipt["state"] = "interrupted"
            self._finish(receipt, path, source, started)
            raise
        return self._finish(receipt, path, source, started)

    def _publish(self, receipt: dict):
        configuration = receipt["recipe"]["configuration"]
        for artifact in receipt["artifacts"]:
            if Path(artifact["path"]).name not in {"SN2_MCP.dll", "sn2_mcp_loader.exe"}:
                continue
            source = Path(artifact["path"])
            destination = self.root / "build" / "native" / configuration / source.name
            self._directory(destination.parent)
            if destination.exists():
                _checked(destination, self.root)
            raw = _read_bytes(source, self.root, None)
            if _sha(raw) != artifact["sha256"]:
                raise BuildError("artifact_changed", "The build output changed before workspace publication")
            if not destination.exists() or _read_bytes(destination, self.root, None) != raw:
                temporary = destination.with_name(destination.name + "." + receipt["build_id"] + ".pending")
                with temporary.open("xb") as stream:
                    stream.write(raw)
                try:
                    os.replace(temporary, destination)
                except OSError:
                    # Preserve the failed publication file as task evidence, not as a pending output.
                    os.replace(temporary, Path(receipt["receipt_path"]).parent / (source.name + ".publication-failed"))
                    raise
            published = _read_bytes(destination, self.root, None)
            receipt["publication"].append({"path": str(destination), "source_path": str(source),
                                            "sha256": _sha(published), "matches_build_output": published == raw,
                                            "effect": "workspace_build_output_available", "installed_in_game": False})

    def _configured_toolchain(self, output: Path) -> dict:
        cache = output / "CMakeCache.txt"
        if not cache.exists():
            return {"available": False}
        raw = _read_bytes(cache, self.root, None)
        values = dict(re.findall(r"^(CMAKE_(?:C_COMPILER|CXX_COMPILER|GENERATOR|GENERATOR_PLATFORM|VS_PLATFORM_TOOLSET|VS_WINDOWS_TARGET_PLATFORM_VERSION)):[^=]*=(.*)$",
                                 raw.decode("utf-8", errors="replace"), re.M))
        compiler_facts = []
        for path in sorted((output / "CMakeFiles").glob("*/CMake*Compiler.cmake")):
            data = _read_bytes(path, self.root, None)
            facts = dict(re.findall(r'set\((CMAKE_(?:C|CXX)_COMPILER(?:_ID|_VERSION)?) "([^"\r\n]*)"\)', data.decode("utf-8", errors="replace")))
            for key, value in tuple(facts.items()):
                if key.endswith("_COMPILER") and Path(value).is_file():
                    compiler = _read_bytes(Path(value), Path(value).parent, None)
                    facts[key + "_SHA256"] = _sha(compiler)
            compiler_facts.append({"path": str(path), "sha256": _sha(data), "facts": facts})
        return {"available": True, "cache_path": str(cache), "cache_sha256": _sha(raw), "cache_values": values,
                "compiler_records": compiler_facts}

    def _finish(self, receipt: dict, path: Path, source: Path | None, started: float) -> dict:
        try:
            final, _ = self._inputs(receipt["recipe"]["project_path"])
            receipt["workspace_inputs_after"] = final
            receipt["workspace_source_stable"] = final == receipt["recipe"]["inputs"]
            receipt["workspace_current_revision_compiled"] = receipt["exact_source_compiled"] and receipt["workspace_source_stable"]
        except (WorkspaceError, BuildError):
            receipt["workspace_source_stable"] = None
            receipt["workspace_current_revision_compiled"] = False
        if source is not None:
            receipt["staged_source_stable"] = self._staged_unchanged(receipt["recipe"], source)
            if not receipt["staged_source_stable"]:
                receipt["exact_source_compiled"] = False
                receipt["workspace_current_revision_compiled"] = False
                if receipt["state"] == "succeeded":
                    receipt["state"] = "inputs_changed"
        parser = self.diagnostics_parser
        if parser is None:
            if __package__ in (None, ""):
                from sn2_mcp.compile_diagnostics import parse_build_report
            else:
                from .compile_diagnostics import parse_build_report
            parser = parse_build_report
        reports = []
        for name, stage in receipt["stages"].items():
            if name not in {"configure", "build", "test"}:
                continue
            for stream in ("stdout", "stderr"):
                if stage.get(stream):
                    raw = _read_bytes(Path(stage[stream]["path"]), self.root, None)
                    if _sha(raw) != stage[stream]["sha256"]:
                        receipt["state"] = "report_changed"
                        receipt["report_integrity_confirmed"] = False
                    keywords = {"source_directory": (source or self.root) / receipt["recipe"]["staged_project_path"]} if self.diagnostics_parser is None else {}
                    reports.append({"stage": name, "stream": stream, "raw_sha256": _sha(raw),
                                    **parser(raw.decode("utf-8", errors="replace"), receipt["recipe"]["inputs"], source or self.root, **keywords)})
        receipt["compiler_reports"] = reports
        receipt.setdefault("report_integrity_confirmed", True)
        receipt["finished_at"] = _now()
        receipt["elapsed_seconds"] = time.monotonic() - started
        _write_json(path, receipt)
        return receipt


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Build the declared native subproject with exact evidence")
    parser.add_argument("--repository-root", type=Path, required=True)
    parser.add_argument("--target", default="all")
    parser.add_argument("--project-path", default="native")
    parser.add_argument("--configuration", default="Release")
    parser.add_argument("--generator", default="Visual Studio 17 2022")
    parser.add_argument("--parallel", type=int, default=4)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--run-tests", choices=("true", "false"), default="true")
    parser.add_argument("--json", choices=("true", "false"), default="false")
    arguments = parser.parse_args()
    try:
        result = BuildWorkflow(arguments.repository_root).build(
            target=arguments.target, configuration=arguments.configuration, generator=arguments.generator,
            parallel=arguments.parallel, timeout_seconds=arguments.timeout_seconds, run_tests=arguments.run_tests == "true",
            project_path=arguments.project_path)
    except (BuildError, WorkspaceError) as error:
        result = {"state": "failed", "error": {"code": error.code, "message": str(error)}, "compiled": False}
    if arguments.json == "true":
        print(json.dumps(result, ensure_ascii=False))
    else:
        print("Native build state: " + result["state"])
        if result.get("receipt_path"):
            print("Receipt: " + result["receipt_path"])
        for name, stage in result.get("stages", {}).items():
            print(name + ": " + stage["state"])
            for stream in ("stdout", "stderr"):
                if stage.get(stream):
                    print(stream + ": " + stage[stream]["path"])
    return 0 if result["state"] == "succeeded" else 1


if __name__ == "__main__":
    sys.exit(main())
