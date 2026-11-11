"""Export an official package to a byte-preserving, verifiable ZIP.

The descriptor and ordinary files in Script, ScriptPatches, Config, Content,
and Story are preserved. The ZIP and manifest are published together in a new
artifacts directory. Compilation, installation, and patch application are not performed.
"""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import zipfile

from .packages import PackageError, validate_package, MAX_DESCRIPTOR_BYTES, COMPONENTS, LOAD_ORDERS
from .workspace import (PackageWorkspace, WorkspaceError, _checked, _parts,
                        _destination_lock, _guard_directories, _read_bytes)
from .workspace import SourceWorkspace

class ExportError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _allowed(relative: str) -> tuple[str, ...]:
    parts = _parts(relative)
    if parts == ("GothicMod.json",):
        return parts
    if len(parts) >= 2 and parts[0] in COMPONENTS:
        return parts
    raise ExportError("unsupported_entry", "Package export accepts GothicMod.json and files in Script, ScriptPatches, Config, Content, or Story")


def _source_files(workspace: PackageWorkspace, relative_package: str) -> tuple[Path, dict, dict[str, bytes]]:
    package, relative = workspace._package(relative_package)
    validation = validate_package(workspace.root, relative)
    if not validation["valid_structure"]:
        raise ExportError("invalid_package", "The official package failed structural validation")
    name = validation["metadata"]["name"]
    if _parts(name) != (name,):
        raise ExportError("invalid_package_name", "The package name must be a single valid Windows path component")
    files = {}
    folded = set()

    def failure(error):
        raise ExportError("access_error", "Failed to enumerate the entire official package")

    with _guard_directories(package, workspace.root):
        for directory, children, names in os.walk(package, followlinks=False, onerror=failure):
            parent = _checked(Path(directory), package, directory=True)
            for child in children:
                path = workspace._unprotected(_checked(parent / child, package, directory=True))
                parts = path.relative_to(package).parts
                _parts(path.relative_to(package).as_posix())
                if not parts or parts[0] not in COMPONENTS:
                    raise ExportError("unsupported_entry", "The package contains a directory outside the official components")
            for filename in names:
                path = workspace._unprotected(_checked(parent / filename, package))
                relative_path = path.relative_to(package).as_posix()
                parts = _allowed(relative_path)
                if relative_path.casefold() in folded:
                    raise ExportError("duplicate_path", "The package contains duplicate Windows paths")
                folded.add(relative_path.casefold())
                raw = _read_bytes(path, package, MAX_DESCRIPTOR_BYTES if len(parts) == 1 else None)
                files[relative_path] = raw
    if _descriptor_metadata(files.get("GothicMod.json", b"")) != validation["metadata"]:
        raise ExportError("source_changed", "The descriptor changed between validation and reading")
    return package, validation["metadata"], files


def _destination(path: Path, workspace: PackageWorkspace) -> Path:
    artifacts = workspace._unprotected(_checked(workspace.root / "artifacts", workspace.root, directory=True), forbid_ancestor=True)
    target = Path(os.path.abspath(path))
    if target == artifacts or not target.is_relative_to(artifacts):
        raise ExportError("unsafe_path", "Export requires a new directory within artifacts in the selected workspace")
    _checked(target.parent, artifacts, directory=True)
    workspace._unprotected(target, forbid_ancestor=True)
    if os.path.lexists(target):
        raise ExportError("already_exists", "The export directory already exists")
    return target


