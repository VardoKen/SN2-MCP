"""Create and edit official packages in the selected workspace.

This module does not determine game directories or the official Modkit. Access
covers GothicMod.json and files in the five official package components;
compilation, patch application, and loading are not performed.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import tempfile
import threading
from typing import Iterable, Iterator

from .packages import COMPONENTS, MAX_DESCRIPTOR_BYTES, PackageError, validate_package

MAX_FILE_BYTES = None
MAX_READ_BYTES = None
MAX_EXACT_READ_BYTES = None
MAX_CONTENT_BYTES = None
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)


class WorkspaceError(Exception):
    """Predictable error without disclosing absolute user paths."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _parts(value: str, *, root_allowed: bool = False) -> tuple[str, ...]:
    if not isinstance(value, str) or not value:
        raise WorkspaceError("invalid_path", "A nonempty relative path is required")
    if root_allowed and value == ".":
        return ()
    if PureWindowsPath(value).anchor or "\\" in value:
        raise WorkspaceError("unsafe_path", "A relative path with forward slash separators is required")
    pieces = tuple(value.split("/"))
    for piece in pieces:
        if (piece in ("", ".", "..") or piece[-1:] in (" ", ".")
                or _RESERVED.fullmatch(piece) or any(ord(c) < 32 or c in '<>:"|?*' for c in piece)):
            raise WorkspaceError("unsafe_path", "The path contains an invalid component")
    return pieces


