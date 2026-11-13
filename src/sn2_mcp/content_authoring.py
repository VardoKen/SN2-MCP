"""Generate editable Unreal assets with the captured Editor-only authoring helper.

The helper is build-time tooling. Published mods contain only ordinary content
assets; this service does not cook, install or execute them in a game.
"""
from __future__ import annotations

from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Callable
import uuid

from .build_workflow import BuildError, _guard_input_files, _positive, run_build_command
from .content_cooker import ContentCookError, _file_info as _cook_file_info, _git, _inventory as _cook_inventory, _manifest, _write_json
from .content_projects import ContentProjectError, _GENERATED_DIRECTORIES, _json, validate_content_project
from .workspace import SourceWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories, _parts, _read_bytes

HELPER_NAME = "SN2MCPAuthoring"
RECEIPT_KIND = "sn2_content_authoring_receipt"
Runner = Callable[[list[str], Path, float, Path, Path], dict]


class ContentAuthoringError(Exception):
    def __init__(self, code: str, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.code, self.receipt = code, receipt


def _file_info(*args, **kwargs) -> dict:
    try:
        return _cook_file_info(*args, **kwargs)
    except ContentCookError as error:
        raise ContentAuthoringError(error.code, str(error), error.receipt) from None


def _inventory(*args, **kwargs) -> list[dict]:
    try:
        return _cook_inventory(*args, **kwargs)
    except ContentCookError as error:
        raise ContentAuthoringError(error.code, str(error), error.receipt) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _asset_file(value: object, plugin: str) -> str:
    """Accept canonical top-level plugin asset paths, never generated classes."""
    if not isinstance(value, str) or not value.startswith("/" + plugin + "/"):
        raise ContentAuthoringError("invalid_asset_path", "Asset paths must belong to the selected content plugin")
    package, separator, name = value.rpartition(".")
    if not separator or not name or name.endswith("_C") or name != package.rsplit("/", 1)[-1]:
        raise ContentAuthoringError("invalid_asset_path", "An asset requires its canonical package and matching object name")
    relative = package[len(plugin) + 2:]
    _parts(relative)
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in relative.split("/")):
        raise ContentAuthoringError("invalid_asset_path", "Asset package names must use Unreal identifiers")
    file_path = "Content/" + relative + ".uasset"
    if any(part.casefold() in _GENERATED_DIRECTORIES for part in _parts(file_path)):
        raise ContentAuthoringError("invalid_asset_path", "Asset outputs must remain in the content project's source inventory")
    return file_path


def _specification(raw: bytes, plugin: str) -> tuple[dict, list[str]]:
    try:
        spec = _json(raw)
        json.dumps(spec, allow_nan=False)
    except (ContentProjectError, TypeError, ValueError):
        raise ContentAuthoringError("invalid_authoring_spec", "The authoring specification must be finite UTF-8 JSON with unique keys") from None
    if spec.get("schema") != 1 or spec.get("plugin_id") != plugin:
        raise ContentAuthoringError("spec_identity_mismatch", "Schema 1 and the selected plugin_id are required")
    assets = spec.get("assets")
    if not isinstance(assets, list) or not assets:
        raise ContentAuthoringError("invalid_authoring_spec", "The specification must contain a nonempty asset list")
    files = []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("kind") not in {"blueprint", "data_asset"}:
            raise ContentAuthoringError("invalid_authoring_spec", "Each asset must declare blueprint or data_asset kind")
        files.append(_asset_file(asset.get("path"), plugin))
    if len({item.casefold() for item in files}) != len(files):
        raise ContentAuthoringError("duplicate_asset_path", "The specification contains duplicate asset paths")
    return spec, files