def _archive_bytes(files: dict[str, bytes], name: str) -> bytes:
    import io
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
        for relative, raw in sorted(files.items()):
            entry = zipfile.ZipInfo(name + "/" + relative, date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(entry, raw)
    return stream.getvalue()


def _cleanup(stage: Path, parent: Path, own_files: dict, stage_identity: tuple[int, int]) -> None:
    try:
        with _guard_directories(stage, parent):
            if _identity(_checked(stage, parent, directory=True)) != stage_identity:
                return
            for name, (identity, digest) in own_files.items():
                path = _checked(stage / name, stage)
                if _identity(path) == identity and _sha(_read_bytes(path, stage, None)) == digest:
                    path.unlink()
        if not any(stage.iterdir()):
            stage.rmdir()
    except (WorkspaceError, OSError):
        pass


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _descriptor_metadata(raw: bytes) -> dict:
    try:
        descriptor = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_json,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise ExportError("invalid_descriptor", "The export descriptor is not valid UTF-8 JSON") from None
    if not isinstance(descriptor, dict) or any(not isinstance(descriptor.get(field), str) or not descriptor[field].strip()
                                               for field in ("name", "title", "author", "modVersion")):
        raise ExportError("invalid_descriptor", "The export descriptor is missing required fields")
    metadata = {field: descriptor[field] for field in ("name", "title", "author", "modVersion")}
    for field in ("description", "projectWebsiteURL"):
        if field in descriptor:
            if not isinstance(descriptor[field], str):
                raise ExportError("invalid_descriptor", "An optional descriptor field must be a string")
            metadata[field] = descriptor[field]
    if "loadOrder" in descriptor:
        if not isinstance(descriptor["loadOrder"], str) or descriptor["loadOrder"] not in LOAD_ORDERS:
            raise ExportError("invalid_descriptor", "Invalid loadOrder")
        metadata["loadOrder"] = descriptor["loadOrder"]
    if "dependencies" in descriptor:
        dependencies = descriptor["dependencies"]
        if not isinstance(dependencies, list) or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                                                    or not item["name"].strip() or not isinstance(item.get("requiredVersion"), str)
                                                    for item in dependencies):
            raise ExportError("invalid_descriptor", "Invalid descriptor dependencies")
        metadata["dependencies"] = [{"name": item["name"], "requiredVersion": item["requiredVersion"]} for item in dependencies]
    return metadata


def export_package(workspace_root: Path, relative_package: str, destination: Path, *,
                   modkit_root: Path, game_root: Path) -> dict:
    workspace = PackageWorkspace(workspace_root, protected_roots=(modkit_root, game_root))
    target = _destination(destination, workspace)
    package, metadata, files = _source_files(workspace, relative_package)
    if target.is_relative_to(package) or package.is_relative_to(target):
        raise ExportError("unsafe_path", "The export directory overlaps the source package")
    _, final_metadata, final_files = _source_files(workspace, relative_package)
    if final_metadata != metadata or final_files != files:
        raise ExportError("source_changed", "The official package changed while reading")
    name = metadata["name"]
    archive_raw = _archive_bytes(files, name)
    archive_name = name + ".zip"
    entries = [{"path": path, "bytes": len(raw), "sha256": _sha(raw)} for path, raw in sorted(files.items())]
    manifest = {"schema_version": 1, "kind": "official_script_package_export", "package_name": name,
                "mod_version": metadata["modVersion"], "archive": {"filename": archive_name, "bytes": len(archive_raw), "sha256": _sha(archive_raw)},
                "files": entries, "file_count": len(entries), "total_source_bytes": sum(item["bytes"] for item in entries),
                "byte_preservation": True, "compiled": False, "installed": False, "runtime_validation": "not_performed"}
    manifest_raw = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    stage = None
    stage_identity = None
    owned = {}
    with _destination_lock(target), _guard_directories(target.parent, workspace.root):
        try:
            if os.path.lexists(target):
                raise ExportError("already_exists", "The export directory appeared before writing")
            stage = Path(tempfile.mkdtemp(prefix=".g1r-script-export-", dir=target.parent))
            stage_identity = _identity(stage)
            with _guard_directories(stage, target.parent):
                for filename, raw in ((archive_name, archive_raw), ("manifest.json", manifest_raw)):
                    file = stage / filename
                    partial = None
                    try:
                        with file.open("xb") as stream:
                            info = os.fstat(stream.fileno())
                            partial = (info.st_dev, info.st_ino)
                            stream.write(raw)
                        owned[filename] = (_identity(file), _sha(raw))
                    except OSError:
                        if partial is not None:
                            checked = _checked(file, stage)
                            current = _read_bytes(checked, stage, None)
                            if _identity(checked) == partial and raw.startswith(current):
                                checked.unlink()
                        raise
                verify_export(stage)
                for filename, (identity, digest) in owned.items():
                    checked = _checked(stage / filename, stage)
                    if _identity(checked) != identity or _sha(_read_bytes(checked, stage, None)) != digest:
                        raise ExportError("copy_changed", "The prepared ZIP or manifest changed before publication")
            if os.path.lexists(target):
                raise ExportError("already_exists", "The export directory appeared before publication")
            stage.rename(target)
            stage = None
            return {"destination": str(target), "archive": archive_name, "archive_sha256": _sha(archive_raw),
                    "manifest_sha256": _sha(manifest_raw), "file_count": len(entries), "total_source_bytes": manifest["total_source_bytes"],
                    "compiled": False, "installed": False, "runtime_validation": "not_performed"}
        finally:
            if stage is not None:
                _cleanup(stage, target.parent, owned, stage_identity)