def _checked(path: Path, boundary: Path | None = None, *, directory: bool = False) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        for part in (absolute, *absolute.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise WorkspaceError("unsafe_path", "The path contains a link or reparse point")
        resolved = absolute.resolve(strict=True)
        if boundary is not None and not resolved.is_relative_to(boundary):
            raise WorkspaceError("unsafe_path", "The path is outside the workspace")
        info = resolved.stat()
        if directory and not stat.S_ISDIR(info.st_mode):
            raise WorkspaceError("invalid_path", "The path must be a directory")
        if not directory and not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise WorkspaceError("invalid_path", "The path has an invalid type")
        return resolved
    except WorkspaceError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise WorkspaceError("access_error", "The path is missing or inaccessible") from None


@contextmanager
def _guard_directories(directory: Path, boundary: Path) -> Iterator[None]:
    """On Windows, retain directory handles without FILE_SHARE_DELETE during the operation."""
    handles: list[int] = []
    try:
        _checked(directory, boundary, directory=True)
        if os.name == "nt":
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            create = kernel.CreateFileW
            create.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
            create.restype = ctypes.c_void_p
            close = kernel.CloseHandle
            close.argtypes = [ctypes.c_void_p]
            close.restype = ctypes.c_int
            for part in reversed((directory, *directory.parents)):
                handle = create(str(part), 0, 3, None, 3, 0x02200000, None)
                if handle in (None, ctypes.c_void_p(-1).value):
                    raise WorkspaceError("access_error", "Could not protect the working directory")
                handles.append(handle)
            _checked(directory, boundary, directory=True)
        yield
    finally:
        if handles:
            for handle in reversed(handles):
                close(handle)


@contextmanager
def _destination_lock(path: Path) -> Iterator[None]:
    """Serialize writes to one destination across MCP processes without files."""
    if os.name != "nt":
        yield
        return
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    wait = kernel.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    release = kernel.ReleaseMutex
    release.argtypes = [ctypes.c_void_p]
    close = kernel.CloseHandle
    close.argtypes = [ctypes.c_void_p]
    name = "Local\\G1R-MCP-workspace-" + hashlib.sha256(os.path.normcase(str(path)).encode("utf-8")).hexdigest()
    handle = create(None, False, name)
    if not handle:
        raise WorkspaceError("access_error", "Could not protect the destination write")
    acquired = False
    try:
        outcome = wait(handle, 5000)
        acquired = outcome in (0, 0x80)
        if not acquired:
            raise WorkspaceError("conflict", "The destination is busy with another write; repeat the request")
        yield
    finally:
        if acquired:
            release(handle)
        close(handle)


def _read_bytes(path: Path, boundary: Path, maximum: int | None) -> bytes:
    checked = _checked(path, boundary)
    if not checked.is_file():
        raise WorkspaceError("invalid_path", "The package entry must be a regular file")
    try:
        before = checked.stat()
        if maximum is not None and before.st_size > maximum:
            raise WorkspaceError("limit_exceeded", "The file exceeds the allowed size")
        with checked.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
                raise WorkspaceError("file_changed", "The file changed during reading")
            raw = stream.read() if maximum is None else stream.read(maximum + 1)
            after_read = os.fstat(stream.fileno())
        _checked(checked, boundary)
        after = checked.stat()
        stamp = lambda info: (info.st_size, info.st_mtime_ns, info.st_ino, info.st_dev)
        if stamp(before) != stamp(after_read) or stamp(before) != stamp(after):
            raise WorkspaceError("file_changed", "The file changed during reading")
        if maximum is not None and len(raw) > maximum:
            raise WorkspaceError("limit_exceeded", "The file exceeds the allowed size")
        return raw
    except WorkspaceError:
        raise
    except OSError:
        raise WorkspaceError("access_error", "Could not read the file") from None


def _result(**values) -> dict:
    return {"source": "workspace_package", "compiled": False,
            "runtime_validation": "not_performed", **values}


class PackageWorkspace:
    def __init__(self, workspace_root: str | Path, *, protected_roots: Iterable[str | Path] = ()):
        self.root = _checked(Path(workspace_root), directory=True)
        self.protected_roots = tuple(_checked(Path(root), directory=True) for root in protected_roots)
        self._lock = threading.RLock()

    def _unprotected(self, path: Path, *, forbid_ancestor: bool = False) -> Path:
        """Validate the canonical destination; a shared parent of workspaces is allowed."""
        for protected in self.protected_roots:
            if path.is_relative_to(protected) or (forbid_ancestor and protected.is_relative_to(path)):
                raise WorkspaceError("unsafe_path", "The path belongs to a protected source directory")
        return path

    def _package(self, relative_path: str) -> tuple[Path, str]:
        parts = _parts(relative_path, root_allowed=True)
        package = self._unprotected(_checked(self.root.joinpath(*parts), self.root, directory=True))
        return package, "/".join(parts) or "."

    @staticmethod
    def _file_parts(file_path: str) -> tuple[str, ...]:
        parts = _parts(file_path)
        if parts == ("GothicMod.json",):
            return parts
        if len(parts) >= 2 and parts[0] in COMPONENTS:
            return parts
        raise WorkspaceError("unsupported_file", "GothicMod.json and files in Script, ScriptPatches, Config, Content, or Story are allowed")

    @contextmanager
    def _write_parent(self, package: Path, parts: tuple[str, ...]) -> Iterator[Path]:
        parent = package.joinpath(*parts[:-1])
        self._unprotected(parent)
        if parts[0] != "ScriptPatches" or os.path.lexists(parent):
            yield _checked(parent, package, directory=True)
            return
        # Creation of the single documented directory is serialized separately.
        with _destination_lock(parent), _guard_directories(package, self.root):
            created = False
            try:
                if not os.path.lexists(parent):
                    parent.mkdir()
                    created = True
                yield self._unprotected(_checked(parent, package, directory=True))
            except BaseException as error:
                if created:
                    # Remove only our empty directory; preserve any files created by others.
                    checked = _checked(parent, package, directory=True)
                    if not any(checked.iterdir()):
                        checked.rmdir()
                if isinstance(error, OSError):
                    raise WorkspaceError("access_error", "Could not prepare the patch directory") from None
                raise

    def create_package(self, relative_path: str, metadata: dict) -> dict:
        parts = _parts(relative_path)
        if not isinstance(metadata, dict):
            raise WorkspaceError("invalid_arguments", "Metadata must be a JSON object")
        try:
            raw = (json.dumps(metadata, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")
        except (ValueError, TypeError, UnicodeEncodeError, RecursionError):
            raise WorkspaceError("invalid_descriptor", "Metadata is not valid UTF-8 JSON") from None
        with self._lock:
            parent = _checked(self.root.joinpath(*parts[:-1]), self.root, directory=True)
            target = self._unprotected(parent / parts[-1], forbid_ancestor=True)
            with _destination_lock(target), _guard_directories(parent, self.root):
                if os.path.lexists(target):
                    raise WorkspaceError("conflict", "The package directory already exists")
                stage: Path | None = None
                try:
                    stage = Path(tempfile.mkdtemp(prefix=".sn2-mcp-", dir=parent))
                    (stage / "Script").mkdir()
                    (stage / "GothicMod.json").write_bytes(raw)
                    report = validate_package(parent, stage.name)
                    if not report["valid_structure"]:
                        raise WorkspaceError("invalid_descriptor", "The descriptor failed structural validation")
                    if os.path.lexists(target):
                        raise WorkspaceError("conflict", "The package directory appeared during creation")
                    # On Windows, rename fails atomically when the destination exists.
                    if os.name != "nt":
                        raise WorkspaceError("unsupported_environment", "Atomic package creation is supported on Windows")
                    stage.rename(target)
                    stage = None
                except WorkspaceError:
                    raise
                except OSError:
                    raise WorkspaceError("access_error", "Could not create the package") from None
                finally:
                    if stage is not None:
                        (stage / "GothicMod.json").unlink(missing_ok=True)
                        if (stage / "Script").exists():
                            (stage / "Script").rmdir()
                        stage.rmdir()
            report["package_path"] = "/".join(parts)
            return _result(package_path="/".join(parts), created=True,
                           files=["GothicMod.json"], directories=["Script"],
                           sha256=hashlib.sha256(raw).hexdigest(), validation=report)

    def create_package_directory(self, relative_path: str, directory_path: str) -> dict:
        """Create component directories of an existing official package."""
        parts = _parts(directory_path)
        if parts[0] not in COMPONENTS:
            raise WorkspaceError("unsupported_directory", "Directories in Script, ScriptPatches, Config, Content, or Story are allowed")
        with self._lock:
            package, name = self._package(relative_path)
            created: list[tuple[Path, tuple[int, int]]] = []
            with _destination_lock(package), _guard_directories(package, self.root):
                descriptor = self._unprotected(_checked(package / "GothicMod.json", package))
                if not descriptor.is_file():
                    raise WorkspaceError("invalid_descriptor", "The package must contain a regular GothicMod.json file")
                # Validate all destinations before the first mkdir, including paths not yet present.
                current = package
                targets: list[Path] = []
                for component in parts:
                    current = self._unprotected(current / component)
                    if os.path.lexists(current):
                        checked = self._unprotected(_checked(current, package))
                        if not checked.is_dir():
                            raise WorkspaceError("conflict", "A path component is already occupied by a file")
                    targets.append(current)
                try:
                    report = validate_package(self.root, relative_path)
                except PackageError as error:
                    raise WorkspaceError(error.code, str(error)) from None
                if not report["valid_structure"]:
                    raise WorkspaceError("invalid_descriptor", "The existing package structure failed validation")
                try:
                    with ExitStack() as guards:
                        for target in targets:
                            guards.enter_context(_destination_lock(target))
                            _checked(target.parent, package, directory=True)
                            self._unprotected(target)
                            if os.path.lexists(target):
                                checked = self._unprotected(_checked(target, package))
                                if not checked.is_dir():
                                    raise WorkspaceError("conflict", "A path component is already occupied by a file")
                            else:
                                try:
                                    target.mkdir()
                                except FileExistsError:
                                    checked = self._unprotected(_checked(target, package))
                                    if not checked.is_dir():
                                        raise WorkspaceError("conflict", "A path component appeared as a file") from None
                                else:
                                    info = target.lstat()
                                    created.append((target, (info.st_dev, info.st_ino)))
                            guards.enter_context(_guard_directories(target, self.root))
                except BaseException as error:
                    # Close no-delete handles before rmdir; then verify the path and owner.
                    for target, identity in reversed(created):
                        try:
                            with _guard_directories(target.parent, self.root):
                                checked = self._unprotected(_checked(target, package, directory=True))
                                info = checked.stat()
                                if (info.st_dev, info.st_ino) == identity and not any(checked.iterdir()):
                                    checked.rmdir()
                        except (WorkspaceError, OSError):
                            # Preserve a changed directory or unrelated contents.
                            continue
                    if isinstance(error, OSError):
                        raise WorkspaceError("access_error", "Could not create source directories") from None
                    raise
            paths = [path.relative_to(package).as_posix() for path, _ in created]
            return _result(package_path=name, directory_path="/".join(parts), created=bool(paths),
                           created_paths=paths, created_count=len(paths), exists=True)

    def list_package_files(self, relative_path: str, limit: int = 100, offset: int = 0) -> dict:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise WorkspaceError("invalid_arguments", "The list limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WorkspaceError("invalid_arguments", "The list offset must be a nonnegative integer")
        with self._lock:
            package, name = self._package(relative_path)
            files: list[dict] = []
            descriptor = package / "GothicMod.json"
            if os.path.lexists(descriptor):
                file = self._unprotected(_checked(descriptor, package))
                if not file.is_file():
                    raise WorkspaceError("invalid_path", "The descriptor must be a regular file")
                files.append({"path": "GothicMod.json", "bytes": file.stat().st_size})
            for component in COMPONENTS:
                directory_root = package / component
                if not os.path.lexists(directory_root):
                    continue
                self._unprotected(_checked(directory_root, package, directory=True))
                def walk_error(error: OSError) -> None:
                    raise WorkspaceError("access_error", "Could not read the source directory")
                for directory, dirs, names in os.walk(directory_root, followlinks=False, onerror=walk_error):
                    parent = self._unprotected(_checked(Path(directory), package, directory=True))
                    for child in dirs:
                        self._unprotected(_checked(parent / child, package, directory=True))
                    for child in names:
                        file = self._unprotected(_checked(parent / child, package))
                        if not file.is_file():
                            raise WorkspaceError("invalid_path", "The package entry must be a regular file")
                        files.append({"path": file.relative_to(package).as_posix(), "bytes": file.stat().st_size})
            files.sort(key=lambda item: item["path"])
            items = files[offset:offset + limit]
            has_more = offset + len(items) < len(files)
            return _result(package_path=name, items=items, total=len(files), offset=offset,
                           next_offset=offset + len(items) if has_more else None, has_more=has_more,
                           truncated=len(items) < len(files), snapshot_atomic=False)

    def _source_files(self, relative_path: str) -> tuple[Path, str, list[str]]:
        package, name = self._package(relative_path)
        files = []
        with _guard_directories(package, self.root):
            def walk_error(error: OSError) -> None:
                raise WorkspaceError("access_error", "Could not read the source directory")
            for component in ("Script", "ScriptPatches"):
                root = package / component
                if not os.path.lexists(root):
                    continue
                self._unprotected(_checked(root, package, directory=True))
                for directory, directories, names in os.walk(root, followlinks=False, onerror=walk_error):
                    parent = self._unprotected(_checked(Path(directory), package, directory=True))
                    for child in directories:
                        self._unprotected(_checked(parent / child, package, directory=True))
                    for child in names:
                        file = self._unprotected(_checked(parent / child, package))
                        if not file.is_file():
                            raise WorkspaceError("invalid_path", "The source must be a regular file")
                        files.append(file.relative_to(package).as_posix())
        return package, name, sorted(files)

    @staticmethod
    def _source_search_arguments(query: str, limit: int, offset: int) -> None:
        if not isinstance(query, str) or not query or "\0" in query:
            raise WorkspaceError("invalid_arguments", "The query must be a nonempty string without NUL")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise WorkspaceError("invalid_arguments", "The limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise WorkspaceError("invalid_arguments", "The offset must be a nonnegative integer")

    def _source_search_result(self, name: str, files: list[str], items: list[dict],
                              total: int, limit: int, offset: int, skipped: list[dict], **extra) -> dict:
        next_offset = offset + len(items)
        result = {"items": items, "truncated": total > len(items), "truncation_scope": "items",
                  "total_matches": total, "scanned_files": len(files), "scan_complete": not skipped,
                  "source": "workspace_package_static", "validation": "source_only",
                  "snapshot_kind": "sequential_file_reads", "scope": "workspace_package",
                  "relative_package": name, "package": name,
                  "offset": offset, "limit": limit,
                  "next_offset": next_offset if next_offset < total else None,
                  "has_more": next_offset < total, "pagination_limit_reached": False, "max_offset": None,
                  "skipped_files": skipped, "skipped_count": len(skipped), "skipped_list_truncated": False,
                  **extra}
        if skipped:
            result["total_matches"] = None
            result["total_matches_lower_bound"] = total
        return result

    def search_source_files(self, relative_path: str, query: str, limit: int = 50,
                             offset: int = 0) -> dict:
        """Search Script and ScriptPatches paths of one workspace package without reading file contents."""
        self._source_search_arguments(query, limit, offset)
        needle = query.casefold()
        with self._lock:
            package, name, files = self._source_files(relative_path)
            matched = [file for file in files if needle in file.casefold()]
            items = [{"path": file, "root_kind": "workspace_script" if file.startswith("Script/")
                      else "workspace_script_patches"} for file in matched[offset:offset + limit]]
            return self._source_search_result(name, files, items, len(matched), limit, offset, [])

    def search_source_text(self, relative_path: str, query: str, limit: int = 50,
                            case_sensitive: bool = False, offset: int = 0) -> dict:
        """Search literal UTF-8 lines in Script and ScriptPatches; report unreadable files without claiming a complete scan."""
        self._source_search_arguments(query, limit, offset)
        if not isinstance(case_sensitive, bool):
            raise WorkspaceError("invalid_arguments", "The case sensitivity parameter must be a boolean")
        needle = query if case_sensitive else query.casefold()
        with self._lock:
            package, name, files = self._source_files(relative_path)
            items = []
            skipped = []
            total = 0
            for file_path in files:
                item = {"path": file_path, "root_kind": "workspace_script" if file_path.startswith("Script/")
                        else "workspace_script_patches"}
                try:
                    parts = self._file_parts(file_path)
                    file = self._unprotected(_checked(package.joinpath(*parts), package))
                    with _guard_directories(file.parent, self.root):
                        raw = _read_bytes(file, package, MAX_FILE_BYTES)
                    text = raw.decode("utf-8-sig", errors="strict")
                except UnicodeDecodeError:
                    skipped.append({**item, "error_code": "invalid_encoding"})
                    continue
                except WorkspaceError as error:
                    if error.code == "unsafe_path":
                        raise
                    skipped.append({**item, "error_code": error.code})
                    continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    if needle in (line if case_sensitive else line.casefold()):
                        total += 1
                        if total > offset and len(items) < limit:
                            items.append({**item, "line": line_number, "text": line, "text_truncated": False})
            return self._source_search_result(name, files, items, total, limit, offset, skipped,
                                              query_kind="literal")

    def read_package_file(self, relative_path: str, file_path: str,
                          start_line: int = 1, line_count: int = 100) -> dict:
        parts = self._file_parts(file_path)
        if (isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1
            or isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 1):
            raise WorkspaceError("invalid_arguments", "The line number and line count must be positive integers")
        with self._lock:
            package, name = self._package(relative_path)
            path = self._unprotected(_checked(package.joinpath(*parts), package))
            raw = _read_bytes(path, package,
                              MAX_DESCRIPTOR_BYTES if len(parts) == 1 else MAX_FILE_BYTES)
            try:
                lines = raw.decode("utf-8-sig").splitlines()
            except UnicodeDecodeError:
                raise WorkspaceError("invalid_encoding", "The file must be valid UTF-8") from None
            selected = lines[start_line - 1:start_line - 1 + line_count]
            text = "\n".join(selected)
            return _result(package_path=name, file_path="/".join(parts), sha256=hashlib.sha256(raw).hexdigest(),
                           bytes=len(raw), total_lines=len(lines), start_line=start_line,
                       returned_lines=len(selected), text=text, truncated=False,
                           text_mode="display_lines",
                       has_more=start_line - 1 + len(selected) < len(lines))

    def read_package_file_exact(self, relative_path: str, file_path: str) -> dict:
        """Return the full editable UTF-8 text without changing the BOM, lines, or EOF."""
        parts = self._file_parts(file_path)
        with self._lock:
            package, name = self._package(relative_path)
            path = self._unprotected(_checked(package.joinpath(*parts), package))
            maximum = MAX_DESCRIPTOR_BYTES if len(parts) == 1 else MAX_EXACT_READ_BYTES
            with _guard_directories(path.parent, self.root):
                if maximum is not None and path.stat().st_size > maximum:
                    raise WorkspaceError("file_too_large", "The file exceeds the full text reading limit")
                raw = _read_bytes(path, package, maximum)
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise WorkspaceError("invalid_encoding", "The file must be valid UTF-8") from None
            return _result(package_path=name, file_path="/".join(parts), sha256=hashlib.sha256(raw).hexdigest(),
                           bytes=len(raw), text=text, text_mode="exact", truncated=False,
                           max_read_bytes=maximum, max_text_bytes=maximum)

    def write_package_file(self, relative_path: str, file_path: str, content: str,
                           expected_sha256: str | None = None) -> dict:
        parts = self._file_parts(file_path)
        if not isinstance(content, str) or "\0" in content:
            raise WorkspaceError("invalid_arguments", "Content must be text without NUL")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
            raise WorkspaceError("invalid_arguments", "The expected SHA-256 must contain 64 hexadecimal characters")
        try:
            raw = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError("invalid_encoding", "Content must be valid UTF-8") from None
        maximum = MAX_DESCRIPTOR_BYTES if len(parts) == 1 else MAX_CONTENT_BYTES
        if maximum is not None and len(raw) > maximum:
            raise WorkspaceError("limit_exceeded", "Content exceeds the allowed size")
        with self._lock:
            package, name = self._package(relative_path)
            parent = package.joinpath(*parts[:-1])
            target = self._unprotected(parent / parts[-1])
            if parts[0] == "ScriptPatches" and not os.path.lexists(parent) and expected_sha256 is not None:
                raise WorkspaceError("conflict", "The file to replace is missing")
            with _destination_lock(package), self._write_parent(package, parts), _destination_lock(target), _guard_directories(parent, self.root):
                def verify_destination() -> None:
                    exists = os.path.lexists(target)
                    if expected_sha256 is None:
                        if exists:
                            raise WorkspaceError("conflict", "The file already exists; replacing it requires its SHA-256")
                    elif not exists:
                        raise WorkspaceError("conflict", "The file to replace is missing")
                    else:
                        old = _read_bytes(target, package, maximum)
                        if hashlib.sha256(old).hexdigest() != expected_sha256.lower():
                            raise WorkspaceError("conflict", "The file changed; read it again")
                verify_destination()
                temporary: Path | None = None
                validation = None
                try:
                    fd, path = tempfile.mkstemp(prefix=".sn2-mcp-", dir=parent)
                    temporary = Path(path)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if len(parts) == 1:
                        # Validate the candidate in an isolated temporary structure without replacing the original.
                        with tempfile.TemporaryDirectory(prefix="sn2-mcp-validation-") as stage_name:
                            stage = Path(stage_name)
                            (stage / "GothicMod.json").write_bytes(raw)
                            validation = validate_package(stage, ".")
                        if not validation["valid_structure"]:
                            raise WorkspaceError("invalid_descriptor", "The descriptor failed structural validation")
                    verify_destination()
                    _checked(parent, package, directory=True)
                    if expected_sha256 is None:
                        try:
                            os.link(temporary, target, follow_symlinks=False)
                        except FileExistsError:
                            raise WorkspaceError("conflict", "The file appeared during creation") from None
                        temporary.unlink()
                    else:
                        os.replace(temporary, target)
                    temporary = None
                except WorkspaceError:
                    raise
                except (PackageError, OSError):
                    raise WorkspaceError("access_error", "Could not write the file") from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
            return _result(package_path=name, file_path="/".join(parts), written=True,
                           created=expected_sha256 is None, bytes=len(raw),
                           sha256=hashlib.sha256(raw).hexdigest(), validation=validation)


class SourceWorkspace(PackageWorkspace):
    """Adapt the donor's exact file operations to ordinary mod-development files.

    No game package descriptor or source language is required. Development
    outputs and repository internals are excluded from source enumeration.
    """

    _internal_directories = frozenset({
        ".git", ".venv", ".trash", "build", "artifacts", "__pycache__", "nppbackup",
    })

    @classmethod
    def _source_parts(cls, relative_path: str, *, root_allowed: bool = False) -> tuple[str, ...]:
        parts = _parts(relative_path, root_allowed=root_allowed)
        if any(part.casefold() in cls._internal_directories for part in parts):
            raise WorkspaceError("unsupported_file", "Repository internals and development outputs are not source workspace entries")
        return parts

    def _selected(self, relative_path: str = ".") -> Path:
        parts = self._source_parts(relative_path, root_allowed=True)
        return self._unprotected(_checked(self.root.joinpath(*parts), self.root, directory=True))

    @staticmethod
    def _source_result(**values) -> dict:
        return {"source": "workspace_source", "compiled": False,
                "runtime_validation": "not_performed", **values}

    def _entries(self, relative_path: str) -> list[dict]:
        selected = self._selected(relative_path)
        files = []

        def failure(error: OSError) -> None:
            raise WorkspaceError("access_error", "Could not enumerate the source workspace")

        with _guard_directories(selected, self.root):
            for directory, children, names in os.walk(selected, followlinks=False, onerror=failure):
                parent = self._unprotected(_checked(Path(directory), self.root, directory=True))
                children[:] = sorted(child for child in children if child.casefold() not in self._internal_directories)
                for child in children:
                    self._unprotected(_checked(parent / child, self.root, directory=True))
                for child in names:
                    if child.casefold() in self._internal_directories:
                        continue
                    path = self._unprotected(_checked(parent / child, self.root))
                    if not path.is_file():
                        raise WorkspaceError("invalid_path", "The source entry must be a regular file")
                    files.append({"path": path.relative_to(self.root).as_posix(), "bytes": path.stat().st_size})
        return sorted(files, key=lambda entry: entry["path"])

    @staticmethod
    def _pagination(offset: int, limit: int) -> None:
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise WorkspaceError("invalid_arguments", "A nonnegative offset and positive limit are required")

    def list_files(self, relative_path: str = ".", limit: int = 100, offset: int = 0) -> dict:
        self._pagination(offset, limit)
        with self._lock:
            files = self._entries(relative_path)
        items = files[offset:offset + limit]
        next_offset = offset + len(items)
        return self._source_result(items=items, total=len(files), offset=offset, limit=limit,
            has_more=next_offset < len(files), next_offset=next_offset if next_offset < len(files) else None,
            snapshot_kind="sequential_file_reads", excluded_directories=sorted(self._internal_directories))

    def search_files(self, query: str, relative_path: str = ".", limit: int = 50, offset: int = 0) -> dict:
        self._source_search_arguments(query, limit, offset)
        with self._lock:
            files = self._entries(relative_path)
        matches = [entry for entry in files if query.casefold() in entry["path"].casefold()]
        items = matches[offset:offset + limit]
        next_offset = offset + len(items)
        return self._source_result(items=items, total_matches=len(matches), scanned_files=len(files),
            scan_complete=True, offset=offset, limit=limit, has_more=next_offset < len(matches),
            next_offset=next_offset if next_offset < len(matches) else None, query_kind="literal_path")

    def search_text(self, query: str, relative_path: str = ".", limit: int = 50,
                    case_sensitive: bool = False, offset: int = 0) -> dict:
        self._source_search_arguments(query, limit, offset)
        if type(case_sensitive) is not bool:
            raise WorkspaceError("invalid_arguments", "Case sensitivity must be a boolean")
        needle = query if case_sensitive else query.casefold()
        matches, skipped = [], []
        total = 0
        with self._lock:
            files = self._entries(relative_path)
            for entry in files:
                try:
                    value = self.read_file_exact(entry["path"])
                except WorkspaceError as error:
                    if error.code == "unsafe_path":
                        raise
                    skipped.append({"path": entry["path"], "error_code": error.code})
                    continue
                for number, line in enumerate(value["text"].splitlines(), 1):
                    if needle in (line if case_sensitive else line.casefold()):
                        if total >= offset and len(matches) < limit:
                            matches.append({"path": entry["path"], "line": number, "text": line,
                                            "sha256": value["sha256"]})
                        total += 1
        next_offset = offset + len(matches)
        return self._source_result(items=matches, total_matches=total if not skipped else None,
            total_matches_lower_bound=total, scanned_files=len(files), scan_complete=not skipped,
            skipped_files=skipped, offset=offset, limit=limit, query_kind="literal",
            next_offset=next_offset if next_offset < total else None, has_more=next_offset < total,
            snapshot_kind="sequential_file_reads")

    def read_file_exact(self, file_path: str) -> dict:
        parts = self._source_parts(file_path)
        with self._lock:
            path = self._unprotected(_checked(self.root.joinpath(*parts), self.root))
            with _guard_directories(path.parent, self.root):
                raw = _read_bytes(path, self.root, None)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise WorkspaceError("invalid_encoding", "The source file must be valid UTF-8") from None
        return self._source_result(path="/".join(parts), text=text, bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(), text_mode="exact", contains_bom=raw.startswith(b"\xef\xbb\xbf"),
            content_truncated=False)

    def read_file(self, file_path: str, start_line: int = 1, line_count: int = 100) -> dict:
        if type(start_line) is not int or start_line < 1 or type(line_count) is not int or line_count < 1:
            raise WorkspaceError("invalid_arguments", "The line number and line count must be positive integers")
        value = self.read_file_exact(file_path)
        lines = value["text"].removeprefix("\ufeff").splitlines()
        selected = lines[start_line - 1:start_line - 1 + line_count]
        return {**value, "text": "\n".join(selected), "text_mode": "display_lines", "start_line": start_line,
                "total_lines": len(lines), "returned_lines": len(selected),
                "content_truncated": start_line > 1 or start_line - 1 + len(selected) < len(lines)}

    def create_directory(self, directory_path: str) -> dict:
        parts = self._source_parts(directory_path)
        with self._lock, _destination_lock(self.root):
            current = self.root
            created = []
            for part in parts:
                target = self._unprotected(current / part, forbid_ancestor=True)
                with _guard_directories(current, self.root):
                    if not os.path.lexists(target):
                        try:
                            target.mkdir()
                            created.append(target.relative_to(self.root).as_posix())
                        except OSError:
                            raise WorkspaceError("access_error", "Could not create the source directory") from None
                    current = _checked(target, self.root, directory=True)
        return self._source_result(path="/".join(parts), created_paths=created, created=bool(created))

    def write_file(self, file_path: str, content: str, expected_sha256: str | None = None) -> dict:
        parts = self._source_parts(file_path)
        if not isinstance(content, str) or "\0" in content:
            raise WorkspaceError("invalid_arguments", "Content must be text without NUL")
        if expected_sha256 is not None and (not isinstance(expected_sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256)):
            raise WorkspaceError("invalid_arguments", "The expected SHA-256 must contain 64 hexadecimal characters")
        try:
            raw = content.encode("utf-8")
        except UnicodeEncodeError:
            raise WorkspaceError("invalid_encoding", "Content must be valid UTF-8") from None
        with self._lock:
            parent = self._unprotected(_checked(self.root.joinpath(*parts[:-1]), self.root, directory=True))
            target = self._unprotected(parent / parts[-1])
            with _destination_lock(target), _guard_directories(parent, self.root):
                def verify() -> None:
                    exists = os.path.lexists(target)
                    if expected_sha256 is None:
                        if exists:
                            raise WorkspaceError("conflict", "The file already exists; replacing it requires its SHA-256")
                    elif not exists or hashlib.sha256(_read_bytes(target, self.root, None)).hexdigest() != expected_sha256.lower():
                        raise WorkspaceError("conflict", "The file changed; read it again")
                verify()
                temporary = None
                try:
                    fd, name = tempfile.mkstemp(prefix=".sn2-source-", dir=parent)
                    temporary = Path(name)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    verify()
                    if expected_sha256 is None:
                        try:
                            os.link(temporary, target, follow_symlinks=False)
                        except FileExistsError:
                            raise WorkspaceError("conflict", "The file appeared during creation") from None
                        temporary.unlink()
                    else:
                        os.replace(temporary, target)
                    temporary = None
                except WorkspaceError:
                    raise
                except OSError:
                    raise WorkspaceError("access_error", "Could not write the source file") from None
                finally:
                    if temporary is not None:
                        temporary.unlink(missing_ok=True)
        return self._source_result(path="/".join(parts), written=True, created=expected_sha256 is None,
                                  bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
