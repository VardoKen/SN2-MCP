"""Editable standalone native mod projects and identified compiled packages.

The project layout and build inputs come from the existing bridge SDK. A package
contains an actual DLL and its build/source receipts; source export, installation
and runtime activation remain separate operations.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import struct
import tempfile
import uuid

from .workspace import SourceWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories, _parts, _read_bytes


PROJECT_KIND = "native_widget_opacity"
PACKAGE_KIND = "sn2_native_mod_package"
TEMPLATE_FILES = ("CMakeLists.txt", "mod.json", "src/mod.cpp", "README.md")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MOD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class ModProjectError(Exception):
    def __init__(self, code: str, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.code = code
        self.receipt = receipt


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json(raw: bytes) -> dict:
    def unique(pairs):
        value = {}
        for key, child in pairs:
            if key in value:
                raise ValueError("duplicate_key")
            value[key] = child
        return value
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    except (UnicodeDecodeError, ValueError):
        raise ModProjectError("invalid_manifest", "The JSON manifest must be valid UTF-8 with unique keys") from None
    if not isinstance(value, dict):
        raise ModProjectError("invalid_manifest", "The manifest must be a JSON object")
    return value


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _mod_id(value: object) -> str:
    if not isinstance(value, str) or not _MOD_ID.fullmatch(value):
        raise ModProjectError("invalid_mod_id", "mod_id must be a CMake identifier beginning with a letter")
    _parts(value)
    if value.casefold() == "sn2_mcp":
        raise ModProjectError("invalid_mod_id", "The modification artifact must have its own name")
    return value


def _read(path: Path, boundary: Path) -> bytes:
    return _read_bytes(_checked(path, boundary), boundary, None)


def _inventory(files: dict[str, bytes]) -> list[dict]:
    return [{"path": path, "bytes": len(raw), "sha256": _sha(raw)} for path, raw in sorted(files.items())]


def _build_revision(inputs: object) -> str:
    if not isinstance(inputs, list) or not inputs:
        raise ModProjectError("build_provenance_mismatch", "The build requires a complete source input inventory")
    try:
        content = [{key: item[key] for key in ("path", "sha256", "bytes", "role")} for item in inputs]
    except (KeyError, TypeError):
        raise ModProjectError("build_provenance_mismatch", "The build input inventory lacks source identity fields") from None
    for item in content:
        if (not isinstance(item["path"], str) or not isinstance(item["sha256"], str) or not _SHA.fullmatch(item["sha256"])
                or type(item["bytes"]) is not int or item["bytes"] < 0 or not isinstance(item["role"], str)):
            raise ModProjectError("build_provenance_mismatch", "The build input inventory has invalid source identity fields")
        _parts(item["path"])
    if len({item["path"].casefold() for item in content}) != len(content):
        raise ModProjectError("build_provenance_mismatch", "The build input inventory contains duplicate paths")
    return _sha(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _file_set(root: Path) -> set[str]:
    result = set()
    def fail(_error):
        raise ModProjectError("access_error", "Could not enumerate every package file")
    for directory, children, names in os.walk(root, followlinks=False, onerror=fail):
        parent = _checked(Path(directory), root, directory=True)
        for child in children:
            _checked(parent / child, root, directory=True)
        for name in names:
            path = _checked(parent / name, root)
            if not path.is_file():
                raise ModProjectError("invalid_file", "Package entries must be ordinary files")
            relative = path.relative_to(root).as_posix()
            _parts(relative)
            result.add(relative)
    if len({item.casefold() for item in result}) != len(result):
        raise ModProjectError("duplicate_path", "The package contains duplicate Windows paths")
    return result


def _pe(raw: bytes) -> dict:
    if len(raw) < 64 or raw[:2] != b"MZ":
        raise ModProjectError("invalid_artifact", "The modification artifact must be a Windows x64 DLL")
    offset = struct.unpack_from("<I", raw, 60)[0]
    if offset < 64 or offset + 24 > len(raw) or raw[offset:offset + 4] != b"PE\0\0":
        raise ModProjectError("invalid_artifact", "The modification artifact has no valid PE header")
    machine = struct.unpack_from("<H", raw, offset + 4)[0]
    flags = struct.unpack_from("<H", raw, offset + 22)[0]
    if machine != 0x8664 or not flags & 0x2000:
        raise ModProjectError("invalid_artifact", "The modification artifact must be a Windows x64 DLL")
    return {"format": "PE", "architecture": "x64", "machine": machine, "kind": "dll"}


def _pe_imports(raw: bytes) -> list[dict]:
    """Read ordinary and delay-import dependencies from the actual PE bytes."""
    _pe(raw)
    offset = struct.unpack_from("<I", raw, 60)[0]
    sections, optional_bytes = struct.unpack_from("<H", raw, offset + 6)[0], struct.unpack_from("<H", raw, offset + 20)[0]
    optional = offset + 24
    if optional_bytes < 112 or optional + optional_bytes > len(raw) or struct.unpack_from("<H", raw, optional)[0] != 0x20B:
        raise ModProjectError("invalid_artifact", "The modification requires a complete PE32+ optional header")
    section_start = optional + optional_bytes
    if section_start + sections * 40 > len(raw):
        raise ModProjectError("invalid_artifact", "The PE section table exceeds the artifact bytes")
    headers_size = struct.unpack_from("<I", raw, optional + 60)[0]
    image_base = struct.unpack_from("<Q", raw, optional + 24)[0]
    data_directories = struct.unpack_from("<I", raw, optional + 108)[0]

    def mapped(rva: int, size: int) -> int:
        if 0 <= rva < headers_size and rva + size <= min(headers_size, len(raw)):
            return rva
        for index in range(sections):
            section = section_start + index * 40
            virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", raw, section + 8)
            if address <= rva and rva + size <= address + max(virtual_size, raw_size):
                delta = rva - address
                if delta + size <= raw_size and raw_offset + delta + size <= len(raw):
                    return raw_offset + delta
        raise ModProjectError("invalid_artifact", "A PE import record is outside mapped artifact bytes")

    def name(rva: int) -> str:
        value = bytearray()
        while True:
            byte = raw[mapped(rva + len(value), 1)]
            if not byte:
                break
            value.append(byte)
        try:
            text = value.decode("ascii")
        except UnicodeDecodeError:
            raise ModProjectError("invalid_artifact", "A PE import module name is not ASCII") from None
        if not text or any(character in text for character in "\\/:\0"):
            raise ModProjectError("invalid_artifact", "A PE import requires an ordinary module filename")
        return text

    modules = {}
    for directory_index, kind, descriptor_size, name_field in ((1, "ordinary_import", 20, 12), (13, "delay_import", 32, 4)):
        if data_directories <= directory_index or optional_bytes < 112 + (directory_index + 1) * 8:
            continue
        rva, size = struct.unpack_from("<II", raw, optional + 112 + directory_index * 8)
        if not rva:
            continue
        if size < descriptor_size:
            raise ModProjectError("invalid_artifact", "The PE import directory has no complete descriptor")
        cursor = 0
        terminated = False
        while cursor + descriptor_size <= size:
            location = mapped(rva + cursor, descriptor_size)
            descriptor = raw[location:location + descriptor_size]
            if not any(descriptor):
                terminated = True
                break
            name_rva = struct.unpack_from("<I", descriptor, name_field)[0]
            if kind == "delay_import" and not struct.unpack_from("<I", descriptor, 0)[0] & 1:
                name_rva -= image_base
            module = name(name_rva)
            entry = modules.setdefault(module.casefold(), {"module": module, "imports": [],
                "resolution": "target_machine_loader", "bundled": False, "availability_checked": False})
            if kind not in entry["imports"]:
                entry["imports"].append(kind)
            cursor += descriptor_size
        if not terminated:
            raise ModProjectError("invalid_artifact", "The PE import directory is not terminated within its recorded size")
    return [modules[key] for key in sorted(modules)]


def _command_export(raw: bytes) -> dict:
    """Validate the required direct command export without executing the DLL."""
    _pe(raw)
    offset = struct.unpack_from("<I", raw, 60)[0]
    optional_bytes = struct.unpack_from("<H", raw, offset + 20)[0]
    optional = offset + 24
    if optional_bytes < 120 or optional + optional_bytes > len(raw):
        raise ModProjectError("invalid_artifact", "The modification requires a complete PE export directory")
    sections = struct.unpack_from("<H", raw, offset + 6)[0]
    section_start = optional + optional_bytes
    if section_start + sections * 40 > len(raw):
        raise ModProjectError("invalid_artifact", "The PE section table exceeds the artifact bytes")
    headers_size = struct.unpack_from("<I", raw, optional + 60)[0]
    section_values = []
    for index in range(sections):
        section = section_start + index * 40
        virtual_size, address, raw_size, raw_offset = struct.unpack_from("<IIII", raw, section + 8)
        flags = struct.unpack_from("<I", raw, section + 36)[0]
        section_values.append((address, max(virtual_size, raw_size), raw_size, raw_offset, flags))
    def mapped(rva: int, size: int) -> int:
        if 0 <= rva < headers_size and rva + size <= min(headers_size, len(raw)):
            return rva
        for address, extent, raw_size, raw_offset, _flags in section_values:
            if address <= rva and rva + size <= address + extent:
                delta = rva - address
                if delta + size <= raw_size and raw_offset + delta + size <= len(raw):
                    return raw_offset + delta
        raise ModProjectError("invalid_artifact", "A PE export record exceeds mapped artifact bytes")
    export_rva, export_size = struct.unpack_from("<II", raw, optional + 112)
    if not export_rva or export_size < 40:
        raise ModProjectError("mod_export_missing", "The artifact has no SN2ModCommand export")
    directory = mapped(export_rva, 40)
    function_count, name_count, functions, names, ordinals = struct.unpack_from("<IIIII", raw, directory + 20)
    for index in range(name_count):
        name_rva = struct.unpack_from("<I", raw, mapped(names + index * 4, 4))[0]
        text = bytearray()
        while True:
            byte = raw[mapped(name_rva + len(text), 1)]
            if not byte:
                break
            text.append(byte)
        if bytes(text) != b"SN2ModCommand":
            continue
        ordinal = struct.unpack_from("<H", raw, mapped(ordinals + index * 2, 2))[0]
        if ordinal >= function_count:
            raise ModProjectError("invalid_artifact", "The command export ordinal exceeds the PE function table")
        command_rva = struct.unpack_from("<I", raw, mapped(functions + ordinal * 4, 4))[0]
        if export_rva <= command_rva < export_rva + export_size:
            raise ModProjectError("mod_export_forwarded", "The command export must not forward to another module")
        if not any(address <= command_rva < address + extent and flags & 0x20000000
                   for address, extent, _raw_size, _raw_offset, flags in section_values):
            raise ModProjectError("mod_export_not_executable", "The command export must point to the DLL's executable section")
        mapped(command_rva, 1)
        return {"name": "SN2ModCommand", "rva": command_rva, "direct": True, "executable": True,
                "abi_declared": 1, "abi_runtime_checked": False}
    raise ModProjectError("mod_export_missing", "The artifact has no SN2ModCommand export")


def validate_mod_project(root: Path, project_path: str) -> dict:
    """Read actual project structure; this does not build or activate code."""
    workspace = SourceWorkspace(root)
    project = workspace._selected(project_path)
    metadata = _json(_read(project / "mod.json", project))
    identity = _mod_id(metadata.get("mod_id"))
    required = {"schema": 1, "kind": PROJECT_KIND, "mod_id": identity,
                "command_abi": 1, "cmake_target": "sn2_mod", "artifact": identity + ".dll",
                "sources": ["src/mod.cpp", "CMakeLists.txt", "mod.json", "README.md"]}
    issues = []
    if metadata != required:
        issues.append({"code": "project_contract_mismatch", "message": "mod.json differs from the standalone native mod contract"})
    files = {}
    for relative in TEMPLATE_FILES:
        try:
            raw = _read(project / relative, project)
            raw.decode("utf-8-sig")
            files[relative] = raw
        except UnicodeDecodeError:
            issues.append({"code": "invalid_encoding", "path": relative, "message": "Project source must be valid UTF-8"})
        except WorkspaceError as error:
            issues.append({"code": error.code, "path": relative, "message": "A required project input is unavailable"})
    cmake = files.get("CMakeLists.txt", b"").decode("utf-8-sig", errors="replace")
    cmake_commands = "\n".join(line.split("#", 1)[0].rstrip() for line in cmake.splitlines()).strip()
    # Check the declared integration, rather than inferring it from a filename.
    required_cmake = ("project(" + identity + " LANGUAGES C CXX)",
                      "SN2_MCP_SDK_ROOT", "SN2_MOD_SOURCE_REVISION",
                      "set(SN2_MCP_MOD_SDK_ONLY ON", 'add_subdirectory("${SN2_MCP_SDK_ROOT}/native" sn2-sdk)',
                      "add_library(sn2_mod SHARED src/mod.cpp)",
                      'OUTPUT_NAME "' + identity + '"',
                      "target_link_libraries(sn2_mod PRIVATE sn2_mod_runtime)")
    if any(token not in cmake_commands for token in required_cmake):
        issues.append({"code": "cmake_contract_mismatch", "path": "CMakeLists.txt",
                       "message": "CMake must declare the standalone SDK, source revision and sn2_mod DLL target"})
    if not re.search(r"extern\s+\"C\"\s+const\s+sn2_mod::Specification\s*\*\s*SN2ModSpecification\s*\(\s*\)\s*noexcept",
                     files.get("src/mod.cpp", b"").decode("utf-8-sig", errors="replace")):
        issues.append({"code": "mod_entry_missing", "path": "src/mod.cpp",
                       "message": "The project must define the standalone SN2ModSpecification entry"})
    inventory = _inventory(files)
    return {"source": "native_mod_project", "project_path": project_path, "project_root": str(project),
            "metadata": metadata, "valid": not issues, "issues": issues, "files": inventory,
            "project_source_manifest_sha256": _sha(_json_bytes({"files": inventory})),
            "compiled": False, "installed": False, "runtime_validation": "not_performed",
            "build_prerequisites": {"platform": "Windows x64", "cmake": ">=3.24", "toolchain": "Visual Studio 2022 MSVC",
                                    "sdk_parameter": "SN2_MCP_SDK_ROOT", "revision_parameter": "SN2_MOD_SOURCE_REVISION"}}


def verify_artifact_package(package_root: Path) -> dict:
    """Verify complete packaged bytes and their compiled build provenance."""
    root = _checked(package_root, directory=True)
    manifest_raw = _read(root / "manifest.json", root)
    manifest = _json(manifest_raw)
    if manifest.get("schema") != 1 or manifest.get("kind") != PACKAGE_KIND or manifest.get("command_abi") != 1:
        raise ModProjectError("invalid_package", "The directory is not a standalone native mod package")
    identity = _mod_id(manifest.get("mod_id"))
    revision = manifest.get("source_revision")
    if not isinstance(revision, str) or not _SHA.fullmatch(revision):
        raise ModProjectError("invalid_package", "The package requires its exact build input revision")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ModProjectError("invalid_package", "The package requires a complete file inventory")
    expected = set()
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ModProjectError("invalid_package", "Invalid package file inventory")
        relative = item["path"]
        _parts(relative)
        if relative == "manifest.json" or relative.casefold() in {path.casefold() for path in expected}:
            raise ModProjectError("duplicate_path", "The package file inventory contains duplicate or reserved entries")
        expected.add(relative)
        raw = _read(root / relative, root)
        if len(raw) != item.get("bytes") or _sha(raw) != item.get("sha256"):
            raise ModProjectError("package_changed", "A packaged file differs from its recorded bytes or SHA-256")
    if _file_set(root) != expected | {"manifest.json"}:
        raise ModProjectError("package_inventory_mismatch", "Package contents differ from the complete recorded inventory")
    artifact = manifest.get("artifact", {})
    if not isinstance(artifact, dict):
        raise ModProjectError("invalid_package", "The package artifact identity must be a JSON object")
    artifact_path = "bin/" + identity + ".dll"
    if artifact.get("path") != artifact_path or artifact_path not in expected:
        raise ModProjectError("invalid_package", "The package must contain its named compiled DLL")
    raw_dll = _read(root / artifact_path, root)
    pe = _pe(raw_dll)
    dependencies = _pe_imports(raw_dll)
    command_export = _command_export(raw_dll)
    if artifact.get("sha256") != _sha(raw_dll) or artifact.get("bytes") != len(raw_dll) or artifact.get("pe") != pe:
        raise ModProjectError("artifact_changed", "The compiled artifact identity differs from the package contract")
    for required in ("build-receipt.json", "source-manifest.json", "activation.json", "README.md"):
        if required not in expected:
            raise ModProjectError("invalid_package", "The package is missing its build, source or activation contract")
    receipt_raw = _read(root / "build-receipt.json", root)
    receipt = _json(receipt_raw)
    recipe = receipt.get("recipe", {})
    build = manifest.get("build", {})
    if not isinstance(recipe, dict) or not isinstance(build, dict) or not isinstance(receipt.get("artifacts"), list):
        raise ModProjectError("invalid_package", "The package requires structured build recipe and artifact identities")
    if (receipt.get("state") != "succeeded" or receipt.get("exact_source_compiled") is not True
            or recipe.get("input_manifest_sha256") != revision or build.get("input_manifest_sha256") != revision
            or build.get("receipt_sha256") != _sha(receipt_raw)
            or build.get("project_path") != recipe.get("project_path")):
        raise ModProjectError("build_provenance_mismatch", "The package does not identify a successful exact-source build")
    if _build_revision(recipe.get("inputs")) != revision:
        raise ModProjectError("build_provenance_mismatch", "The exact build input inventory does not match its source revision")
    candidates = [item for item in receipt.get("artifacts", [])
                  if isinstance(item, dict) and item.get("target") == "sn2_mod"
                  and item.get("sha256") == artifact["sha256"] and item.get("bytes") == artifact["bytes"]
                  and item.get("authoritative_build_output") is True and item.get("pe") == pe]
    if len(candidates) != 1:
        raise ModProjectError("build_provenance_mismatch", "The DLL is not the identified build output")
    source_manifest = _json(_read(root / "source-manifest.json", root))
    if source_manifest != {"input_manifest_sha256": revision, "inputs": recipe.get("inputs")}:
        raise ModProjectError("build_provenance_mismatch", "Packaged source identities differ from the compiled build inputs")
    unavailable = [item for item in receipt.get("artifacts", [])
                   if isinstance(item, dict) and item.get("available") is False and item.get("required") is False]
    if manifest.get("unavailable_optional_artifacts", []) != unavailable:
        raise ModProjectError("build_provenance_mismatch", "Optional unavailable build outputs differ from the recorded receipt")
    activation = _json(_read(root / "activation.json", root))
    if (activation.get("command_abi") != 1 or activation.get("artifact") != artifact_path
            or activation.get("mechanism") != "workspace_load_library_and_SN2ModCommand"
            or activation.get("requires_installation") is not False):
        raise ModProjectError("activation_contract_mismatch", "The package has no supported workspace activation contract")
    if manifest.get("dependencies") != dependencies or activation.get("dependencies") != dependencies:
        raise ModProjectError("dependency_contract_mismatch", "The package dependency inventory differs from the actual DLL imports")
    if manifest.get("command_export") != command_export:
        raise ModProjectError("mod_export_mismatch", "The command export identity differs from the actual DLL bytes")
    return {"source": "native_mod_artifact_package", "valid": True, "package_root": str(root),
            "manifest_sha256": _sha(manifest_raw), "metadata": manifest, "activation": activation,
            "artifact": {**artifact, "path": str(root / artifact_path), "package_path": artifact_path},
            "source_revision": revision, "file_count": len(entries), "compiled": True,
            "installed": False, "runtime_validation": "not_performed"}


class ModProjects:
    def __init__(self, workspace: SourceWorkspace, repository_root: Path):
        self.workspace = workspace
        self.repository = _checked(repository_root, directory=True)

    def _publish(self, destination: str, files: dict[str, bytes], *, artifacts: bool) -> Path:
        parts = _parts(destination)
        if artifacts and (len(parts) < 2 or parts[0] != "artifacts"):
            raise ModProjectError("unsafe_path", "Artifact packages require a new directory under workspace artifacts")
        if not artifacts:
            self.workspace._source_parts(destination)
        target = self.workspace._unprotected(self.workspace.root.joinpath(*parts), forbid_ancestor=True)
        with _destination_lock(target):
            # Parent creation is the same explicit workspace operation used by source tools.
            parent_relative = "/".join(parts[:-1])
            if parent_relative:
                if artifacts:
                    parent = self.workspace.root
                    for part in parts[:-1]:
                        child = self.workspace._unprotected(parent / part, forbid_ancestor=True)
                        with _guard_directories(parent, self.workspace.root):
                            if not os.path.lexists(child):
                                child.mkdir()
                        parent = _checked(child, self.workspace.root, directory=True)
                else:
                    self.workspace.create_directory(parent_relative)
            parent = _checked(target.parent, self.workspace.root, directory=True)
            with _guard_directories(parent, self.workspace.root):
                if os.path.lexists(target):
                    raise ModProjectError("already_exists", "The destination already exists")
                stage = Path(tempfile.mkdtemp(prefix=".sn2-mod-stage-", dir=parent))
                try:
                    for relative, raw in files.items():
                        _parts(relative)
                        path = stage / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("xb") as stream:
                            stream.write(raw)
                    if artifacts:
                        verify_artifact_package(stage)
                    stage.rename(target)
                except BaseException as error:
                    # Keep failed preparation recoverable in the coordination trash.
                    trash = self.repository.parent / ".trash" / self.repository.name / "mod-preparation"
                    trash.mkdir(parents=True, exist_ok=True)
                    recovered = trash / uuid.uuid4().hex
                    stage.rename(recovered)
                    if isinstance(error, (WorkspaceError, ModProjectError)):
                        raise
                    raise ModProjectError("publish_failed", "Preparation failed; partial files were moved to project trash",
                                          {"trash_path": str(recovered)}) from None
        return target

    def create(self, project_path: str, mod_id: str) -> dict:
        identity = _mod_id(mod_id)
        template = _checked(self.repository / "development/mod_templates/widget_opacity", self.repository, directory=True)
        files = {}
        origin = []
        for relative in TEMPLATE_FILES:
            raw = _read(template / relative, template)
            text = raw.decode("utf-8")
            files[relative] = text.replace("@MOD_ID@", identity).encode("utf-8")
            origin.append({"path": "development/mod_templates/widget_opacity/" + relative, "sha256": _sha(raw)})
        # Detect a mixed template revision before creating a project from it.
        for item in origin:
            if _sha(_read(self.repository / item["path"], self.repository)) != item["sha256"]:
                raise ModProjectError("template_changed", "The selected template changed while preparing the project")
        target = self._publish(project_path, files, artifacts=False)
        result = self.validate(project_path)
        return {**result, "created": True, "template_origin": origin,
                "build_recipe": {"project_path": project_path, "target": "sn2_mod", "configuration": "Release",
                                 "sdk_root": str(self.repository), "workspace_root": str(self.workspace.root),
                                 "source_revision": "supplied_by_exact_build_input_manifest"}}

    def validate(self, project_path: str) -> dict:
        self.workspace._selected(project_path)
        return validate_mod_project(self.workspace.root, project_path)

    def package(self, build_receipt_path: str, destination: str) -> dict:
        receipt_path = Path(build_receipt_path)
        if not receipt_path.is_absolute():
            receipt_path = self.repository.joinpath(*_parts(build_receipt_path))
        receipt_raw = _read(receipt_path, self.repository)
        receipt = _json(receipt_raw)
        recipe = receipt.get("recipe", {})
        if not isinstance(recipe, dict) or not isinstance(receipt.get("artifacts"), list):
            raise ModProjectError("invalid_manifest", "The build receipt requires a structured recipe and artifact list")
        revision = recipe.get("input_manifest_sha256")
        if (receipt.get("state") != "succeeded" or receipt.get("exact_source_compiled") is not True
                or not isinstance(revision, str) or not _SHA.fullmatch(revision)
                or not isinstance(recipe.get("inputs"), list) or not recipe["inputs"]):
            raise ModProjectError("build_not_succeeded", "Packaging requires a successful exact-source build receipt")
        if _build_revision(recipe["inputs"]) != revision:
            raise ModProjectError("build_provenance_mismatch", "The source inventory does not match the recorded build revision")
        # Identity comes from the bytes actually staged for compilation, not from
        # an edited current workspace revision that may already be different.
        source_root = _checked(Path(receipt.get("staged_source_root", "")), self.repository, directory=True)
        for item in recipe["inputs"]:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise ModProjectError("build_provenance_mismatch", "A staged build input has no recorded identity")
            _parts(item["path"])
            staged_raw = _read(source_root / item["path"], source_root)
            if _sha(staged_raw) != item.get("sha256") or len(staged_raw) != item.get("bytes"):
                raise ModProjectError("staged_source_changed", "A staged source differs from the successful build input")
        metadata_candidates = [item for item in recipe["inputs"]
                               if isinstance(item, dict) and item.get("path") == "project/mod.json"]
        if len(metadata_candidates) != 1:
            raise ModProjectError("build_provenance_mismatch", "The build does not contain one staged mod project manifest")
        metadata_entry = metadata_candidates[0]
        raw_metadata = _read(source_root / metadata_entry["path"], source_root)
        if _sha(raw_metadata) != metadata_entry.get("sha256"):
            raise ModProjectError("staged_source_changed", "The staged mod manifest differs from the build input")
        metadata = _json(raw_metadata)
        identity = _mod_id(metadata.get("mod_id"))
        if metadata.get("kind") != PROJECT_KIND or metadata.get("command_abi") != 1:
            raise ModProjectError("unsupported_project", "The build is not a supported standalone native mod project")
        candidates = [item for item in receipt.get("artifacts", [])
                      if isinstance(item, dict) and item.get("target") == "sn2_mod"
                      and item.get("authoritative_build_output") is True and item.get("available", True) is True
                      and isinstance(item.get("pe"), dict) and item["pe"].get("kind") == "dll"]
        if len(candidates) != 1:
            raise ModProjectError("missing_artifact", "The build must identify one compiled sn2_mod artifact")
        output = candidates[0]
        raw_dll = _read(Path(output.get("path", "")), self.repository)
        pe = _pe(raw_dll)
        dependencies = _pe_imports(raw_dll)
        command_export = _command_export(raw_dll)
        local_outputs = {Path(item["path"]).name.casefold(): item for item in receipt["artifacts"]
                         if isinstance(item, dict) and isinstance(item.get("path"), str)
                         and item.get("available", True) is True and isinstance(item.get("pe"), dict)
                         and item["pe"].get("kind") == "dll" and item != output}
        if any(item["module"].casefold() in local_outputs for item in dependencies):
            raise ModProjectError("unsupported_local_dependency", "The standalone package requires additional compiled DLL outputs not included by this project contract")
        if _sha(raw_dll) != output.get("sha256") or len(raw_dll) != output.get("bytes") or pe != output.get("pe"):
            raise ModProjectError("artifact_changed", "The build artifact differs from its receipt")
        artifact_path = "bin/" + identity + ".dll"
        activation = {"mechanism": "workspace_load_library_and_SN2ModCommand", "artifact": artifact_path,
                      "command_abi": 1, "requires_installation": False, "module_residency": "until_process_exit",
                      "operations": ["START", "STATUS", "DISABLE"], "code_revision_change": "controlled_game_restart",
                      "dependencies": dependencies,
                      "prerequisites": {"platform": "Windows x64", "game_process": "Subnautica2-Win64-Shipping.exe",
                                        "runtime": "MSVC Visual C++ runtime required by the compiled DLL imports"},
                      "effects": {"game_files_changed": False, "game_configuration_changed": False,
                                  "save_files_changed": False, "loads_package_dll_into_selected_process": True}}
        readme = ("# Standalone native modification package\n\n"
                  f"This package contains `{identity}.dll`, compiled from input revision `{revision}`.\n\n"
                  "Use the bridge's checked mod loading tool with this directory and an exact game process identity. "
                  "Loading the DLL and START initialization are separate states; inspect STATUS and independently observe the effect. "
                  "DISABLE attempts to restore the captured value. DLL and Tick trampoline residency ends when the selected game process exits. "
                  "A new compiled code revision requires controlled game restart. No game-root installation is required.\n\n"
                  "The target machine needs the MSVC Visual C++ runtime required by this DLL's imports; system runtime DLLs are not copied here. "
                  "File inventory integrity, compilation, installation and runtime acceptance are distinct results.\n").encode("utf-8")
        files = {artifact_path: raw_dll, "build-receipt.json": receipt_raw,
                 "source-manifest.json": _json_bytes({"input_manifest_sha256": revision, "inputs": recipe["inputs"]}),
                 "activation.json": _json_bytes(activation), "README.md": readme}
        artifact = {"path": artifact_path, "sha256": _sha(raw_dll), "bytes": len(raw_dll), "pe": pe}
        manifest = {"schema": 1, "kind": PACKAGE_KIND, "mod_id": identity, "command_abi": 1,
                    "source_revision": revision, "build": {"receipt_sha256": _sha(receipt_raw),
                    "project_path": recipe.get("project_path"), "input_manifest_sha256": revision},
                    "artifact": artifact, "files": _inventory(files), "compiled": True,
                    "dependencies": dependencies,
                    "command_export": command_export,
                    "unavailable_optional_artifacts": [item for item in receipt.get("artifacts", [])
                        if isinstance(item, dict) and item.get("available") is False and item.get("required") is False],
                    "installed": False, "runtime_validation": "not_performed"}
        files["manifest.json"] = _json_bytes(manifest)
        target = self._publish(destination, files, artifacts=True)
        result = verify_artifact_package(target)
        return {**result, "destination": destination, "created": True}

    def verify(self, package_path: str) -> dict:
        parts = _parts(package_path)
        if len(parts) < 2 or parts[0] != "artifacts":
            raise ModProjectError("unsafe_path", "Select a package directory under workspace artifacts")
        root = self.workspace._unprotected(_checked(self.workspace.root.joinpath(*parts), self.workspace.root, directory=True))
        return verify_artifact_package(root)
