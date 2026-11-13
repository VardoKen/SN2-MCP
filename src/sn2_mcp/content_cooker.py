"""Cook selected content plugins without installing them into the target game.

The fixed DLC parameters follow the recorded Subnautica 2 Modkit's Alpakit
PackagePlugin.GetParams. Alpakit's installation and deletion steps are not run.
Recipes are source-derived; only actual cooker receipts establish a local cook.
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
import stat
import subprocess
import time
from typing import Callable
import uuid

from .build_workflow import BuildError, _guard_input_files, _positive, run_build_command
from .workspace import SourceWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories, _parts, _read_bytes


class ContentCookError(Exception):
    def __init__(self, code: str, message: str, receipt: dict | None = None):
        super().__init__(message)
        self.code, self.receipt = code, receipt


CookRunner = Callable[[list[str], Path, float, Path, Path], dict]
_GENERATED = {".git", ".venv", "__pycache__", "nppBackup", "Saved", "Intermediate",
              "DerivedDataCache", "artifacts", "build", ".trash"}
_MODKIT_DIRECTORIES = ("Binaries", "Config", "Content", "Plugins", "Releases", "Source")
_ENGINE_RUNTIME_DIRECTORIES = ("Binaries", "Build", "Config", "Content", "Plugins", "Shaders", "Platforms")
_COOK_UNUSED_DIRECTORIES = {"Source", "Documentation", "Extras", "Intermediate"}
_COOK_UNUSED_SUFFIXES = {".pdb", ".lib", ".obj", ".exp"}
_FMOD_FILES = ("fmod.dll", "fmodL.dll", "fmodstudio.dll", "fmodstudioL.dll",
               "resonanceaudio.dll", "fmod_vc.lib", "fmodL_vc.lib",
               "fmodstudio_vc.lib", "fmodstudioL_vc.lib")
_MANIFEST_FIELDS = ("path", "sha256", "bytes", "role")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _manifest(items: list[dict]) -> str:
    entries = [{key: item[key] for key in _MANIFEST_FIELDS} for item in items]
    return _sha(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _file_info(path: Path, boundary: Path, relative: str, role: str) -> dict:
    """Hash complete files in bounded memory and detect identity/content races."""
    path = _checked(path, boundary)
    if not path.is_file():
        raise ContentCookError("invalid_input", "Cook inputs must be regular files")
    before = path.stat()
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ContentCookError("input_changed", "A cook input changed while opening")
        while raw := stream.read(1024 * 1024):
            digest.update(raw)
            size += len(raw)
        read = os.fstat(stream.fileno())
    after = _checked(path, boundary).stat()
    stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if stamp(before) != stamp(read) or stamp(before) != stamp(after) or size != before.st_size:
        raise ContentCookError("input_changed", "A cook input changed while hashing")
    return {"path": relative, "source_path": str(path), "sha256": digest.hexdigest(),
            "bytes": size, "role": role}


def _inventory(root: Path, prefix: str, role: str, *, exclude: set[str] | None = None,
               omit_suffixes: set[str] | None = None) -> list[dict]:
    root = _checked(root, directory=True)
    omitted = _GENERATED | (exclude or set())
    entries = []
    with _guard_directories(root, root):
        def failed(error):
            raise ContentCookError("input_access_error", "Could not enumerate every declared cook dependency") from error
        for directory, children, names in os.walk(root, followlinks=False, onerror=failed):
            parent = _checked(Path(directory), root, directory=True)
            children[:] = sorted(name for name in children if name not in omitted)
            with _guard_directories(parent, root):
                for child in children:
                    _checked(parent / child, root, directory=True)
                for name in sorted(names):
                    path = parent / name
                    if omit_suffixes and path.suffix.lower() in omit_suffixes:
                        continue
                    relative = prefix + "/" + path.relative_to(root).as_posix()
                    entries.append(_guarded_file_info(path, relative, role) if os.name == "nt"
                                   else _file_info(path, root, relative, role))
    return sorted(entries, key=lambda item: item["path"])


def _guarded_file_info(path: Path, relative: str, role: str) -> dict:
    """Hash a direct child while its validated parent/ancestors cannot be renamed.

    Windows directory guards make repeated ancestor traversal per file unnecessary.
    The file itself still rejects reparse points and verifies lstat/open/read identity.
    """
    def checked_file_stat():
        information = path.lstat()
        if stat.S_ISLNK(information.st_mode) or getattr(information, "st_file_attributes", 0) & 0x400:
            raise WorkspaceError("unsafe_path", "The cook dependency is a link or reparse point")
        if not stat.S_ISREG(information.st_mode):
            raise ContentCookError("invalid_input", "Cook inputs must be regular files")
        return information

    before = checked_file_stat()
    digest, size = hashlib.sha256(), 0
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ContentCookError("input_changed", "A cook input changed while opening")
        while raw := stream.read(1024 * 1024):
            digest.update(raw)
            size += len(raw)
        read = os.fstat(stream.fileno())
    after = checked_file_stat()
    stamp = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)
    if stamp(before) != stamp(read) or stamp(before) != stamp(after) or size != before.st_size:
        raise ContentCookError("input_changed", "A cook input changed while hashing")
    return {"path": relative, "source_path": str(path), "sha256": digest.hexdigest(),
            "bytes": size, "role": role}


def _write_json(path: Path, value: dict) -> None:
    pending = path.with_suffix(".json.pending")
    pending.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(pending, path)


def _validate_project(root: Path, project_path: str, *, workspace: SourceWorkspace | None = None) -> dict:
    from .content_projects import validate_content_project
    return validate_content_project(root, project_path, workspace=workspace)


def _git(root: Path) -> dict:
    result = {"head": None, "status_porcelain": None}
    for key, arguments in (("head", ["rev-parse", "HEAD"]),
                           ("status_porcelain", ["status", "--porcelain=v1"])):
        try:
            process = subprocess.run(["git", "-C", str(root), *arguments], shell=False,
                                     capture_output=True, timeout=30,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if process.returncode == 0:
                result[key] = process.stdout.decode("utf-8", errors="replace").rstrip("\r\n")
        except (OSError, subprocess.TimeoutExpired):
            pass
    return result


class ContentCooker:
    """A local, per-build UAT worker with captured plugin and dependency evidence."""

    def __init__(self, repository_root: Path, *, workspace_root: Path,
                 modkit_root: Path | None = None, engine_root: Path | None = None,
                 game_root: Path | None = None, runner: CookRunner = run_build_command,
                 diagnostics_parser=None, workspace: SourceWorkspace | None = None):
        self.repository_root = _checked(Path(repository_root).absolute(), directory=True)
        self.workspace_root = _checked(Path(workspace_root).absolute(), directory=True)
        self.modkit_root = Path(modkit_root or self.workspace_root / "Modkit").absolute()
        self.engine_root = Path(engine_root or self.workspace_root / "Engine").absolute()
        self.game_root = Path(game_root).absolute() if game_root is not None else None
        self.runner, self.diagnostics_parser = runner, diagnostics_parser
        self.workspace = workspace
        if workspace is not None and workspace.root != self.workspace_root:
            raise ContentCookError("workspace_mismatch", "The content cooker and source workspace must share the same root")

    def _project_validation(self, project_path: str) -> dict:
        if self.workspace is None:
            return _validate_project(self.workspace_root, project_path)
        return _validate_project(self.workspace_root, project_path, workspace=self.workspace)

    def _engine_directory(self) -> Path:
        return self.engine_root if (self.engine_root / "Build" / "Build.version").is_file() else self.engine_root / "Engine"

    def _game_reference_root(self) -> Path | None:
        if self.game_root is None or not self.game_root.is_dir():
            return None
        root = _checked(self.game_root, directory=True)
        matches = [path for path in (root, root / "Subnautica2") if (path / "Content" / "Paks").is_dir()]
        if len(matches) > 1:
            raise ContentCookError("ambiguous_game_reference", "More than one project-content root exists under the selected game directory")
        return _checked(matches[0], root, directory=True) if matches else None

    def inspect(self, project_path: str | None = None) -> dict:
        engine = self._engine_directory()
        files = {
            "engine_version": engine / "Build" / "Build.version",
            "editor_commandlet": engine / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe",
            "run_uat": engine / "Build" / "BatchFiles" / "RunUAT.bat",
            "dotnet": engine / "Binaries" / "ThirdParty" / "DotNet" / "8.0.300" / "win-x64" / "dotnet.exe",
            "automation_tool": engine / "Binaries" / "DotNET" / "AutomationTool" / "AutomationTool.dll",
            "modkit_project": self.modkit_root / "Subnautica2.uproject",
            "base_asset_registry": self.modkit_root / "Releases" / "SN2Base" / "Windows" / "AssetRegistry.bin",
            "editor_receipt": self.modkit_root / "Binaries" / "Win64" / "Subnautica2Editor.target",
        }
        inventory, missing, warnings = {}, [], []
        for name, path in files.items():
            available = path.is_file()
            inventory[name] = {"path": str(path), "available": available}
            if available:
                identity = _file_info(path, path.parent, name, "dependency_metadata")
                inventory[name].update(sha256=identity["sha256"], bytes=identity["bytes"],
                                       source_path=identity["source_path"], role=identity["role"],
                                       identity_path=identity["path"])
            else:
                missing.append(name)
        version = None
        if inventory["engine_version"]["available"]:
            version = json.loads(_read_bytes(files["engine_version"], engine, None))
            actual = tuple(version.get(key) for key in ("MajorVersion", "MinorVersion", "PatchVersion"))
            if actual != (5, 6, 1):
                warnings.append("Engine version label differs from the recorded Modkit UE 5.6.1 baseline; actual cooker compatibility remains to be checked")
        expected_version = None
        if inventory["editor_receipt"]["available"]:
            expected_version = json.loads(_read_bytes(files["editor_receipt"], self.modkit_root, None)).get("Version")
        fmod = []
        for name in _FMOD_FILES:
            path = self.modkit_root / "Plugins" / "FMODStudio" / "Binaries" / "Win64" / name
            role = ("conditional_native_rebuild_import_library" if name.endswith(".lib") else
                    "declared_editor_logging_runtime_dependency" if name in {"fmodL.dll", "fmodstudioL.dll"} else
                    "bank_specific_dependency_not_verified" if name == "resonanceaudio.dll" else
                    "release_runtime_dependency")
            item = {"path": str(path), "name": name, "available": path.is_file(),
                    "requirement_role": role, "cook_requirement": "conditional_not_locally_verified"}
            if item["available"]:
                item.update(_file_info(path, self.modkit_root, "fmod/" + name, "fmod_dependency"))
            else:
                warnings.append("FMOD file is absent: " + name + "; " + role +
                    "; the actual prebuilt Editor/cooker path must establish whether it is required")
            fmod.append(item)
        mappings = []
        automation = self.modkit_root / "Automation"
        if automation.is_dir():
            for path in sorted(automation.glob("*.usmap")):
                mappings.append(_file_info(path, self.modkit_root, path.relative_to(self.modkit_root).as_posix(), "mapping_evidence"))
        if not mappings:
            warnings.append("No Modkit mapping evidence was found; mappings are research evidence rather than cooker executables")
        game_reference = self._game_reference_root()
        if self.game_root is None or not self.game_root.is_dir():
            missing.append("game_reference_directory")
        elif game_reference is None:
            missing.append("game_reference_paks")
        project = self._project_validation(project_path) if project_path is not None else None
        if project is not None and not project.get("valid"):
            missing.append("valid_content_project")
        if project is not None and not project.get("authored_assets_present"):
            missing.append("authored_content_assets")
        return {"source": "content_cook_environment", "modkit_root": str(self.modkit_root),
                "engine_root": str(self.engine_root), "engine_directory": str(engine),
                "game_root": str(self.game_root) if self.game_root is not None else None,
                "game_reference_root": str(game_reference) if game_reference is not None else None,
                "files": inventory, "engine_version": version, "expected_editor_version": expected_version,
                "modkit_git": _git(self.modkit_root) if self.modkit_root.is_dir() else None,
                "mapping_evidence": mappings, "fmod": fmod, "project": project,
                "missing": missing, "warnings": warnings, "ready_to_attempt_cook": not missing,
                "cooker_compatibility": "not_runtime_verified", "compiled": False,
                "installed_in_game": False, "runtime_validation": "not_performed"}

    def recipe(self, project_path: str) -> dict:
        _parts(project_path)
        environment = self.inspect(project_path)
        project = environment["project"]
        if not project.get("valid"):
            raise ContentCookError("invalid_content_project", "The selected content project failed validation")
        plugin_id = project["plugin_id"]
        if not isinstance(plugin_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", plugin_id):
            raise ContentCookError("invalid_plugin_id", "The plugin identifier must be an Unreal content plugin name")
        inputs = [{**item, "origin_path": str(Path(project["project_root"]) / item["path"])}
                  for item in project["files"]]
        source_hash = _manifest(inputs)
        if source_hash != project["project_source_manifest_sha256"]:
            raise ContentCookError("input_manifest_mismatch", "The project validation manifest does not match its files")
        cache_path = self.workspace_root / "Cache" / "DDC"
        return {"source": "content_cook_recipe", "project_path": project_path,
                "plugin_id": plugin_id, "environment": environment, "inputs": inputs,
                "source_manifest_sha256": source_hash, "base_release": "SN2Base",
                "target_platform": "Win64", "configuration": "Shipping",
                "cook_options": ["-CookAgainstFixedBase", "-AllowUncookedAssetReferences",
                                 "-ddc=NoZenLocalFallback", '-LocalDataCachePath="' + str(cache_path) + '"',
                                 "-ini:Engine:[/Script/EngineSettings.GameMapsSettings]:GameDefaultMap=/Engine/Maps/Entry"],
                "ddc": {"graph": "NoZenLocalFallback", "path": str(cache_path),
                        "scope": "cook_process", "configuration_changed": False,
                        "source": "Engine/Config/BaseEngine.ini:NoZenLocalFallback"},
                "command_contract_source": "Plugins/Alpakit/Source/Alpakit.Automation/PackagePlugin.cs:GetParams",
                "launcher_contract_source": "Engine/Build/BatchFiles/RunUAT.bat:DoRunUAT; GetDotnetPath.bat",
                "launcher": "bundled_dotnet_precompiled_automation_tool",
                "dependency_contract": {
                    "engine": "Declared precompiled cooker runtime, configuration, assets, shaders and staging metadata",
                    "game": "Selected installed IoStore table-of-contents and data containers",
                    "excluded": "Engine/plugin native source, rebuild libraries, SDK material, documentation and debug symbols are not precompiled cook inputs; project target source is retained",
                    "sources": ["Engine/Source/Runtime/PakFile/Private/IPlatformFilePak.cpp:MountModKitGameContainers",
                                "Engine/Build/BatchFiles/RunUAT.bat:DoRunUAT",
                                "Engine/Source/Programs/AutomationTool/Scripts/BuildProjectCommand.Automation.cs:Build"]},
                "staging_remap": {
                    "from": "Subnautica2/Plugins/" + plugin_id,
                    "to": "Subnautica2/Mods/" + plugin_id,
                    "configuration": "Captured Config/DefaultGame.ini [Staging].RemapDirectories",
                    "source": "Engine/Source/Programs/AutomationTool/AutomationUtils/DeploymentContext.cs:573; "
                              "Plugins/Alpakit/Source/Alpakit.Automation/PackagePlugin.cs:RemapCookedPluginsContentPaths",
                },
                "command_contract_validation": "source_inspected; local UAT execution required",
                "modkit_git": environment["modkit_git"], "compiled": False,
                "installed_in_game": False, "runtime_validation": "not_performed"}

    def _dependencies(self) -> list[dict]:
        dependencies = []
        for name in _MODKIT_DIRECTORIES:
            path = self.modkit_root / name
            if path.is_dir():
                dependencies.extend(_inventory(path, "modkit/" + name, "modkit_dependency",
                                               exclude=_COOK_UNUSED_DIRECTORIES,
                                               omit_suffixes=_COOK_UNUSED_SUFFIXES))
        for name in ("Subnautica2.uproject", "AssetRegistry.bin", "GameInstallDirectory.txt"):
            path = self.modkit_root / name
            if path.is_file():
                dependencies.append(_file_info(path, self.modkit_root, "modkit/" + name, "modkit_dependency"))
        dependencies.extend(self._live_dependencies())
        return sorted(dependencies, key=lambda item: item["path"])

    def _live_dependencies(self) -> list[dict]:
        dependencies = []
        engine = self._engine_directory()
        for name in _ENGINE_RUNTIME_DIRECTORIES:
            path = engine / name
            if path.is_dir():
                dependencies.extend(_inventory(path, "engine/" + name, "engine_dependency",
                                               exclude=_COOK_UNUSED_DIRECTORIES,
                                               omit_suffixes=_COOK_UNUSED_SUFFIXES))
        game_reference = self._game_reference_root()
        containers = game_reference / "Content" / "Paks"
        if containers.is_dir():
            for path in sorted(containers.iterdir()):
                if path.suffix.lower() in {".utoc", ".ucas"}:
                    dependencies.append(_file_info(path, game_reference,
                                                  "game/Content/Paks/" + path.name, "game_reference"))
        return sorted(dependencies, key=lambda item: item["path"])

    def _captured_dependencies(self, source: Path, plugin_id: str) -> list[dict]:
        dependencies = []
        for name in _MODKIT_DIRECTORIES:
            selected = source / name
            if selected.is_dir():
                dependencies.extend(_inventory(selected, "modkit/" + name, "modkit_dependency",
                                               exclude=_COOK_UNUSED_DIRECTORIES | ({plugin_id} if name == "Plugins" else set()),
                                               omit_suffixes=_COOK_UNUSED_SUFFIXES))
        for name in ("Subnautica2.uproject", "AssetRegistry.bin", "GameInstallDirectory.txt"):
            if (source / name).is_file():
                dependencies.append(_file_info(source / name, source, "modkit/" + name, "modkit_dependency"))
        dependencies.extend(self._live_dependencies())
        return sorted(dependencies, key=lambda item: item["path"])

    def _directory(self, path: Path) -> Path:
        if not path.is_relative_to(self.workspace_root):
            raise ContentCookError("unsafe_output", "Cook output must remain in the selected authoring workspace")
        with _destination_lock(path):
            parent = path.parent
            if not parent.exists():
                self._directory(parent)
            _checked(parent, self.workspace_root, directory=True)
            with _guard_directories(parent, self.workspace_root):
                path.mkdir(exist_ok=True)
                return _checked(path, self.workspace_root, directory=True)

    def _capture(self, recipe: dict, directory: Path, dependencies: list[dict]) -> tuple[Path, Path, list[dict]]:
        source = self._directory(directory / "source" / "Subnautica2")
        plugin = self._directory(source / "Plugins" / recipe["plugin_id"])
        captured = []
        for item in dependencies:
            if not item["path"].startswith("modkit/"):
                continue
            relative = item["path"][len("modkit/"):]
            destination = source / relative
            self._directory(destination.parent)
            shutil.copyfile(_checked(Path(item["source_path"]), self.modkit_root), destination)
            if _file_info(destination, source, relative, item["role"])["sha256"] != item["sha256"]:
                raise ContentCookError("dependency_changed", "A Modkit dependency changed during capture")
            captured.append({**item, "origin_path": item["source_path"], "source_path": str(destination)})
        # A source checkout's generic example path is replaced only in this fresh capture.
        (source / "GameInstallDirectory.txt").write_text(str(self._game_reference_root()) + "\n", encoding="utf-8")
        # The target discovers this plugin under Mods; stored pak/UFS paths must agree.
        # This is the standard UAT configuration equivalent of Alpakit's staging remap.
        staging_config = source / "Config" / "DefaultGame.ini"
        self._directory(staging_config.parent)
        previous_config = staging_config.read_bytes() if staging_config.exists() else b""
        remap = recipe["staging_remap"]
        staging_config.write_bytes(previous_config + (
            '\n[Staging]\n+RemapDirectories=(From="' + remap["from"] + '",To="' + remap["to"] + '")\n'
        ).encode("utf-8"))
        for item in recipe["inputs"]:
            destination = plugin.joinpath(*_parts(item["path"]))
            if destination.exists():
                raise ContentCookError("plugin_collision", "The selected plugin collides with an existing Modkit plugin")
            self._directory(destination.parent)
            shutil.copyfile(_checked(Path(item["origin_path"]), Path(recipe["environment"]["project"]["project_root"])), destination)
            current = _file_info(destination, plugin, item["path"], item["role"])
            if (current["sha256"], current["bytes"]) != (item["sha256"], item["bytes"]):
                raise ContentCookError("input_changed", "A plugin input changed during capture")
            item["source_path"] = str(destination)
        for item in captured:
            if item["path"] == "modkit/GameInstallDirectory.txt":
                current = _file_info(source / "GameInstallDirectory.txt", source, item["path"], item["role"])
                item.update(sha256=current["sha256"], bytes=current["bytes"],
                            local_adaptation="Fresh-build game reference configuration")
        # Record the generated local configuration even when absent from the checkout.
        if not any(item["path"] == "modkit/GameInstallDirectory.txt" for item in captured):
            captured.append(_file_info(source / "GameInstallDirectory.txt", source,
                                      "modkit/GameInstallDirectory.txt", "modkit_dependency"))
        staged_config_identity = _file_info(staging_config, source, "modkit/Config/DefaultGame.ini", "modkit_dependency")
        for item in captured:
            if item["path"] == staged_config_identity["path"]:
                item.update(sha256=staged_config_identity["sha256"], bytes=staged_config_identity["bytes"],
                            local_adaptation="Fresh-build selected plugin UFS staging remap from Plugins to Mods")
                break
        else:
            captured.append({**staged_config_identity,
                             "local_adaptation": "Fresh-build selected plugin UFS staging remap from Plugins to Mods"})
        captured.extend(item for item in dependencies if not item["path"].startswith("modkit/"))
        return source, plugin, sorted(captured, key=lambda item: item["path"])

    def _commands(self, recipe: dict, source: Path, stage: Path) -> list[str]:
        environment = recipe["environment"]
        # Direct execution preserves the quoted nested cooker arguments that cmd.exe splits.
        # RunUAT's precompiled branch runs this same bundled host and AutomationTool assembly.
        return [environment["files"]["dotnet"]["path"], environment["files"]["automation_tool"]["path"],
                "-noP4", "-nocompile", "BuildCookRun",
                "-project=" + str(source / "Subnautica2.uproject"), "-unrealexe=" + environment["files"]["editor_commandlet"]["path"],
                "-platform=Win64", "-clientconfig=Shipping", "-cook", "-stage", "-pak", "-iostore", "-compressed",
                "-dlcname=" + recipe["plugin_id"], "-basedonreleaseversion=SN2Base",
                "-additionalcookeroptions=" + " ".join(recipe["cook_options"]),
                "-stagingdirectory=" + str(stage), "-utf8output", "-unattended", "-nop4",
                "uebp_LogFolder=" + str(stage.parent / "uat-logs"),
                "uebp_FinalLogFolder=" + str(stage.parent / "uat-logs"),
                "uebp_EngineSavedFolder=" + str(stage.parent / "uat-saved")]

    def _immutable_inputs(self, recipe: dict, captured: list[dict]) -> list[Path]:
        """Protect selected source/capture and launched tools, not every vendor asset.

        Engine/game dependencies retain complete initial and final byte inventories.
        Read handles for all vendor files add no source-capture evidence and can make
        a precompiled cook's preparation exceed its caller deadline on Windows.
        """
        paths = [Path(item[key]) for item in recipe["inputs"]
                 for key in ("origin_path", "source_path")]
        paths.extend(Path(item["source_path"]) for item in captured
                     if item["path"].startswith("modkit/"))
        paths.extend(Path(item["path"]) for item in recipe["environment"]["files"].values()
                     if item["available"])
        return sorted(set(paths), key=lambda path: os.path.normcase(str(path)))

    def _outputs(self, stage: Path, plugin: Path, output: Path, plugin_id: str) -> list[dict]:
        candidates = [stage / project / kind / plugin_id
                      for project in ("Subnautica2", "Windows/Subnautica2")
                      for kind in ("Plugins", "Mods")]
        # UAT can remap the descriptor/UFS entries while retaining container output
        # under Plugins. Select the container subtree rather than every staged root.
        found = [path for path in candidates if (path / "Content" / "Paks" / "Windows").is_dir()]
        if len(found) != 1:
            raise ContentCookError("unidentified_cook_output", "The cooker did not produce one identified selected-plugin staging subtree")
        staged = _checked(found[0], stage, directory=True)
        paks = staged / "Content" / "Paks" / "Windows"
        if not paks.is_dir():
            raise ContentCookError("missing_cook_output", "The selected plugin has no staged Windows containers")
        containers = _inventory(paks, "Content/Paks/Windows", "cook_output")
        grouped = {}
        for item in containers:
            suffix = Path(item["path"]).suffix.lower()
            if suffix not in {".pak", ".utoc", ".ucas"}:
                continue
            grouped.setdefault(str(Path(item["path"]).with_suffix("")), set()).add(suffix)
        if not grouped or any(suffixes != {".pak", ".utoc", ".ucas"} for suffixes in grouped.values()):
            raise ContentCookError("incomplete_cook_output", "Every selected IoStore container stem must have pak, utoc and ucas outputs")
        result = []
        descriptor = plugin / (plugin_id + ".uplugin")
        files = [(descriptor, plugin_id + ".uplugin", "plugin_descriptor")]
        files.extend((Path(item["source_path"]), item["path"], Path(item["path"]).suffix[1:])
                     for item in containers if Path(item["path"]).suffix.lower() in {".pak", ".utoc", ".ucas"})
        for original, relative, role in files:
            target = output.joinpath(*_parts(relative))
            self._directory(target.parent)
            with target.open("xb") as destination, original.open("rb") as stream:
                shutil.copyfileobj(stream, destination, length=1024 * 1024)
            evidence = _file_info(target, output, relative, role)
            if evidence["sha256"] != _file_info(original, original.parent, relative, role)["sha256"]:
                raise ContentCookError("output_changed", "A staged output changed during output capture")
            result.append({**evidence, "path": str(target), "relative_path": relative,
                           "authoritative_cook_output": False})
        return result

    def build(self, project_path: str, timeout_seconds: float = 3600) -> dict:
        timeout_seconds = _positive(timeout_seconds, "timeout_seconds")
        recipe = self.recipe(project_path)
        started, build_id = time.monotonic(), uuid.uuid4().hex
        directory = self._directory(self.workspace_root / "artifacts" / "content-builds" / build_id)
        receipt_path = directory / "receipt.json"
        receipt = {"kind": "sn2_content_cook_receipt", "schema": 1, "source": "content_cook",
                   "build_id": build_id, "receipt_path": str(receipt_path), "project_path": project_path,
                   "plugin_id": recipe["plugin_id"], "source_manifest_sha256": recipe["source_manifest_sha256"],
                   "inputs": recipe["inputs"], "recipe": recipe, "state": "preparing", "started_at": _now(),
                   "timeout_seconds": timeout_seconds, "stages": {}, "commands": [], "reports": [], "outputs": [],
                   "compiled": False, "exact_source_compiled": False, "workspace_current_revision_compiled": False,
                   "installed_in_game": False, "runtime_validation": "not_performed", "resident_worker": False,
                   "cleanup": {"worker_started": False, "tree_exit_confirmed": True},
                   "dependency_capture_complete": False}
        _write_json(receipt_path, receipt)
        _write_json(directory.parent / "latest.json", {"build_id": build_id})
        try:
            if recipe["environment"]["missing"]:
                receipt.update(state="blocked", blocked_reasons=recipe["environment"]["missing"])
                return self._finish(receipt, receipt_path, started)
            receipt["state"] = "hashing_dependencies"
            _write_json(receipt_path, receipt)
            dependencies = self._dependencies()
            receipt["origin_dependencies"] = dependencies
            receipt["state"] = "capturing_inputs"
            _write_json(receipt_path, receipt)
            source, plugin, captured = self._capture(recipe, directory, dependencies)
            receipt.update(staged_source_root=str(plugin), staged_modkit_root=str(source),
                           shared_dependencies=captured, dependency_manifest_sha256=_manifest(captured),
                           output_root=str(directory / "output" / recipe["plugin_id"]))
            stage = self._directory(directory / "stage")
            output = self._directory(Path(receipt["output_root"]))
            self._directory(Path(recipe["ddc"]["path"]))
            receipt["build_identity"] = {"build_id": build_id, "source_manifest_sha256": receipt["source_manifest_sha256"],
                                         "dependency_manifest_sha256": receipt["dependency_manifest_sha256"],
                                         "engine_version": recipe["environment"]["engine_version"],
                                         "modkit_head": (recipe["modkit_git"] or {}).get("head"),
                                         "target_platform": "Win64", "configuration": "Shipping", "base_release": "SN2Base"}
            arguments = self._commands(recipe, source, stage)
            receipt["commands"] = [arguments]
            receipt["staging_directory"] = str(stage)
            with _destination_lock(self.workspace_root / "artifacts" / "content-builds"), ExitStack() as guards:
                for selected in (source, self._engine_directory(), self._game_reference_root()):
                    guards.enter_context(_guard_directories(selected, selected))
                immutable_inputs = self._immutable_inputs(recipe, captured)
                guards.enter_context(_guard_input_files(immutable_inputs))
                receipt["input_files_write_protected_during_cook"] = os.name == "nt"
                receipt["immutable_input_scope"] = {
                    "selected_source_and_capture": True,
                    "captured_modkit_and_launch_tools": True,
                    "write_protected_file_count": len(immutable_inputs) if os.name == "nt" else 0,
                    "shared_engine_and_game_dependencies": "Full initial and final byte inventories; directory guards",
                    "all_shared_dependency_files_write_protected": False,
                }
                receipt["dependency_capture_complete"] = True
                receipt["cleanup"]["worker_started"] = True
                receipt["state"] = "cooking"
                _write_json(receipt_path, receipt)
                remaining = timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    raise ContentCookError("deadline_exhausted", "Input capture exhausted the caller's cook deadline")
                try:
                    command = self.runner(arguments, source, remaining, directory / "cook.stdout.log", directory / "cook.stderr.log")
                except BuildError as error:
                    if error.receipt is not None:
                        receipt["stages"]["cook"] = error.receipt
                    raise
                receipt["stages"]["cook"] = command
                receipt["cleanup"] = {"worker_started": True, "tree_exit_confirmed": command.get("tree_exit_confirmed", False),
                                      "termination_requested": command.get("termination_requested", False)}
                if command.get("state") != "succeeded" or command.get("exit_code") != 0:
                    raise ContentCookError("cooker_failed", "The cooker command did not succeed")
                if not command.get("tree_exit_confirmed") or not command.get("output_complete"):
                    raise ContentCookError("cleanup_unconfirmed", "Cook command completion, output or worker-tree exit is unconfirmed")
                reports = self._reports(receipt)
                if any(report["error_reported"] for report in reports):
                    raise ContentCookError("cooker_errors", "The cooker reported errors; inspect the complete reports")
                receipt["outputs"] = self._outputs(stage, plugin, output, recipe["plugin_id"])
                receipt["state"] = "verifying_dependencies"
                _write_json(receipt_path, receipt)
                after = self._captured_dependencies(source, recipe["plugin_id"])
                receipt["dependencies_stable"] = _manifest(after) == receipt["dependency_manifest_sha256"]
                captured_inputs = _validate_project(plugin.parent, recipe["plugin_id"])["files"]
                receipt["staged_source_stable"] = _manifest(captured_inputs) == receipt["source_manifest_sha256"]
                if not receipt["dependencies_stable"] or not receipt["staged_source_stable"]:
                    raise ContentCookError("inputs_changed", "Captured inputs or shared dependencies changed during cooking")
                receipt.update(state="succeeded", compiled=True, exact_source_compiled=True)
                for item in receipt["outputs"]:
                    item["authoritative_cook_output"] = True
        except (ContentCookError, BuildError, WorkspaceError, OSError, ValueError, KeyError) as error:
            receipt.update(state="failed", failure_code=getattr(error, "code", "cook_evidence_error"),
                           error={"code": getattr(error, "code", "cook_evidence_error"),
                                  "message": str(error) if isinstance(error, (ContentCookError, BuildError, WorkspaceError))
                                  else "Could not establish complete cook input/output evidence"})
            command = receipt["stages"].get("cook")
            if command is not None:
                receipt["cleanup"].update(tree_exit_confirmed=command.get("tree_exit_confirmed", False),
                                          termination_requested=command.get("termination_requested", False))
        except BaseException:
            receipt.update(state="failed", failure_code="interrupted")
            self._finish(receipt, receipt_path, started)
            raise
        return self._finish(receipt, receipt_path, started)

    def _reports(self, receipt: dict) -> list[dict]:
        reports = []
        for name, command in receipt["stages"].items():
            for stream in ("stdout", "stderr"):
                evidence = command.get(stream)
                if not evidence:
                    continue
                raw = _read_bytes(Path(evidence["path"]), Path(receipt["receipt_path"]).parent, None)
                if _sha(raw) != evidence["sha256"] or len(raw) != evidence["bytes"]:
                    raise ContentCookError("report_changed", "A cooker report differs from its command receipt")
                text = raw.decode("utf-8", errors="replace")
                report = {"stage": name, "stream": stream, "path": evidence["path"],
                          "sha256": _sha(raw), "bytes": len(raw), "text": text,
                          "encoding": "utf-8", "decode_errors": "replace", "truncated": False,
                          "error_reported": bool(re.search(r"(?im)(?:^|\b)(?:fatal\s+error\b|error\s*:)", text))}
                if self.diagnostics_parser is not None:
                    report["diagnostics"] = self.diagnostics_parser(text, receipt["inputs"],
                                                                   Path(receipt.get("staged_source_root", self.workspace_root)))
                reports.append(report)
        return reports

    def _finish(self, receipt: dict, path: Path, started: float) -> dict:
        try:
            current = self._project_validation(receipt["project_path"])
            receipt["workspace_source_stable"] = current.get("project_source_manifest_sha256") == receipt["source_manifest_sha256"]
            receipt["workspace_current_revision_compiled"] = receipt["exact_source_compiled"] and receipt["workspace_source_stable"]
            receipt["reports"] = self._reports(receipt)
        except (ContentCookError, WorkspaceError, OSError, ValueError, KeyError) as error:
            receipt.update(state="failed", failure_code=getattr(error, "code", "evidence_error"),
                           exact_source_compiled=False, workspace_current_revision_compiled=False)
        receipt["finished_at"], receipt["elapsed_seconds"] = _now(), time.monotonic() - started
        _write_json(path, receipt)
        receipt["receipt_sha256"] = _sha(_read_bytes(path, self.workspace_root, None))
        return receipt

    def status(self, build_id: str | None = None) -> dict:
        root = self.workspace_root / "artifacts" / "content-builds"
        if build_id is None:
            if not (root / "latest.json").is_file():
                return {"source": "content_cook_status", "state": "not_built", "resident_worker": False}
            build_id = json.loads(_read_bytes(root / "latest.json", self.workspace_root, None))["build_id"]
        if not isinstance(build_id, str) or not re.fullmatch(r"[0-9a-f]{32}", build_id):
            raise ContentCookError("invalid_build_id", "build_id must be a returned content cook identifier")
        path = root / build_id / "receipt.json"
        raw = _read_bytes(path, self.workspace_root, None)
        return {**json.loads(raw), "receipt_path": str(path), "receipt_sha256": _sha(raw), "resident_worker": False}

    def read_report(self, build_id: str, stage: str = "cook", stream: str = "stdout") -> dict:
        receipt = self.status(build_id)
        if stream not in {"stdout", "stderr"}:
            raise ContentCookError("unknown_report", "The requested report stream is unavailable")
        evidence = receipt.get("stages", {}).get(stage, {}).get(stream)
        if not evidence:
            raise ContentCookError("unknown_report", "The requested cook stage or report is unavailable")
        raw = _read_bytes(Path(evidence["path"]), Path(receipt["receipt_path"]).parent, None)
        return {"source": "content_cook_report", "build_id": build_id, "stage": stage, "stream": stream,
                "path": evidence["path"], "bytes": len(raw), "sha256": _sha(raw),
                "receipt_matches": len(raw) == evidence["bytes"] and _sha(raw) == evidence["sha256"],
                "text": raw.decode("utf-8", errors="replace"), "encoding": "utf-8", "truncated": False}
