"""Editable Unreal content plugins and identified cook/runtime packages.

Source preparation, compilation, installation, mounting and Blueprint activation
are distinct outcomes. This service does not synthesize cooked assets or load a
mod into the game.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from .workspace import (
    SourceWorkspace, WorkspaceError, _checked, _destination_lock,
    _guard_directories, _parts, _read_bytes,
)

PROJECT_KIND = "sn2_content_mod_project"
PACKAGE_KIND = "sn2_content_mod_package"
COOK_RECEIPT_KIND = "sn2_content_cook_receipt"
RUNTIME_BUILD_RECEIPT_KIND = "sn2_runtime_build_receipt"
TEMPLATE_FILES = ("Plugin.uplugin", "mod.json", "README.md")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_IDENTITY = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
_GENERATED_DIRECTORIES = SourceWorkspace._internal_directories | frozenset({
    "binaries", "intermediate", "saved", "deriveddatacache",
})
_PACKAGE_INTERNAL_DIRECTORIES = frozenset({".git", ".venv", ".trash", "__pycache__", "nppbackup"})
_CONTAINER_ROLES = {".pak": "pak", ".utoc": "utoc", ".ucas": "ucas"}


class ContentProjectError(Exception):
    def __init__(self, code: str, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.code = code
        self.receipt = receipt


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


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
        raise ContentProjectError("invalid_manifest", "JSON must be valid UTF-8 with unique keys") from None
    if not isinstance(value, dict):
        raise ContentProjectError("invalid_manifest", "The manifest must be a JSON object")
    return value


def _identity(value: object) -> str:
    if not isinstance(value, str) or not _IDENTITY.fullmatch(value):
        raise ContentProjectError("invalid_mod_id", "mod_id must begin with a letter and contain letters, digits or underscores")
    _parts(value)
    return value


def _entry_class(value: object, plugin: str) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.startswith("/" + plugin + "/"):
        raise ContentProjectError("invalid_entry_class", "entry_class must be a canonical generated-class path inside the selected plugin")
    # Match the native top-level asset parser while retaining ordinary Unicode
    # package names. Export-text wrappers and subobject paths are different APIs.
    if any(character.isspace() or ord(character) <= 0x20 or 0x7f <= ord(character) <= 0x9f
           or character in "\\:*?\"<>|' ,&!~@#;$^" for character in value):
        raise ContentProjectError("invalid_entry_class", "entry_class contains invalid characters or a subobject delimiter")
    parts = value.split("/")
    if len(parts) < 3 or parts[0] or any(not part or "." in part for part in parts[1:-1]):
        raise ContentProjectError("invalid_entry_class", "entry_class requires a canonical top-level asset path")
    names = parts[-1].split(".")
    if len(names) != 2 or not names[0] or names[1] != names[0] + "_C":
        raise ContentProjectError("invalid_entry_class", "entry_class must identify Asset.Asset_C without a subobject path")
    authored_path = "Content/" + "/".join([*parts[2:-1], names[0] + ".uasset"])
    _parts(authored_path)
    return value, authored_path


def _read(path: Path, boundary: Path) -> bytes:
    return _read_bytes(_checked(path, boundary), boundary, None)


def _absolute_path(value: object, field: str, *, runtime: bool = False) -> Path:
    code = "invalid_runtime_build_receipt" if runtime else "invalid_cook_receipt"
    producer = "runtime build" if runtime else "cook"
    if not isinstance(value, str) or not value:
        raise ContentProjectError(code, "The " + producer + " receipt requires an absolute " + field)
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ContentProjectError(code, "The " + producer + " receipt requires a canonical absolute " + field)
    return path


def _role(path: str, plugin: str) -> str:
    if path == plugin + ".uplugin":
        return "plugin_descriptor"
    if path == "mod.json":
        return "project_manifest"
    if path.startswith("Content/") and Path(path).suffix.casefold() in {".uasset", ".umap"}:
        return "authored_asset"
    return "authoring_source"


def _stream_identity(path: Path, boundary: Path) -> dict:
    """Hash complete binary assets in bounded memory and detect read races."""
    try:
        return _stream_identity_read(path, boundary)
    except OSError:
        raise WorkspaceError("access_error", "Could not hash the selected input file") from None


def _stream_identity_read(path: Path, boundary: Path) -> dict:
    path = _checked(path, boundary)
    if not path.is_file():
        raise WorkspaceError("invalid_path", "Inventory entries must be regular files")
    before = path.stat()
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_ino, opened.st_dev) != (before.st_ino, before.st_dev):
            raise WorkspaceError("file_changed", "The file changed while opening it")
        while raw := stream.read(1024 * 1024):
            digest.update(raw)
            size += len(raw)
        read = os.fstat(stream.fileno())
    after = _checked(path, boundary).stat()
    stamp = lambda value: (value.st_ino, value.st_dev, value.st_size, value.st_mtime_ns)
    if stamp(before) != stamp(read) or stamp(before) != stamp(after) or size != before.st_size:
        raise WorkspaceError("file_changed", "The file changed during hashing")
    return {"sha256": digest.hexdigest(), "bytes": size}


def _inventory(files: dict[str, bytes | Path], *, plugin: str | None = None, roles: dict[str, str] | None = None) -> list[dict]:
    result = []
    for path, source in sorted(files.items()):
        identity = _stream_identity(source, source.parent) if isinstance(source, Path) else {"sha256": _sha(source), "bytes": len(source)}
        result.append({"path": path, **identity, "role": roles[path] if roles is not None else _role(path, plugin or "")})
    return result


def source_manifest_sha256(inputs: object) -> str:
    """Identify the complete captured input inventory, excluding machine paths."""
    if not isinstance(inputs, list) or not inputs:
        raise ContentProjectError("source_provenance_mismatch", "A complete nonempty source inventory is required")
    content = []
    paths = set()
    for item in inputs:
        if not isinstance(item, dict):
            raise ContentProjectError("source_provenance_mismatch", "Invalid source input inventory")
        path, sha, size, role = (item.get(key) for key in ("path", "sha256", "bytes", "role"))
        if (not isinstance(path, str) or not isinstance(sha, str) or not _SHA.fullmatch(sha)
                or type(size) is not int or size < 0 or not isinstance(role, str) or not role):
            raise ContentProjectError("source_provenance_mismatch", "Source identity fields are invalid")
        _parts(path)
        if path.casefold() in paths:
            raise ContentProjectError("duplicate_path", "The source inventory contains duplicate Windows paths")
        paths.add(path.casefold())
        content.append({"path": path, "sha256": sha, "bytes": size, "role": role})
    return _sha(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _file_set(root: Path, *, source: bool = False, workspace: SourceWorkspace | None = None) -> set[str]:
    result = set()

    def fail(_error):
        raise ContentProjectError("access_error", "Could not enumerate the selected directory")

    for directory, children, names in os.walk(root, followlinks=False, onerror=fail):
        parent = _checked(Path(directory), root, directory=True)
        if workspace is not None:
            workspace._unprotected(parent)
        if source:
            children[:] = [name for name in children if name.casefold() not in _GENERATED_DIRECTORIES]
        for child in children:
            checked_child = _checked(parent / child, root, directory=True)
            if workspace is not None:
                workspace._unprotected(checked_child)
        for name in names:
            if source and name.casefold() in _GENERATED_DIRECTORIES:
                continue
            path = _checked(parent / name, root)
            if workspace is not None:
                workspace._unprotected(path)
            if not path.is_file():
                raise ContentProjectError("invalid_file", "Inventory entries must be ordinary files")
            relative = path.relative_to(root).as_posix()
            _parts(relative)
            result.add(relative)
    if len({path.casefold() for path in result}) != len(result):
        raise ContentProjectError("duplicate_path", "The directory contains duplicate Windows paths")
    return result


def _descriptor_issues(descriptor: dict, path: str) -> list[dict]:
    issues = []
    if descriptor.get("FileVersion") != 3:
        issues.append({"code": "invalid_descriptor", "path": path, "message": "The plugin descriptor requires FileVersion 3"})
    if descriptor.get("CanContainContent") is not True or descriptor.get("EnabledByDefault") is not True:
        issues.append({"code": "invalid_descriptor", "path": path,
                       "message": "The selected content plugin must enable content and default discovery"})
    if "Modules" in descriptor:
        issues.append({"code": "native_modules_unsupported", "path": path,
                       "message": "A content-only mod descriptor must not declare native Modules"})
    return issues


def validate_content_project(root: Path, project_path: str, *, workspace: SourceWorkspace | None = None) -> dict:
    """Inventory an editable plugin; no assets, compilation or activation are inferred."""
    workspace = workspace or SourceWorkspace(root)
    project = workspace._selected(project_path)
    files = {path: project / path for path in sorted(_file_set(project, source=True, workspace=workspace))}
    issues = []
    metadata = _json(_read(files["mod.json"], project) if "mod.json" in files else b"{}")
    try:
        plugin = _identity(metadata.get("plugin_name"))
        if _identity(metadata.get("mod_id")) != plugin:
            raise ContentProjectError("project_contract_mismatch", "mod_id and plugin_name must identify the same plugin")
    except ContentProjectError as error:
        plugin = _identity(project.name)
        issues.append({"code": error.code, "path": "mod.json", "message": str(error)})
    if metadata.get("schema") != 1 or metadata.get("kind") != PROJECT_KIND:
        issues.append({"code": "project_contract_mismatch", "path": "mod.json", "message": "mod.json does not identify a content/Blueprint mod project"})
    descriptor_path = plugin + ".uplugin"
    descriptor = _json(_read(files[descriptor_path], project) if descriptor_path in files else b"{}")
    issues.extend(_descriptor_issues(descriptor, descriptor_path))
    if {path for path in files if Path(path).suffix.casefold() == ".uplugin"} != {descriptor_path}:
        issues.append({"code": "descriptor_identity_mismatch", "path": descriptor_path,
                       "message": "The project requires exactly its named plugin descriptor"})
    if not (project / "Content").is_dir():
        issues.append({"code": "content_root_missing", "path": "Content", "message": "The editable content root is missing"})
    if any(Path(path).suffix.casefold() in {".dll", ".exe"} for path in files):
        issues.append({"code": "native_payload_unsupported", "message": "The selected mod format does not include native executable payloads"})
    entry = None
    entry_asset = None
    try:
        entry, entry_asset = _entry_class(metadata.get("entry_class"), plugin)
        if entry_asset is not None and entry_asset not in files:
            issues.append({"code": "entry_asset_missing", "path": entry_asset,
                           "message": "The declared generated class has no authored asset file in this project"})
    except ContentProjectError as error:
        issues.append({"code": error.code, "path": "mod.json", "message": str(error)})
    inventory = _inventory(files, plugin=plugin)
    authored = [item for item in inventory if item["role"] == "authored_asset"]
    return {"source": "content_mod_project", "project_path": project_path, "project_root": str(project),
            "plugin_id": plugin, "metadata": metadata, "descriptor": descriptor,
            "valid": not issues, "issues": issues, "files": inventory,
            "project_source_manifest_sha256": source_manifest_sha256(inventory),
            "authored_assets": authored, "authored_assets_present": bool(authored),
            "entry_class": entry, "entry_asset_path": entry_asset,
            "entry_asset_present": entry_asset is not None and entry_asset in files,
            "compiled": False, "installed": False, "runtime_validation": "not_performed",
            "build_prerequisites": {"workflow": "compatible_Subnautica2_Modkit_custom_Unreal_cooker",
                                    "platform": "Win64", "configuration": "Shipping",
                                    "cooked_format": ["pak", "utoc", "ucas"]}}


def _receipt(receipt: dict) -> tuple[str, str, list[dict]]:
    if (receipt.get("kind") != COOK_RECEIPT_KIND or receipt.get("state") != "succeeded"
            or receipt.get("exact_source_compiled") is not True):
        raise ContentProjectError("cook_not_succeeded", "Packaging requires a successful exact-source content cook receipt")
    plugin = _identity(receipt.get("plugin_id"))
    revision = receipt.get("source_manifest_sha256")
    inputs = receipt.get("inputs")
    if not isinstance(revision, str) or source_manifest_sha256(inputs) != revision:
        raise ContentProjectError("source_provenance_mismatch", "The cook input inventory does not match its source revision")
    if any(item["role"] != _role(item["path"], plugin) for item in inputs):
        raise ContentProjectError("source_provenance_mismatch", "The source roles do not match the identified project files")
    if not any(item["role"] == "authored_asset" for item in inputs):
        raise ContentProjectError("source_provenance_mismatch", "A successful content cook must identify authored assets")
    dependencies = receipt.get("shared_dependencies")
    dependency_revision = receipt.get("dependency_manifest_sha256")
    if not isinstance(dependency_revision, str) or source_manifest_sha256(dependencies) != dependency_revision:
        raise ContentProjectError("dependency_provenance_mismatch", "The cook dependency inventory does not match its recorded revision")
    identity = receipt.get("build_identity")
    if (not isinstance(identity, dict) or not isinstance(identity.get("build_id"), str) or not identity["build_id"]
            or identity.get("source_manifest_sha256") != revision
            or identity.get("dependency_manifest_sha256") != dependency_revision
            or identity.get("target_platform") != "Win64" or identity.get("configuration") != "Shipping"
            or identity.get("base_release") != "SN2Base"):
        raise ContentProjectError("cook_provenance_mismatch", "The cook requires an identified target, source and dependency build identity")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, list) or not outputs:
        raise ContentProjectError("missing_cook_output", "The cook receipt requires identified output files")
    _parts(receipt.get("project_path"))
    return plugin, revision, inputs


def _runtime_receipt(receipt: dict) -> tuple[str, str, list[dict]]:
    if (receipt.get("kind") != RUNTIME_BUILD_RECEIPT_KIND or receipt.get("schema") != 1
            or receipt.get("method") != "runtime_blueprint"):
        raise ContentProjectError("invalid_runtime_build_receipt", "Select an actual runtime Blueprint build receipt")
    if receipt.get("containerized") is not True:
        raise ContentProjectError("containerization_not_completed", "Raw serialized assets are not an installable Pak/IoStore package")
    if (receipt.get("state") != "succeeded" or receipt.get("compiled") is not True
            or receipt.get("persisted") is not True or receipt.get("exact_source_compiled") is not True):
        raise ContentProjectError("runtime_build_not_succeeded", "Packaging requires a successful exact-source compiled and persisted runtime build")
    plugin = _identity(receipt.get("plugin_id"))
    revision, inputs = receipt.get("source_manifest_sha256"), receipt.get("inputs")
    if not isinstance(revision, str) or source_manifest_sha256(inputs) != revision:
        raise ContentProjectError("source_provenance_mismatch", "Runtime build inputs do not match their source revision")
    if any(item["role"] != _role(item["path"], plugin) for item in inputs):
        raise ContentProjectError("source_provenance_mismatch", "Runtime source roles do not match the identified project files")
    identity = receipt.get("build_identity")
    if (not isinstance(identity, dict) or not isinstance(identity.get("build_id"), str) or not identity["build_id"]
            or identity.get("source_manifest_sha256") != revision
            or identity.get("target_platform") != "Win64" or identity.get("configuration") != "Shipping"):
        raise ContentProjectError("runtime_build_provenance_mismatch", "The runtime build requires its source and Shipping target identity")
    specification = receipt.get("compiler_specification")
    specification_revision = receipt.get("specification_sha256")
    if (not isinstance(specification, dict) or not isinstance(specification_revision, str)
            or not _SHA.fullmatch(specification_revision) or specification.get("sha256") != specification_revision
            or type(specification.get("bytes")) is not int or specification["bytes"] <= 0
            or identity.get("specification_sha256") != specification_revision):
        raise ContentProjectError("runtime_build_provenance_mismatch", "The runtime build must identify its exact captured compiler specification")
    _absolute_path(specification.get("source_path"), "compiler_specification.source_path", runtime=True)
    generated = receipt.get("generated_class_paths")
    if (not isinstance(generated, list) or not generated or any(not isinstance(path, str) for path in generated)
            or len({path.casefold() for path in generated}) != len(generated)):
        raise ContentProjectError("runtime_build_provenance_mismatch", "The runtime build requires its generated-class identities")
    for path in generated:
        _entry_class(path, plugin)
    assets = receipt.get("compiled_asset_paths")
    if (not isinstance(assets, list) or any(not isinstance(path, str) for path in assets)
            or len(assets) != len(generated) or set(assets) != {path[:-2] for path in generated}):
        raise ContentProjectError("runtime_build_provenance_mismatch", "The compiled assets must identify the generated classes in this runtime build")
    if not isinstance(receipt.get("outputs"), list) or not receipt["outputs"]:
        raise ContentProjectError("containerization_not_completed", "The runtime build has no identified container outputs")
    _parts(receipt.get("project_path"))
    return plugin, revision, inputs


def _runtime_entry(receipt: dict, entry: str | None) -> None:
    if entry is not None and entry not in receipt["generated_class_paths"]:
        raise ContentProjectError("source_provenance_mismatch", "The declared entry class was not compiled in the selected runtime build")


def _runtime_specification(receipt: dict, raw: bytes) -> None:
    evidence = receipt["compiler_specification"]
    if {"sha256": _sha(raw), "bytes": len(raw)} != {key: evidence[key] for key in ("sha256", "bytes")}:
        raise ContentProjectError("compiler_specification_changed", "The compiler specification differs from the selected exact-source runtime build")
    specification = _json(raw)
    assets = specification.get("assets")
    if (specification.get("schema") != 1 or specification.get("plugin_id") != receipt["plugin_id"]
            or not isinstance(assets, list) or not all(isinstance(asset, dict) for asset in assets)
            or not set(receipt["compiled_asset_paths"]) <= {asset.get("path") for asset in assets if isinstance(asset.get("path"), str)}):
        raise ContentProjectError("runtime_build_provenance_mismatch", "The compiler specification does not identify the runtime build's compiled assets")


def _plugin_config_inputs(inputs: list[dict]) -> list[dict]:
    """Select the game's supported loose plugin configuration from captured source."""
    return [item for item in inputs if item["path"] == "Config/Game.ini"]