class ContentAuthoring:
    def __init__(self, repository_root: Path, *, workspace_root: Path,
                 modkit_root: Path | None = None, engine_root: Path | None = None,
                 game_root: Path | None = None, helper_root: Path | None = None,
                 runner: Runner = run_build_command, workspace: SourceWorkspace | None = None):
        self.repository = _checked(Path(repository_root).absolute(), directory=True)
        self.root = _checked(Path(workspace_root).absolute(), directory=True)
        self.workspace = workspace or SourceWorkspace(self.root)
        if self.workspace.root != self.root:
            raise ContentAuthoringError("workspace_mismatch", "Authoring and source services must share the selected workspace")
        self.modkit = Path(modkit_root or self.root / "Modkit").absolute()
        self.engine = Path(engine_root or self.root / "Engine").absolute()
        if not (self.engine / "Build/Build.version").is_file():
            self.engine = self.engine / "Engine"
        self.game = Path(game_root).absolute() if game_root is not None else None
        self.helper_source = self.repository / "development/authoring" / HELPER_NAME
        self.helper_cache = Path(helper_root or self.root / "artifacts/authoring-helper").absolute()
        if not self.helper_cache.is_relative_to(self.root):
            raise ContentAuthoringError("unsafe_helper_cache", "Helper builds must remain in the selected authoring workspace")
        self.runner = runner

    def _directory(self, path: Path) -> Path:
        if not path.is_relative_to(self.root):
            raise ContentAuthoringError("unsafe_output", "Authoring artifacts must remain inside the selected workspace")
        self.workspace._unprotected(path, forbid_ancestor=True)
        with _destination_lock(path):
            if not path.parent.exists():
                self._directory(path.parent)
            parent = _checked(path.parent, self.root, directory=True)
            with _guard_directories(parent, self.root):
                path.mkdir(exist_ok=True)
            return _checked(path, self.root, directory=True)

    def _project(self, project_path: str) -> dict:
        project = validate_content_project(self.root, project_path, workspace=self.workspace)
        # A declared entry asset may be generated by this operation.
        issues = [item for item in project["issues"] if item["code"] != "entry_asset_missing"]
        if issues:
            raise ContentAuthoringError("invalid_content_project", "The selected content project failed validation", project)
        return project

    def recipe(self, project_path: str, spec_path: str) -> dict:
        project = self._project(project_path)
        spec_parts = self.workspace._source_parts(spec_path)
        spec_file = self.workspace._unprotected(_checked(self.root.joinpath(*spec_parts), self.root))
        raw = _read_bytes(spec_file, self.root, None)
        spec_info = _file_info(spec_file, self.root, spec_path, "authoring_specification")
        if spec_info["sha256"] != hashlib.sha256(raw).hexdigest() or spec_info["bytes"] != len(raw):
            raise ContentAuthoringError("specification_changed", "The specification changed during recipe preparation")
        spec, asset_files = _specification(raw, project["plugin_id"])
        if project["plugin_id"] == HELPER_NAME:
            raise ContentAuthoringError("plugin_collision", "The content plugin name is reserved by the authoring helper")
        if project.get("entry_asset_path") and not project.get("entry_asset_present") and project["entry_asset_path"] not in asset_files:
            raise ContentAuthoringError("entry_asset_unresolved", "The missing declared entry asset must be generated by the specification")
        for asset, file_path in zip(spec["assets"], asset_files):
            if file_path == project.get("entry_asset_path") and asset["kind"] != "blueprint":
                raise ContentAuthoringError("entry_class_kind_mismatch", "The declared entry class must be authored as a Blueprint")
        files = {"editor": self.engine / "Binaries/Win64/UnrealEditor-Cmd.exe",
                 "engine_version": self.engine / "Build/Build.version",
                 "engine_modules": self.engine / "Binaries/Win64/UnrealEditor.modules",
                 "build_tool": self.engine / "Build/BatchFiles/Build.bat",
                 "dotnet": self.engine / "Binaries/ThirdParty/DotNet/8.0.300/win-x64/dotnet.exe",
                 "metadata_tool": self.engine / "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll",
                 "modkit_project": self.modkit / "Subnautica2.uproject"}
        missing = [key for key, path in files.items() if not path.is_file()]
        identities = {key: _file_info(path, path.parent, key, "tool_identity")
                      for key, path in files.items() if path.is_file()}
        helper = _inventory(self.helper_source, "helper", "helper_source") if self.helper_source.is_dir() else []
        if not helper:
            missing.append("authoring_helper_source")
        identity = {"helper_source_manifest_sha256": _manifest(helper),
                    "engine_identity": {key: item["sha256"] for key, item in identities.items() if key != "modkit_project"}}
        helper_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        return {"source": "content_authoring_recipe", "project_path": project_path, "spec_path": spec_path,
                "plugin_id": project["plugin_id"], "project": project,
                "source_manifest_sha256": project["project_source_manifest_sha256"],
                "specification": spec, "specification_sha256": hashlib.sha256(raw).hexdigest(),
                "specification_file": spec_info,
                "asset_files": asset_files, "tools": identities, "helper_source": helper,
                "helper_identity": identity, "helper_id": helper_id,
                "helper_cache": str(self.helper_cache / helper_id / HELPER_NAME),
                "helper_build_required": not self._cached_helper(helper_id, identity),
                "missing": missing, "ready_to_attempt_authoring": not missing,
                "modkit_git": _git(self.modkit) if self.modkit.is_dir() else None,
                "compiled": False, "installed_in_game": False}

    def _cached_helper(self, helper_id: str, identity: dict) -> dict | None:
        receipt_path = self.helper_cache / helper_id / "receipt.json"
        if not receipt_path.is_file():
            return None
        try:
            receipt = _json(_read_bytes(receipt_path, self.root, None))
            if receipt.get("state") != "succeeded" or receipt.get("identity") != identity:
                return None
            files = receipt.get("files")
            if not isinstance(files, list) or not files or not all(isinstance(item, dict) for item in files):
                return None
            root = receipt_path.parent / HELPER_NAME
            current = _inventory(root, "helper", "authoring_helper")
            if _manifest(current) != _manifest(files):
                return None
            paths = [item["path"] for item in files]
            if len({path.casefold() for path in paths}) != len(paths):
                return None
            required = {"helper/" + HELPER_NAME + ".uplugin",
                        "helper/Binaries/Win64/UnrealEditor-" + HELPER_NAME + ".dll",
                        "helper/Binaries/Win64/UnrealEditor.modules"}
            if not required.issubset(paths):
                return None
            for item in files:
                if not item["path"].startswith("helper/"):
                    return None
                expected = root.joinpath(*_parts(item["path"][len("helper/"):]))
                if _checked(Path(item["source_path"]), root) != _checked(expected, root):
                    return None
            source = _inventory(self.helper_source, "helper", "helper_source")
            cache_sources = [{**item, "role": "helper_source"} for item in current
                             if item["path"] in {original["path"] for original in source}]
            if _manifest(cache_sources) != identity["helper_source_manifest_sha256"]:
                return None
            modules = _json(_read_bytes(root / "Binaries/Win64/UnrealEditor.modules", root, None))
            if not isinstance(modules.get("Modules"), dict) or modules["Modules"].get(HELPER_NAME) != "UnrealEditor-" + HELPER_NAME + ".dll":
                return None
            return receipt
        except (OSError, ValueError, KeyError, TypeError, WorkspaceError, ContentProjectError, ContentAuthoringError):
            return None

    def _copy(self, source: Path, destination: Path, boundary: Path, expected: dict | None = None) -> dict:
        self._directory(destination.parent)
        with _checked(source, boundary).open("rb") as stream, destination.open("xb") as target:
            shutil.copyfileobj(stream, target, length=1024 * 1024)
        info = _file_info(destination, self.root, str(destination.relative_to(self.root)), "captured_file")
        if expected and (info["sha256"], info["bytes"]) != (expected["sha256"], expected["bytes"]):
            raise ContentAuthoringError("input_changed", "An authoring input changed during capture")
        return info

    def _capture(self, recipe: dict, directory: Path) -> tuple[Path, Path, list[dict]]:
        source = self._directory(directory / "source/Subnautica2")
        plugin = self._directory(source / "Plugins" / recipe["plugin_id"])
        dependencies = []
        for name in ("Binaries", "Config", "Content", "Plugins", "Releases", "Source"):
            selected = self.modkit / name
            if selected.is_dir():
                dependencies.extend(_inventory(selected, "modkit/" + name, "modkit_dependency"))
        for name in ("Subnautica2.uproject", "AssetRegistry.bin", "GameInstallDirectory.txt"):
            path = self.modkit / name
            if path.is_file():
                dependencies.append(_file_info(path, self.modkit, "modkit/" + name, "modkit_dependency"))
        for item in dependencies:
            destination = source / item["path"][len("modkit/"):]
            if destination.is_relative_to(plugin) or destination.is_relative_to(source / "Plugins" / HELPER_NAME):
                raise ContentAuthoringError("plugin_collision", "The selected authoring plugin collides with a Modkit plugin")
            self._copy(Path(item["source_path"]), destination, self.modkit, item)
            item["origin_path"], item["source_path"] = item["source_path"], str(destination)
        if self.game is not None:
            reference = self.game / "Subnautica2" if (self.game / "Subnautica2/Content/Paks").is_dir() else self.game
            if (reference / "Content/Paks").is_dir():
                (source / "GameInstallDirectory.txt").write_text(str(reference) + "\n", encoding="utf-8")
                for item in dependencies:
                    if item["path"] == "modkit/GameInstallDirectory.txt":
                        item.update(_file_info(source / "GameInstallDirectory.txt", source, item["path"], item["role"]))
        for item in recipe["project"]["files"]:
            if item["path"] in recipe["asset_files"]:
                continue  # Regenerate specified assets in the capture; originals remain untouched.
            self._copy(Path(recipe["project"]["project_root"]) / item["path"], plugin / item["path"],
                       Path(recipe["project"]["project_root"]), item)
        self._directory(plugin / "Content")
        spec = directory / "specification.json"
        self._copy(Path(recipe["specification_file"]["source_path"]), spec, self.root, recipe["specification_file"])
        return source, plugin, dependencies

    def _captured_dependencies(self, source: Path, plugin_id: str) -> list[dict]:
        entries = []
        for name in ("Binaries", "Config", "Content", "Plugins", "Releases", "Source"):
            selected = source / name
            if selected.is_dir():
                entries.extend(_inventory(selected, "modkit/" + name, "modkit_dependency",
                    exclude={plugin_id, HELPER_NAME} if name == "Plugins" else None))
        for name in ("Subnautica2.uproject", "AssetRegistry.bin", "GameInstallDirectory.txt"):
            path = source / name
            if path.is_file():
                entries.append(_file_info(path, source, "modkit/" + name, "modkit_dependency"))
        return sorted(entries, key=lambda item: item["path"])

    def _run(self, arguments: list[str], cwd: Path, directory: Path, stage: str,
             timeout: float, started: float, receipt: dict) -> dict:
        remaining = timeout - (time.monotonic() - started)
        if remaining <= 0:
            raise ContentAuthoringError("deadline_exhausted", "Authoring input preparation exhausted the caller deadline")
        receipt["commands"].append(arguments)
        receipt["state"] = stage
        receipt["cleanup"] = {"worker_started": True, "tree_exit_confirmed": False,
                              "termination_requested": False}
        receipt["resident_worker"] = True
        _write_json(Path(receipt["receipt_path"]), receipt)
        try:
            command = self.runner(arguments, cwd, remaining, directory / (stage + ".stdout.log"),
                                  directory / (stage + ".stderr.log"))
        except BuildError as error:
            if error.receipt:
                receipt["stages"][stage] = error.receipt
                receipt["cleanup"] = {"worker_started": True,
                                      "tree_exit_confirmed": error.receipt.get("tree_exit_confirmed", False),
                                      "termination_requested": error.receipt.get("termination_requested", False)}
            raise
        receipt["stages"][stage] = command
        receipt["cleanup"] = {"worker_started": True,
                              "tree_exit_confirmed": command.get("tree_exit_confirmed", False),
                              "termination_requested": command.get("termination_requested", False)}
        if not command.get("tree_exit_confirmed") or not command.get("output_complete"):
            raise ContentAuthoringError("cleanup_unconfirmed", "Authoring command output or process-tree exit is unconfirmed")
        if command.get("state") != "succeeded" or command.get("exit_code") != 0:
            if stage == "authoring" and (directory / "authoring-report.json").is_file():
                report = _json(_read_bytes(directory / "authoring-report.json", directory, None))
                receipt["authoring_report"] = report
            raise ContentAuthoringError("authoring_command_failed", "The authoring command failed; inspect its complete report")
        return command

    def _helper(self, recipe: dict, source: Path, directory: Path, timeout: float,
                started: float, receipt: dict) -> Path:
        plugin = self._directory(source / "Plugins" / HELPER_NAME)
        cached = self._cached_helper(recipe["helper_id"], recipe["helper_identity"])
        if cached:
            for item in cached["files"]:
                relative = item["path"][len("helper/"):]
                self._copy(Path(item["source_path"]), plugin / relative, self.root, item)
            receipt["helper_build"] = {"state": "reused", "receipt_path": str(self.helper_cache / recipe["helper_id"] / "receipt.json"),
                                       "identity": recipe["helper_identity"]}
            return plugin
        for item in recipe["helper_source"]:
            relative = item["path"][len("helper/"):]
            self._copy(Path(item["source_path"]), plugin / relative, self.repository, item)
        host = self._directory(directory / "helper-host")
        host_project = host / "HostProject.uproject"
        host_project.write_text(json.dumps({"FileVersion": 3, "EngineAssociation": "",
                                            "DisableEnginePluginsByDefault": True}) + "\n", encoding="utf-8")
        arguments = [recipe["tools"]["build_tool"]["source_path"], "UnrealEditor", "Win64", "Development",
                     "-Project=" + str(host_project),
                     "-Plugin=" + str(plugin / (HELPER_NAME + ".uplugin")), "-Module=" + HELPER_NAME,
                     "-NoHotReload", "-NoUBTMakefiles", "-NoEngineChanges", "-UsePrecompiled", "-NoUBA", "-NoXGE"]
        preferred_compilers = []
        studio_roots = [Path(os.environ.get(name, default)) / "Microsoft Visual Studio/2022"
                        for name, default in (("ProgramFiles", "C:/Program Files"),
                                              ("ProgramFiles(x86)", "C:/Program Files (x86)"))]
        for visual_studio in studio_roots:
            if visual_studio.is_dir():
                preferred_compilers.extend(sorted(visual_studio.glob("*/VC/Tools/MSVC/14.44.35207/bin/Hostx64/x64/cl.exe")))
        if preferred_compilers:
            compiler = _checked(preferred_compilers[0])
            receipt["helper_compiler"] = _file_info(compiler, compiler.parent, "msvc/cl.exe", "compiler_identity")
            arguments.extend(["-Compiler=VisualStudio2022", "-CompilerVersion=14.44.35207"])
        compiler_inputs = [Path(receipt["helper_compiler"]["source_path"])] if receipt.get("helper_compiler") else []
        with _guard_input_files([Path(item["source_path"]) for item in _inventory(plugin, "helper", "helper_source")]
                                + [Path(item["source_path"]) for item in recipe["tools"].values()] + compiler_inputs):
            self._run(arguments, host, directory, "helper_build", timeout, started, receipt)
        helper_sources = [item for item in _inventory(plugin, "helper", "helper_source")
                          if not item["path"].startswith("helper/Binaries/")]
        if _manifest(helper_sources) != recipe["helper_identity"]["helper_source_manifest_sha256"]:
            raise ContentAuthoringError("helper_source_changed", "The captured authoring helper source set changed during compilation")
        binaries = plugin / "Binaries/Win64"
        if not (binaries / "UnrealEditor.modules").is_file():
            metadata = host / "Intermediate/Build/Win64/x64/UnrealEditor/Development/TargetMetadata.dat"
            if not metadata.is_file():
                raise ContentAuthoringError("helper_metadata_missing", "UBT did not produce captured target metadata for the helper")
            arguments = [recipe["tools"]["dotnet"]["source_path"], recipe["tools"]["metadata_tool"]["source_path"], "-Mode=WriteMetadata",
                         "-Input=" + str(metadata), "-Version=2"]
            with _guard_input_files([metadata, Path(recipe["tools"]["engine_modules"]["source_path"]),
                                     Path(recipe["tools"]["metadata_tool"]["source_path"])]):
                self._run(arguments, host, directory, "helper_metadata", timeout, started, receipt)
        if not (binaries / ("UnrealEditor-" + HELPER_NAME + ".dll")).is_file() or not (binaries / "UnrealEditor.modules").is_file():
            raise ContentAuthoringError("helper_output_missing", "The helper build did not produce its Editor DLL and module metadata")
        files = _inventory(plugin, "helper", "authoring_helper")
        cache = self._directory(self.helper_cache / recipe["helper_id"])
        with _destination_lock(cache):
            existing = self._cached_helper(recipe["helper_id"], recipe["helper_identity"])
            if existing:
                receipt["helper_build"] = existing
                return plugin
            target = cache / HELPER_NAME
            if target.exists():
                target = self.workspace._unprotected(_checked(target, cache, directory=True), forbid_ancestor=True)
                recovery = self._directory(directory / "invalid-helper-cache")
                shutil.move(str(target), str(recovery / (HELPER_NAME + "-" + uuid.uuid4().hex)))
            target = self._directory(target)
            captured = []
            for item in files:
                destination = target / item["path"][len("helper/"):]
                self._copy(Path(item["source_path"]), destination, plugin, item)
                captured.append({**item, "source_path": str(destination)})
            built = {"state": "succeeded", "identity": recipe["helper_identity"], "files": captured,
                     "command": receipt["stages"]["helper_build"],
                     "metadata_command": receipt["stages"].get("helper_metadata")}
            _write_json(cache / "receipt.json", built)
            receipt["helper_build"] = {"state": "built", "receipt_path": str(cache / "receipt.json"),
                                       "identity": recipe["helper_identity"]}
        return plugin

    def _report(self, directory: Path, recipe: dict) -> tuple[dict, list[dict]]:
        report_path = directory / "authoring-report.json"
        raw_report = _read_bytes(report_path, directory, None)
        report = _json(raw_report)
        if report.get("schema") != 1 or report.get("kind") != "sn2_content_authoring_report":
            raise ContentAuthoringError("invalid_authoring_report", "The helper report does not identify its schema")
        if report.get("state") != "succeeded" or report.get("errors"):
            raise ContentAuthoringError("asset_compile_failed", "The helper reported authoring or compilation errors", report)
        expected = {asset["path"] for asset in recipe["specification"]["assets"]}
        entries = report.get("assets")
        if not isinstance(entries, list) or not all(isinstance(item, dict) and isinstance(item.get("path"), str) for item in entries):
            raise ContentAuthoringError("invalid_authoring_report", "Asset reports must be JSON objects")
        if {item.get("path") for item in entries} != expected or len(entries) != len(expected):
            raise ContentAuthoringError("incomplete_asset_report", "The helper did not report every selected asset exactly once")
        expected_assets = {asset["path"]: asset for asset in recipe["specification"]["assets"]}
        for asset in entries:
            kind = expected_assets[asset["path"]]["kind"]
            if asset.get("kind") != kind or asset.get("saved") is not True:
                raise ContentAuthoringError("asset_compile_failed", "A selected asset was not saved with its declared kind", report)
            if kind == "blueprint":
                compiler = asset.get("compiler")
                if not isinstance(compiler, dict) or type(compiler.get("errors")) is not int or compiler["errors"] != 0:
                    raise ContentAuthoringError("asset_compile_failed", "Blueprint compilation must explicitly report zero errors", report)
                if asset.get("generated_class") != asset["path"] + "_C":
                    raise ContentAuthoringError("output_identity_mismatch", "A Blueprint report requires its generated-class identity")
        plugin = Path(directory / "source/Subnautica2/Plugins" / recipe["plugin_id"])
        outputs = report.get("outputs")
        if not isinstance(outputs, list) or not all(isinstance(item, dict) and isinstance(item.get("asset"), str)
                                                   and isinstance(item.get("path"), str) for item in outputs):
            raise ContentAuthoringError("invalid_authoring_report", "Output reports must be JSON objects")
        if {item.get("asset") for item in outputs} != expected or len(outputs) != len(expected):
            raise ContentAuthoringError("incomplete_authoring_outputs", "The helper must identify every saved selected asset")
        inventory = []
        for item in outputs:
            relative = _asset_file(item["asset"], recipe["plugin_id"])
            path = _checked(Path(item["path"]), plugin)
            if path != _checked(plugin / relative, plugin):
                raise ContentAuthoringError("output_identity_mismatch", "A saved file does not match its declared asset path")
            inventory.append({**_file_info(path, plugin, relative, "authored_asset"), "asset": item["asset"]})
        evidence = _file_info(report_path, directory, "authoring-report.json", "authoring_report")
        if evidence["sha256"] != hashlib.sha256(raw_report).hexdigest() or evidence["bytes"] != len(raw_report):
            raise ContentAuthoringError("report_changed", "The structured report changed while validating its outputs")
        report["report_evidence"] = evidence
        return report, inventory

    def _publish(self, recipe: dict, outputs: list[dict], directory: Path, receipt: dict) -> None:
        project = self.workspace._selected(recipe["project_path"])
        backup = self._directory(directory / "publication-backup")
        published, originals = [], []
        with self.workspace._lock, _destination_lock(project), ExitStack() as destinations:
            destinations.enter_context(_guard_directories(project, self.root))
            destinations.enter_context(_destination_lock(Path(recipe["specification_file"]["source_path"])))
            current_spec = _read_bytes(Path(recipe["specification_file"]["source_path"]), self.root, None)
            if hashlib.sha256(current_spec).hexdigest() != recipe["specification_sha256"]:
                raise ContentAuthoringError("workspace_revision_changed", "The authoring specification changed before publication")
            for output in outputs:
                destinations.enter_context(_destination_lock(project / output["path"]))
            current = self._project(recipe["project_path"])
            if current["project_source_manifest_sha256"] != recipe["source_manifest_sha256"]:
                raise ContentAuthoringError("workspace_revision_changed", "The project changed during authoring; captured outputs were retained without publication")
            original_map = {item["path"]: item for item in current["files"]}
            try:
                for item in outputs:
                    relative = item["path"]
                    target = self.workspace._unprotected(project.joinpath(*_parts(relative)))
                    parent = project
                    for part in _parts(relative)[:-1]:
                        child = self.workspace._unprotected(parent / part)
                        with _guard_directories(parent, self.root):
                            if not child.exists():
                                child.mkdir()
                        parent = _checked(child, self.root, directory=True)
                    old = original_map.get(relative)
                    if old:
                        original = backup / relative
                        self._copy(target, original, project, old)
                        originals.append((target, original))
                    else:
                        originals.append((target, None))
                    pending = target.with_name(target.name + "." + receipt["authoring_id"] + ".pending")
                    try:
                        self._copy(Path(item["source_path"]), pending, directory, item)
                        if old and _file_info(target, project, relative, "authored_asset")["sha256"] != old["sha256"]:
                            raise ContentAuthoringError("workspace_revision_changed", "A destination asset changed before publication")
                        if not old and target.exists():
                            raise ContentAuthoringError("workspace_revision_changed", "An unrecorded destination asset appeared before publication")
                        os.replace(pending, target)
                    except BaseException:
                        if pending.exists():
                            try:
                                recovery = self._directory(directory / "publication-recovery" / Path(relative).parent)
                                shutil.move(str(pending), str(recovery / pending.name))
                            except (OSError, WorkspaceError, ContentAuthoringError):
                                receipt["pending_output_recovery_confirmed"] = False
                                receipt["pending_output_path"] = str(pending)
                        raise
                    published.append(target)
                receipt["published_outputs"] = [_file_info(project / item["path"], project, item["path"], "authored_asset") for item in outputs]
                receipt["published"] = True
                receipt["result_source_manifest_sha256"] = self._project(recipe["project_path"])["project_source_manifest_sha256"]
            except BaseException:
                restored = True
                for target, original in reversed(originals):
                    try:
                        if original is None:
                            if target in published:
                                # Preserve the failed operation's new output outside source before rollback.
                                relative = target.relative_to(project)
                                recovery = self._directory(directory / "publication-recovery" / relative.parent) / relative.name
                                shutil.move(str(target), str(recovery))
                        elif target in published:
                            shutil.copyfile(original, target)
                    except (OSError, WorkspaceError, ContentAuthoringError):
                        restored = False
                if restored:
                    try:
                        restored = self._project(recipe["project_path"])["project_source_manifest_sha256"] == recipe["source_manifest_sha256"]
                    except (WorkspaceError, ContentAuthoringError, OSError):
                        restored = False
                receipt["publication_rollback_confirmed"] = restored
                receipt["published"] = False
                raise

    def author(self, project_path: str, spec_path: str, timeout_seconds: float = 3600) -> dict:
        timeout = _positive(timeout_seconds, "timeout_seconds")
        recipe = self.recipe(project_path, spec_path)
        started, authoring_id = time.monotonic(), uuid.uuid4().hex
        directory = self._directory(self.root / "artifacts/content-authoring" / authoring_id)
        receipt = {"kind": RECEIPT_KIND, "schema": 1, "source": "content_authoring",
                   "authoring_id": authoring_id, "receipt_path": str(directory / "receipt.json"),
                   "project_path": project_path, "spec_path": spec_path, "plugin_id": recipe["plugin_id"],
                   "source_manifest_sha256": recipe["source_manifest_sha256"],
                   "specification_sha256": recipe["specification_sha256"], "recipe": recipe,
                   "started_at": _now(), "timeout_seconds": timeout, "state": "preparing",
                   "commands": [], "stages": {}, "outputs": [], "reports": [],
                   "published": False, "compiled": False, "captured_assets_compiled": False, "installed_in_game": False,
                   "cleanup": {"worker_started": False, "tree_exit_confirmed": True}}
        path = Path(receipt["receipt_path"])
        _write_json(path, receipt)
        _write_json(directory.parent / "latest.json", {"authoring_id": authoring_id})
        try:
            if recipe["missing"]:
                receipt.update(state="blocked", blocked_reasons=recipe["missing"])
            else:
                source, plugin, dependencies = self._capture(recipe, directory)
                receipt["captured_modkit_root"] = str(source)
                receipt["input_projection"] = {"omitted_regenerated_assets": recipe["asset_files"],
                    "original_source_manifest_sha256": recipe["source_manifest_sha256"],
                    "specification_sha256": recipe["specification_sha256"],
                    "composition": "Original plugin files plus specification; selected generated assets omitted only in capture"}
                receipt["dependencies"] = dependencies
                helper = self._helper(recipe, source, directory, timeout, started, receipt)
                # Build may update captured target/module metadata; record this local composition.
                receipt["origin_dependency_manifest_sha256"] = _manifest(dependencies)
                after_build = self._captured_dependencies(source, recipe["plugin_id"])
                before = {item["path"]: (item["sha256"], item["bytes"]) for item in dependencies}
                receipt["helper_build_dependency_adaptations"] = [item for item in after_build
                    if before.get(item["path"]) != (item["sha256"], item["bytes"])]
                dependencies = after_build
                receipt["dependencies"] = dependencies
                receipt["dependency_manifest_sha256"] = _manifest(dependencies)
                cache = self._directory(self.root / "Cache/DDC")
                arguments = [recipe["tools"]["editor"]["source_path"], str(source / "Subnautica2.uproject"),
                             "-run=SN2MCPAuthoring", "-EnablePlugins=" + HELPER_NAME,
                             "-Spec=" + str(directory / "specification.json"),
                             "-Report=" + str(directory / "authoring-report.json"), "-PluginRoot=" + str(plugin),
                             "-unattended", "-nop4", "-nosound", "-NullRHI", "-stdout", "-FullStdOutLogOutput",
                             "-ddc=NoZenLocalFallback", "-LocalDataCachePath=" + str(cache),
                             "-abslog=" + str(directory / "editor.log")]
                with _destination_lock(self.root / "artifacts/content-authoring"), ExitStack() as guards:
                    guards.enter_context(_guard_directories(source, self.root))
                    # Original inputs are captured; output assets must remain writable.
                    locked = [directory / "specification.json"]
                    locked.extend(Path(item["source_path"]) for item in dependencies)
                    locked.extend(Path(item["source_path"]) for item in recipe["tools"].values())
                    captured_project_files = _inventory(plugin, "project", "authoring_input")
                    locked.extend(Path(item["source_path"]) for item in captured_project_files)
                    receipt["captured_input_manifest_sha256"] = _manifest(captured_project_files)
                    locked.extend(Path(item["source_path"]) for item in _inventory(helper, "helper", "authoring_helper"))
                    guards.enter_context(_guard_input_files(locked))
                    self._run(arguments, source, directory, "authoring", timeout, started, receipt)
                # Editor generation must not alter captured files outside its declared new assets.
                after = [item for item in _inventory(plugin, "project", "authoring_input")
                         if item["path"][len("project/"):] not in recipe["asset_files"]]
                if _manifest(after) != receipt["captured_input_manifest_sha256"]:
                    raise ContentAuthoringError("captured_inputs_changed", "Captured plugin inputs changed during authoring")
                after_dependencies = self._captured_dependencies(source, recipe["plugin_id"])
                if _manifest(after_dependencies) != receipt["dependency_manifest_sha256"]:
                    raise ContentAuthoringError("captured_dependency_changed", "The captured Modkit dependency set changed during authoring")
                for item in recipe["tools"].values():
                    if _file_info(Path(item["source_path"]), Path(item["source_path"]).parent,
                                  item["path"], item["role"])["sha256"] != item["sha256"]:
                        raise ContentAuthoringError("tool_changed", "An authoring tool changed during the selected operation")
                report, outputs = self._report(directory, recipe)
                receipt["authoring_report"] = report
                receipt["outputs"] = outputs
                receipt["captured_assets_compiled"] = True
                if hashlib.sha256(_read_bytes(directory / "specification.json", directory, None)).hexdigest() != recipe["specification_sha256"]:
                    raise ContentAuthoringError("captured_specification_changed", "The captured specification changed during authoring")
                current_raw = _read_bytes(Path(recipe["specification_file"]["source_path"]), self.root, None)
                if hashlib.sha256(current_raw).hexdigest() != recipe["specification_sha256"]:
                    raise ContentAuthoringError("workspace_revision_changed", "The specification changed during authoring; outputs were not published")
                with _guard_input_files([directory / "authoring-report.json"]):
                    if hashlib.sha256(_read_bytes(directory / "authoring-report.json", directory, None)).hexdigest() != report["report_evidence"]["sha256"]:
                        raise ContentAuthoringError("report_changed", "The structured report changed before publication")
                    self._publish(recipe, outputs, directory, receipt)
                receipt.update(state="succeeded", compiled=True)
        except (ContentAuthoringError, ContentProjectError, BuildError, WorkspaceError, OSError, ValueError, KeyError) as error:
            receipt.update(state="failed", failure_code=getattr(error, "code", "authoring_evidence_error"),
                           error={"code": getattr(error, "code", "authoring_evidence_error"),
                                  "message": str(error) if isinstance(error, (ContentAuthoringError, ContentProjectError, BuildError, WorkspaceError))
                                  else "Could not establish complete authoring evidence"})
            if isinstance(error, ContentAuthoringError) and error.receipt:
                receipt["authoring_report"] = error.receipt
        except BaseException:
            receipt.update(state="failed", failure_code="interrupted")
            self._finish(receipt, directory, started)
            raise
        return self._finish(receipt, directory, started)

    def _finish(self, receipt: dict, directory: Path, started: float) -> dict:
        receipt["reports"] = []
        for stage, command in receipt["stages"].items():
            for stream in ("stdout", "stderr"):
                evidence = command.get(stream)
                if evidence:
                    receipt["reports"].append({"stage": stage, "stream": stream, **evidence})
        report = directory / "authoring-report.json"
        try:
            if report.is_file():
                evidence = _file_info(report, directory, "authoring-report.json", "authoring_report")
                original = receipt.get("authoring_report", {}).get("report_evidence")
                if original and (evidence["sha256"], evidence["bytes"]) != (original["sha256"], original["bytes"]):
                    raise ContentAuthoringError("report_changed", "The structured report changed after output validation")
                receipt["structured_report"] = evidence
        except (OSError, WorkspaceError, ContentAuthoringError) as error:
            receipt["finalization_error"] = {"code": getattr(error, "code", "report_identity_error"),
                "message": str(error) if isinstance(error, (WorkspaceError, ContentAuthoringError))
                else "Could not verify the final authoring report"}
            if receipt["state"] == "succeeded":
                # Known publication remains known even if final evidence validation fails.
                receipt.update(state="failed", failure_code=receipt["finalization_error"]["code"])
        receipt["elapsed_seconds"], receipt["finished_at"] = time.monotonic() - started, _now()
        receipt["resident_worker"] = not receipt["cleanup"]["tree_exit_confirmed"]
        _write_json(directory / "receipt.json", receipt)
        return receipt

    def status(self, authoring_id: str | None = None) -> dict:
        root = self.root / "artifacts/content-authoring"
        if authoring_id is None:
            latest = root / "latest.json"
            if not latest.is_file():
                return {"source": "content_authoring_status", "state": "not_authored", "resident_worker": False}
            authoring_id = _json(_read_bytes(latest, self.root, None))["authoring_id"]
        if not isinstance(authoring_id, str) or not re.fullmatch(r"[0-9a-f]{32}", authoring_id):
            raise ContentAuthoringError("invalid_authoring_id", "Select a recorded authoring identifier")
        receipt = _json(_read_bytes(root / authoring_id / "receipt.json", self.root, None))
        if receipt.get("kind") != RECEIPT_KIND or receipt.get("authoring_id") != authoring_id:
            raise ContentAuthoringError("receipt_identity_mismatch", "The authoring receipt has a different identity")
        return receipt

    def read_report(self, authoring_id: str, stage: str = "authoring", stream: str = "stdout") -> dict:
        receipt = self.status(authoring_id)
        if stream == "structured":
            evidence = receipt.get("structured_report")
            if evidence is None:
                raise ContentAuthoringError("report_missing", "No structured asset report was produced")
            report_path = Path(evidence["source_path"])
        else:
            if stream not in {"stdout", "stderr"}:
                raise ContentAuthoringError("invalid_report_stream", "Select stdout, stderr or structured")
            evidence = next((item for item in receipt["reports"] if item["stage"] == stage and item["stream"] == stream), None)
            if evidence is None:
                raise ContentAuthoringError("report_missing", "No matching authoring command report exists")
            report_path = Path(evidence["path"])
        raw = _read_bytes(report_path, Path(receipt["receipt_path"]).parent, None)
        if hashlib.sha256(raw).hexdigest() != evidence["sha256"] or len(raw) != evidence["bytes"]:
            raise ContentAuthoringError("report_changed", "The report differs from its recorded bytes")
        return {"source": "content_authoring_report", "authoring_id": authoring_id, "stage": stage,
                "stream": stream, "text": raw.decode("utf-8", errors="replace"), "sha256": evidence["sha256"],
                "bytes": len(raw), "content_truncated": False}