def verify_export(export_root: Path) -> dict:
    root = _checked(export_root, directory=True)
    raw_manifest = _read_bytes(_checked(root / "manifest.json", root), root, None)
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"), object_pairs_hook=_unique_json,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    except (ValueError, UnicodeError):
        raise ExportError("invalid_manifest", "The export manifest is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("kind") != "official_script_package_export":
        raise ExportError("invalid_manifest", "Unsupported export manifest")
    if (manifest.get("byte_preservation") is not True or manifest.get("compiled") is not False
            or manifest.get("installed") is not False or manifest.get("runtime_validation") != "not_performed"):
        raise ExportError("invalid_manifest", "The export manifest contains unverified runtime claims")
    name = manifest.get("package_name")
    if not isinstance(name, str) or _parts(name) != (name,):
        raise ExportError("invalid_manifest", "Invalid package root name")
    archive_info = manifest.get("archive")
    if not isinstance(archive_info, dict) or archive_info.get("filename") != name + ".zip":
        raise ExportError("invalid_manifest", "Invalid ZIP name")
    archive_path = _checked(root / archive_info["filename"], root)
    archive_raw = _read_bytes(archive_path, root, None)
    if type(archive_info.get("bytes")) is not int or len(archive_raw) != archive_info["bytes"] or _sha(archive_raw) != archive_info.get("sha256"):
        raise ExportError("integrity_mismatch", "The ZIP hash does not match the manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ExportError("invalid_manifest", "Invalid source list")
    expected = {}
    folded = set()
    total = 0
    for item in files:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ExportError("invalid_manifest", "Invalid source entry")
        path = item["path"]
        _allowed(path)
        if path.casefold() in folded or type(item.get("bytes")) is not int or item["bytes"] < 0:
            raise ExportError("invalid_manifest", "Duplicate source path or invalid source size")
        folded.add(path.casefold())
        expected[name + "/" + path] = item
        total += item["bytes"]
    if name + "/GothicMod.json" not in expected:
        raise ExportError("incomplete_package", "The ZIP is missing the descriptor")
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(archive_raw)) as archive:
            entries = archive.infolist()
            if len(entries) != len(expected):
                raise ExportError("integrity_mismatch", "The ZIP entry count differs from the manifest")
            seen = set()
            descriptor = None
            for entry in entries:
                if entry.filename.casefold() in seen or entry.filename not in expected or entry.flag_bits & 1 or entry.compress_type != zipfile.ZIP_STORED:
                    raise ExportError("unsafe_archive", "The ZIP contains duplicate, encrypted, or invalid entries")
                seen.add(entry.filename.casefold())
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (mode and not stat.S_ISREG(mode)) or entry.is_dir() or entry.file_size != expected[entry.filename]["bytes"]:
                    raise ExportError("unsafe_archive", "The ZIP contains a non-regular file or an invalid size")
                with archive.open(entry) as stream:
                    content = stream.read()
                if len(content) != entry.file_size or _sha(content) != expected[entry.filename]["sha256"]:
                    raise ExportError("integrity_mismatch", "A ZIP source differs from the manifest")
                if entry.filename == name + "/GothicMod.json":
                    descriptor = _descriptor_metadata(content)
            if not isinstance(descriptor, dict) or descriptor.get("name") != name or descriptor.get("modVersion") != manifest.get("mod_version"):
                raise ExportError("integrity_mismatch", "The ZIP descriptor does not match the manifest")
    except (zipfile.BadZipFile, UnicodeError, ValueError):
        raise ExportError("unsafe_archive", "The ZIP or its descriptor is corrupted") from None
    actual = {_checked(path, root).name for path in root.iterdir()}
    if actual != {"manifest.json", name + ".zip"} or manifest.get("file_count") != len(expected) or manifest.get("total_source_bytes") != total:
        raise ExportError("integrity_mismatch", "The export directory contains unrelated files or invalid totals")
    return {"package_name": name, "file_count": len(expected), "archive_sha256": _sha(archive_raw),
            "verified_files": True, "compiled": False, "installed": False, "runtime_validation": "not_performed"}


def export_sources(workspace: SourceWorkspace, source_files: list[str], destination: str) -> dict:
    """Reuse byte-preserving export for selected development source and evidence.

    This ZIP is a source archive, not an official game package or compiled mod.
    """
    if not source_files or any(not isinstance(path, str) for path in source_files):
        raise ExportError("invalid_arguments", "Select nonempty source file paths")
    if len({path.casefold() for path in source_files}) != len(source_files):
        raise ExportError("duplicate_path", "Select unique Windows source paths")
    parts = _parts(destination)
    if len(parts) < 2 or parts[0] != "artifacts":
        raise ExportError("unsafe_path", "Export requires a new directory under workspace artifacts")
    files = {}
    for relative in source_files:
        source_parts = workspace._source_parts(relative)
        path = workspace._unprotected(_checked(workspace.root.joinpath(*source_parts), workspace.root))
        with _guard_directories(path.parent, workspace.root):
            files[relative] = _read_bytes(path, workspace.root, None)
    for relative, raw in files.items():
        path = workspace._unprotected(_checked(workspace.root / relative, workspace.root))
        if _read_bytes(path, workspace.root, None) != raw:
            raise ExportError("source_changed", "A selected source changed while preparing export")
    entries = [{"path": relative, "bytes": len(raw), "sha256": _sha(raw)} for relative, raw in sorted(files.items())]
    archive = _archive_bytes(files, "sources")
    manifest = {"schema_version": 1, "kind": "mod_development_source_export", "files": entries,
        "file_count": len(entries), "archive": {"filename": "sources.zip", "bytes": len(archive), "sha256": _sha(archive)},
        "byte_preservation": True, "compiled": False, "installed": False, "runtime_validation": "not_performed"}
    raw_manifest = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    artifacts = workspace.root / "artifacts"
    target = workspace._unprotected(workspace.root.joinpath(*parts), forbid_ancestor=True)
    with _destination_lock(target):
        with _destination_lock(artifacts), _guard_directories(workspace.root, workspace.root):
            if not os.path.lexists(artifacts):
                try:
                    artifacts.mkdir()
                except OSError:
                    raise ExportError("access_error", "Could not prepare the source export directory") from None
        parent = workspace._unprotected(_checked(target.parent, workspace.root, directory=True))
        with _guard_directories(parent, workspace.root):
            if os.path.lexists(target):
                raise ExportError("already_exists", "The export directory already exists")
            try:
                stage = Path(tempfile.mkdtemp(prefix=".sn2-source-export-", dir=parent))
            except OSError:
                raise ExportError("access_error", "Could not prepare source export staging") from None
            stage_identity = _identity(stage)
            owned = {}
            try:
                for filename, raw in (("sources.zip", archive), ("manifest.json", raw_manifest)):
                    path = stage / filename
                    partial_identity = None
                    try:
                        with path.open("xb") as stream:
                            info = os.fstat(stream.fileno())
                            partial_identity = (info.st_dev, info.st_ino)
                            stream.write(raw)
                    except OSError:
                        if partial_identity is not None:
                            try:
                                checked = _checked(path, stage)
                                current = _read_bytes(checked, stage, None)
                                if _identity(checked) == partial_identity and raw.startswith(current):
                                    checked.unlink()
                            except (WorkspaceError, OSError):
                                pass
                        raise
                    owned[filename] = (_identity(path), _sha(raw))
                verify_source_export(stage)
                if os.path.lexists(target):
                    raise ExportError("already_exists", "The export destination appeared during preparation")
                stage.rename(target)
                stage = None
            except OSError:
                raise ExportError("access_error", "Could not publish the source export") from None
            finally:
                if stage is not None:
                    _cleanup(stage, parent, owned, stage_identity)
    return {"destination": destination, "archive_sha256": _sha(archive), "manifest_sha256": _sha(raw_manifest),
            "file_count": len(entries), "compiled": False, "installed": False, "runtime_validation": "not_performed"}


def verify_source_export(export_root: Path) -> dict:
    """Verify the selected archive inventory and every source byte hash."""
    root = _checked(export_root, directory=True)
    try:
        manifest = json.loads(_read_bytes(root / "manifest.json", root, None).decode("utf-8"), object_pairs_hook=_unique_json)
    except (ValueError, UnicodeError):
        raise ExportError("invalid_manifest", "The source export manifest is invalid") from None
    if (not isinstance(manifest, dict) or manifest.get("kind") != "mod_development_source_export"
            or manifest.get("schema_version") != 1 or not isinstance(manifest.get("files"), list)
            or not manifest["files"]):
        raise ExportError("invalid_manifest", "The manifest is not a source export")
    archive = _read_bytes(root / "sources.zip", root, None)
    if manifest.get("archive") != {"filename": "sources.zip", "bytes": len(archive), "sha256": _sha(archive)}:
        raise ExportError("integrity_mismatch", "The source archive differs from its manifest")
    expected, folded = {}, set()
    for entry in manifest["files"]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                or type(entry.get("bytes")) is not int or entry["bytes"] < 0
                or not isinstance(entry.get("sha256"), str)):
            raise ExportError("invalid_manifest", "A source entry is invalid")
        SourceWorkspace._source_parts(entry["path"])
        if entry["path"].casefold() in folded:
            raise ExportError("duplicate_path", "The source manifest contains duplicate paths")
        folded.add(entry["path"].casefold())
        expected["sources/" + entry["path"]] = entry
    import io
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as opened:
            entries = opened.infolist()
            if len(entries) != len(expected) or len({entry.filename for entry in entries}) != len(entries):
                raise ExportError("integrity_mismatch", "The archive inventory differs from its manifest")
            for entry in entries:
                mode = (entry.external_attr >> 16) & 0xFFFF
                if (entry.filename not in expected or entry.is_dir() or entry.flag_bits & 1
                        or entry.compress_type != zipfile.ZIP_STORED
                        or (mode and not stat.S_ISREG(mode))):
                    raise ExportError("unsafe_archive", "The source archive contains an invalid entry")
                raw = opened.read(entry)
                if len(raw) != expected[entry.filename]["bytes"] or _sha(raw) != expected[entry.filename]["sha256"]:
                    raise ExportError("integrity_mismatch", "An archived source differs from its manifest")
    except zipfile.BadZipFile:
        raise ExportError("unsafe_archive", "The source ZIP is corrupted") from None
    if {path.name for path in root.iterdir()} != {"manifest.json", "sources.zip"} or manifest.get("file_count") != len(expected):
        raise ExportError("integrity_mismatch", "The export contains unrelated files or invalid totals")
    return {"kind": manifest["kind"], "verified_files": True, "file_count": len(expected),
            "archive_sha256": _sha(archive), "compiled": False, "installed": False, "runtime_validation": "not_performed"}