def _container_contract(entries: list[dict], plugin: str, *, missing_code: str = "missing_cook_output") -> list[dict]:
    containers = []
    groups = {}
    for item in entries:
        path = item["path"]
        role = item.get("role")
        if role not in _CONTAINER_ROLES.values():
            continue
        parts = _parts(path)
        suffix = Path(path).suffix.casefold()
        if (len(parts) < 5 or parts[:4] != (plugin, "Content", "Paks", "Windows")
                or _CONTAINER_ROLES.get(suffix) != role or item.get("bytes", 0) <= 0):
            raise ContentProjectError("invalid_container_layout", "Cooked containers must have their recorded role and plugin Content/Paks/Windows layout")
        stem = path[:-len(suffix)].casefold()
        roles = groups.setdefault(stem, set())
        if role in roles:
            raise ContentProjectError("duplicate_path", "The package repeats a cooked container role")
        roles.add(role)
        containers.append(item)
    if not groups or any(roles != {"pak", "utoc", "ucas"} for roles in groups.values()):
        raise ContentProjectError(missing_code, "Each selected IoStore container stem requires .pak, .utoc and .ucas outputs")
    return containers


def verify_content_package(package_root: Path) -> dict:
    """Recheck packaged bytes and their source/build correlation without installation."""
    root = _checked(package_root, directory=True)
    manifest_raw = _read(root / "manifest.json", root)
    manifest = _json(manifest_raw)
    if manifest.get("schema") != 1 or manifest.get("kind") != PACKAGE_KIND:
        raise ContentProjectError("invalid_package", "The directory is not a content/Blueprint mod package")
    plugin = _identity(manifest.get("plugin_id"))
    if manifest.get("mod_id") != plugin:
        raise ContentProjectError("invalid_package", "The package mod and plugin identities differ")
    entries = manifest.get("files")
    source_manifest_sha256(entries)
    expected = set()
    files = {}
    for item in entries:
        path = item["path"]
        if path.casefold() == "manifest.json":
            raise ContentProjectError("invalid_package", "The file inventory cannot include its own manifest")
        identity = _stream_identity(root / path, root)
        if identity["bytes"] != item["bytes"] or identity["sha256"] != item["sha256"]:
            raise ContentProjectError("package_changed", "A packaged file differs from its recorded bytes or SHA-256")
        expected.add(path)
        files[path] = root / path
    if _file_set(root) != expected | {"manifest.json"}:
        raise ContentProjectError("package_inventory_mismatch", "Package contents differ from the complete file inventory")
    build = manifest.get("build")
    runtime = isinstance(build, dict) and build.get("method") == "runtime_blueprint"
    receipt_name = "runtime-build-receipt.json" if runtime else "cook-receipt.json"
    provenance_code = "runtime_build_provenance_mismatch" if runtime else "cook_provenance_mismatch"
    output_marker = "authoritative_runtime_output" if runtime else "authoritative_cook_output"
    required = {receipt_name, "source-manifest.json", "project-metadata.json", "activation.json", "README.md", plugin + "/" + plugin + ".uplugin"}
    if runtime:
        required.add("compiler-specification.json")
    if not required <= expected:
        raise ContentProjectError("invalid_package", "The package lacks its descriptor, build, source or activation contract")
    if any(Path(path).suffix.casefold() in {".dll", ".exe"} for path in expected):
        raise ContentProjectError("native_payload_unsupported", "A content/Blueprint package must not include native executable payloads")
    receipt_raw = _read(files[receipt_name], root)
    receipt = _json(receipt_raw)
    receipt_plugin, revision, inputs = (_runtime_receipt if runtime else _receipt)(receipt)
    if (not isinstance(build, dict) or receipt_plugin != plugin or manifest.get("source_revision") != revision
            or build.get("source_manifest_sha256") != revision or build.get("receipt_sha256") != _sha(receipt_raw)
            or build.get("project_path") != receipt.get("project_path")
            or build.get("identity") != receipt.get("build_identity")):
        raise ContentProjectError(provenance_code, "The package does not identify its exact successful build")
    if runtime:
        if build.get("specification_sha256") != receipt["specification_sha256"]:
            raise ContentProjectError(provenance_code, "The package does not identify its exact compiler specification")
        specification_entry = next(item for item in entries if item["path"] == "compiler-specification.json")
        if specification_entry["role"] != "compiler_specification":
            raise ContentProjectError(provenance_code, "The captured compiler specification requires its identified package role")
        _runtime_specification(receipt, _read(files["compiler-specification.json"], root))
    if _json(_read(files["source-manifest.json"], root)) != {"source_manifest_sha256": revision, "inputs": inputs}:
        raise ContentProjectError("source_provenance_mismatch", "Packaged source identities differ from the build inputs")
    metadata_raw = _read(files["project-metadata.json"], root)
    source_metadata = _json(metadata_raw)
    metadata_inputs = [item for item in inputs if item["path"] == "mod.json" and item["role"] == "project_manifest"]
    if (len(metadata_inputs) != 1 or metadata_inputs[0]["sha256"] != _sha(metadata_raw)
            or metadata_inputs[0]["bytes"] != len(metadata_raw)
            or source_metadata.get("schema") != 1 or source_metadata.get("kind") != PROJECT_KIND
            or source_metadata.get("mod_id") != plugin or source_metadata.get("plugin_name") != plugin):
        raise ContentProjectError("source_provenance_mismatch", "The packaged project metadata differs from the captured input")
    descriptor_path = plugin + "/" + plugin + ".uplugin"
    descriptor_raw = _read(files[descriptor_path], root)
    descriptor = _json(descriptor_raw)
    if _descriptor_issues(descriptor, descriptor_path):
        raise ContentProjectError("invalid_descriptor", "The package descriptor is not the selected content-only plugin contract")
    descriptor_inputs = [item for item in inputs if item["path"] == plugin + ".uplugin" and item["role"] == "plugin_descriptor"]
    if (len(descriptor_inputs) != 1 or descriptor_inputs[0]["sha256"] != _sha(descriptor_raw)
            or descriptor_inputs[0]["bytes"] != len(descriptor_raw)):
        raise ContentProjectError("source_provenance_mismatch", "The packaged descriptor differs from the captured source descriptor")
    config_inputs = _plugin_config_inputs(inputs)
    config_paths = {plugin + "/" + item["path"] for item in config_inputs}
    configs = [item for item in entries if item["path"] in config_paths]
    if len(configs) != len(config_inputs) or (configs and manifest.get("configs") != configs):
        raise ContentProjectError("source_provenance_mismatch", "The package must identify every selected captured plugin configuration")
    if not configs and "configs" in manifest:
        raise ContentProjectError("source_provenance_mismatch", "The package declares configuration absent from its captured source")
    for source in config_inputs:
        config = next(item for item in configs if item["path"] == plugin + "/" + source["path"])
        if (config["role"] != "plugin_config" or any(config[key] != source[key] for key in ("sha256", "bytes"))):
            raise ContentProjectError("source_provenance_mismatch", "A loose plugin configuration differs from its captured source input")
    containers = _container_contract(entries, plugin, missing_code="containerization_not_completed" if runtime else "missing_cook_output")
    if manifest.get("containers") != containers:
        raise ContentProjectError(provenance_code, "The package container identities differ from its inventory")
    payload = [item for item in entries if item["path"].startswith(plugin + "/")]
    if len(payload) != len(containers) + 1 + len(configs) or manifest.get("descriptor") != next(item for item in payload if item["path"] == descriptor_path):
        raise ContentProjectError("invalid_package", "The selected game payload must contain its descriptor, cooked containers and captured plugin configuration")
    if expected != required | {item["path"] for item in containers} | config_paths:
        raise ContentProjectError("invalid_package", "The package contains entries outside its complete content/build contract")
    outputs = receipt["outputs"]
    build_payload = [item for item in payload if item["path"] not in config_paths]
    if len(outputs) != len(build_payload):
        raise ContentProjectError(provenance_code, "The package does not account for every identified build output")
    output_root = _absolute_path(receipt.get("output_root"), "output_root", runtime=runtime)
    output_paths = set()
    for output in outputs:
        if (not isinstance(output, dict) or not isinstance(output.get("path"), str)
                or output.get(output_marker) is not True):
            raise ContentProjectError(provenance_code, "Invalid build output identity")
        absolute = _absolute_path(output["path"], "output path", runtime=runtime)
        if not absolute.is_relative_to(output_root):
            raise ContentProjectError(provenance_code, "A build output is outside its recorded output root")
        relative = absolute.relative_to(output_root).as_posix()
        _parts(relative)
        package_path = plugin + "/" + relative
        if package_path.casefold() in output_paths:
            raise ContentProjectError("duplicate_path", "The build output inventory repeats a Windows path")
        output_paths.add(package_path.casefold())
        candidate = next((item for item in build_payload if item["path"] == package_path), None)
        if candidate is None or any(candidate[key] != output.get(key) for key in ("sha256", "bytes", "role")):
            raise ContentProjectError(provenance_code, "A packaged payload differs from its identified build output")
    activation = _json(_read(files["activation.json"], root))
    if (manifest.get("activation") != activation or activation.get("plugin_name") != plugin
            or activation.get("mount_root") != "/" + plugin + "/"
            or activation.get("entry_class") != source_metadata.get("entry_class")
            or activation.get("replacement_boundary") != "game_restart"):
        raise ContentProjectError("activation_contract_mismatch", "The package activation identity is inconsistent")
    entry, entry_asset = _entry_class(activation.get("entry_class"), plugin)
    if runtime:
        _runtime_entry(receipt, entry)
    elif entry_asset is not None and not any(item["path"] == entry_asset and item["role"] == "authored_asset" for item in inputs):
        raise ContentProjectError("source_provenance_mismatch", "The entry class lacks an identified authored source asset")
    return {"source": "content_mod_artifact_package", "valid": True, "package_root": str(root),
            "manifest_sha256": _sha(manifest_raw), "metadata": manifest, "plugin_id": plugin,
            "files": entries, "activation": activation, "containers": containers,
            "descriptor": manifest["descriptor"], "source_revision": revision,
            **({"configs": configs} if configs else {}),
            **({"specification_sha256": receipt["specification_sha256"]} if runtime else {}),
            "file_count": len(entries), "compiled": True, "installed": False,
            "container_validation": ("identified_runtime_outputs_not_fresh_load_verification" if runtime
                                     else "identified_cooker_outputs_not_runtime_mount_verification"),
            "runtime_validation": "not_performed"}


class ContentProjects:
    def __init__(self, workspace: SourceWorkspace, repository_root: Path):
        self.workspace = workspace
        self.repository = _checked(repository_root, directory=True)

    def _publish(self, destination: str, files: dict[str, bytes | Path], *, package: bool) -> Path:
        parts = _parts(destination)
        if not package:
            self.workspace._source_parts(destination)
        elif any(part.casefold() in _PACKAGE_INTERNAL_DIRECTORIES for part in parts):
            raise ContentProjectError("unsafe_path", "Package destinations must not use repository internals, environments or trash")
        target = self.workspace._unprotected(self.workspace.root.joinpath(*parts), forbid_ancestor=True)
        with _destination_lock(target):
            parent = self.workspace.root
            for part in parts[:-1]:
                child = self.workspace._unprotected(parent / part, forbid_ancestor=True)
                with _guard_directories(parent, self.workspace.root):
                    if not os.path.lexists(child):
                        child.mkdir()
                parent = _checked(child, self.workspace.root, directory=True)
            with _guard_directories(parent, self.workspace.root):
                if os.path.lexists(target):
                    raise ContentProjectError("already_exists", "The destination already exists")
                stage = Path(tempfile.mkdtemp(prefix=".sn2-content-stage-", dir=parent))
                try:
                    for relative, source in files.items():
                        _parts(relative)
                        path = stage / relative
                        path.parent.mkdir(parents=True, exist_ok=True)
                        with path.open("xb") as stream:
                            if isinstance(source, Path):
                                source = _checked(source, source.parent)
                                with source.open("rb") as source_stream:
                                    shutil.copyfileobj(source_stream, stream, length=1024 * 1024)
                            else:
                                stream.write(source)
                    if package:
                        verify_content_package(stage)
                    else:
                        (stage / "Content").mkdir(exist_ok=True)
                    stage.rename(target)
                except BaseException as error:
                    trash = self.repository.parent / ".trash" / self.repository.name / "content-preparation"
                    trash.mkdir(parents=True, exist_ok=True)
                    recovered = trash / uuid.uuid4().hex
                    shutil.move(str(stage), str(recovered))
                    if isinstance(error, (WorkspaceError, ContentProjectError)):
                        raise
                    raise ContentProjectError("publish_failed", "Preparation failed; partial files moved to project trash",
                                              {"trash_path": str(recovered)}) from None
        return target

    def create(self, project_path: str, mod_id: str, title: str | None = None, entry_class: str | None = None) -> dict:
        plugin = _identity(mod_id)
        if title is None:
            title = plugin
        if not isinstance(title, str) or not title.strip() or any(ord(character) < 32 for character in title):
            raise ContentProjectError("invalid_title", "The project title must be nonempty text without control characters")
        _entry_class(entry_class, plugin)
        template = _checked(self.repository / "development/mod_templates/content_plugin", self.repository, directory=True)
        raw_templates = {name: _read(template / name, template) for name in TEMPLATE_FILES}
        descriptor = _json(raw_templates["Plugin.uplugin"])
        descriptor["FriendlyName"] = title
        metadata = _json(raw_templates["mod.json"])
        metadata.update({"mod_id": plugin, "plugin_name": plugin, "title": title, "entry_class": entry_class})
        files = {plugin + ".uplugin": _json_bytes(descriptor), "mod.json": _json_bytes(metadata),
                 "README.md": raw_templates["README.md"].replace(b"@PLUGIN_ID@", plugin.encode("ascii"))}
        origins = [{"path": "development/mod_templates/content_plugin/" + name, "sha256": _sha(raw)}
                   for name, raw in raw_templates.items()]
        for item in origins:
            if _sha(_read(self.repository / item["path"], self.repository)) != item["sha256"]:
                raise ContentProjectError("template_changed", "The selected content template changed during project preparation")
        self._publish(project_path, files, package=False)
        return {**self.validate(project_path), "created": True, "template_origin": origins,
                "authoring_required": True,
                "build_recipe": {"workflow": "content", "project_path": project_path,
                                 "plugin_id": plugin, "workspace_root": str(self.workspace.root),
                                 "configuration": "Shipping", "platform": "Win64"}}

    def validate(self, project_path: str) -> dict:
        self.workspace._selected(project_path)
        return validate_content_project(self.workspace.root, project_path, workspace=self.workspace)

    def package(self, cook_receipt_path: str, destination: str) -> dict:
        return self._package(cook_receipt_path, destination, runtime=False)

    def runtime_package(self, runtime_build_receipt: str, destination: str) -> dict:
        """Publish identified native-built containers without fabricating a cook."""
        return self._package(runtime_build_receipt, destination, runtime=True)

    def _package(self, producer_receipt_path: str, destination: str, *, runtime: bool) -> dict:
        path = Path(producer_receipt_path)
        if not path.is_absolute():
            path = self.repository.joinpath(*_parts(producer_receipt_path))
        receipt_path = _checked(path)
        receipt_raw = _read(receipt_path, receipt_path.parent)
        receipt = _json(receipt_raw)
        plugin, revision, inputs = (_runtime_receipt if runtime else _receipt)(receipt)
        receipt_name = "runtime-build-receipt.json" if runtime else "cook-receipt.json"
        receipt_role = "runtime_build_receipt" if runtime else "cook_receipt"
        output_marker = "authoritative_runtime_output" if runtime else "authoritative_cook_output"
        provenance_code = "runtime_build_provenance_mismatch" if runtime else "cook_provenance_mismatch"
        captured = _checked(_absolute_path(receipt.get("staged_source_root"), "staged_source_root", runtime=runtime), directory=True)
        captured_files = {}
        for item in inputs:
            source = captured.joinpath(*_parts(item["path"]))
            if not isinstance(item.get("source_path"), str) or Path(item["source_path"]) != source:
                raise ContentProjectError("source_provenance_mismatch", "The source identity does not name its captured input")
            identity = _stream_identity(source, captured)
            if identity["sha256"] != item["sha256"] or identity["bytes"] != item["bytes"]:
                raise ContentProjectError("captured_source_changed", "A captured source input differs from the successful cook")
            captured_files[item["path"]] = source
        if _file_set(captured, source=True) != set(captured_files):
            raise ContentProjectError("source_provenance_mismatch", "The captured source directory differs from the complete input inventory")
        if any(Path(path).suffix.casefold() in {".dll", ".exe"} for path in captured_files):
            raise ContentProjectError("native_payload_unsupported", "Captured content project inputs must not include native executable payloads")
        metadata_raw = _read(captured_files["mod.json"], captured) if "mod.json" in captured_files else b"{}"
        metadata = _json(metadata_raw)
        if (metadata.get("schema") != 1 or metadata.get("kind") != PROJECT_KIND
                or metadata.get("mod_id") != plugin or metadata.get("plugin_name") != plugin):
            raise ContentProjectError("source_provenance_mismatch", "The captured source is not the selected content/Blueprint project")
        entry, entry_asset = _entry_class(metadata.get("entry_class"), plugin)
        if runtime:
            _runtime_entry(receipt, entry)
        elif entry_asset is not None and entry_asset not in captured_files:
            raise ContentProjectError("source_provenance_mismatch", "The captured entry class has no authored source asset")
        output_root = _checked(_absolute_path(receipt.get("output_root"), "output_root", runtime=runtime), directory=True)
        files = {}
        roles = {}
        for item in receipt["outputs"]:
            if (not isinstance(item, dict) or not isinstance(item.get("path"), str)
                    or item.get(output_marker) is not True):
                raise ContentProjectError("missing_runtime_output" if runtime else "missing_cook_output", "A build output lacks its producer and path identity")
            output = _absolute_path(item["path"], "output path", runtime=runtime)
            output = _checked(output, output_root)
            identity = _stream_identity(output, output_root)
            if identity["sha256"] != item.get("sha256") or identity["bytes"] != item.get("bytes"):
                raise ContentProjectError("runtime_output_changed" if runtime else "cook_output_changed", "A build output differs from its successful receipt")
            relative = output.relative_to(output_root).as_posix()
            _parts(relative)
            destination_path = plugin + "/" + relative
            if destination_path.casefold() in {value.casefold() for value in files}:
                raise ContentProjectError("duplicate_path", "The cook output inventory repeats a Windows path")
            files[destination_path] = output
            roles[destination_path] = item.get("role")
        if _file_set(output_root) != {path[len(plugin) + 1:] for path in files}:
            raise ContentProjectError(provenance_code, "The build output directory differs from its complete output inventory")
        descriptor_path = plugin + "/" + plugin + ".uplugin"
        if descriptor_path not in files or plugin + ".uplugin" not in captured_files or roles.get(descriptor_path) != "plugin_descriptor":
            raise ContentProjectError("source_provenance_mismatch", "The built descriptor differs from the captured source descriptor")
        descriptor_raw = _read(files[descriptor_path], output_root)
        if descriptor_raw != _read(captured_files[plugin + ".uplugin"], captured):
            raise ContentProjectError("source_provenance_mismatch", "The cooked descriptor differs from the captured source descriptor")
        descriptor = _json(descriptor_raw)
        if _descriptor_issues(descriptor, descriptor_path):
            raise ContentProjectError("invalid_descriptor", "The captured plugin descriptor is not content-only")
        payload = _inventory(files, roles=roles)
        containers = _container_contract(payload, plugin, missing_code="containerization_not_completed" if runtime else "missing_cook_output")
        if len(payload) != len(containers) + 1:
            raise ContentProjectError("invalid_package", "The selected payload must contain only its descriptor and cooked containers")
        config_files = {}
        for item in _plugin_config_inputs(inputs):
            config_path = plugin + "/" + item["path"]
            files[config_path] = captured_files[item["path"]]
            config_files[config_path] = files[config_path]
            roles[config_path] = "plugin_config"
        configs = _inventory(config_files, roles=roles)
        activation = {"plugin_name": plugin, "mount_root": "/" + plugin + "/", "entry_class": entry,
                      "mechanism": ("shipping_plugin_discovery_and_native_bridge_asset_activation" if entry
                                    else "shipping_plugin_discovery"),
                      "replacement_boundary": "game_restart", "requires_installation": True,
                      "runtime_validation": "not_performed", "entry_class_load_verified": False}
        activation_description = (
            "Install the selected plugin subtree through the bridge's installation tool. "
            "Use a fresh game process to discover the selected revision, inspect plugin/mount "
            "state, and independently verify content registration and authored Blueprint "
            "effects. A generated-class reference identifies intended activation; it does "
            "not prove class loading or BeginPlay. "
            if entry else
            "With the game closed, install the selected plugin subtree in Subnautica2/Mods. "
            "Start a fresh game process to discover the selected revision and independently "
            "verify plugin/mount state and content registration. No Blueprint entry is declared "
            "for native bridge activation. Captured plugin configuration, when present, may "
            "select game-owned startup classes; verify that the game applies it and that the "
            "intended behavior occurs. Discovery alone does not execute arbitrary content. "
        )
        files.update({receipt_name: receipt_raw,
                      "source-manifest.json": _json_bytes({"source_manifest_sha256": revision, "inputs": inputs}),
                      "project-metadata.json": metadata_raw,
                      "activation.json": _json_bytes(activation),
                      "README.md": (("# Runtime-built content and Blueprint modification package\n\n" if runtime
                                     else "# Cooked content and Blueprint modification package\n\n") +
                                    f"Plugin `{plugin}` was produced from identified source revision `{revision}`. "
                                    + ("The build ran through the native runtime Blueprint backend. " if runtime else "") +
                                    "The descriptor and cooked Pak/IoStore containers are recorded in manifest.json. "
                                    "This package preparation does not install, mount or activate the mod.\n\n"
            + activation_description + "Replacement uses confirmed game restart.\n").encode("utf-8")})
        roles.update({receipt_name: receipt_role, "source-manifest.json": "source_inventory",
                      "project-metadata.json": "project_manifest",
                      "activation.json": "activation_manifest", "README.md": "package_documentation"})
        if runtime:
            specification_path = _absolute_path(receipt["compiler_specification"]["source_path"],
                                                "compiler_specification.source_path", runtime=True)
            specification_raw = _read(specification_path, specification_path.parent)
            _runtime_specification(receipt, specification_raw)
            files["compiler-specification.json"] = specification_raw
            roles["compiler-specification.json"] = "compiler_specification"
        manifest = {"schema": 1, "kind": PACKAGE_KIND, "mod_id": plugin, "plugin_id": plugin,
                    "source_revision": revision,
                    "build": {"receipt_path": str(receipt_path), "receipt_sha256": _sha(receipt_raw),
                              "source_manifest_sha256": revision, "project_path": receipt.get("project_path"),
                              "identity": receipt.get("build_identity"),
                              **({"method": "runtime_blueprint", "specification_sha256": receipt["specification_sha256"]}
                                 if runtime else {})},
                    "descriptor": next(item for item in payload if item["path"] == descriptor_path),
                    "containers": containers, "files": _inventory(files, roles=roles), "activation": activation,
                    **({"configs": configs} if configs else {}),
                    "compiled": True, "installed": False, "runtime_validation": "not_performed"}
        files["manifest.json"] = _json_bytes(manifest)
        target = self._publish(destination, files, package=True)
        return {**verify_content_package(target), "created": True, "destination": destination}

    def verify(self, package_path: str) -> dict:
        parts = _parts(package_path)
        if any(part.casefold() in _PACKAGE_INTERNAL_DIRECTORIES for part in parts):
            raise ContentProjectError("unsafe_path", "Select a package outside repository internals, environments and trash")
        root = self.workspace._unprotected(_checked(self.workspace.root.joinpath(*parts), self.workspace.root, directory=True))
        return verify_content_package(root)
