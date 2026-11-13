"""Install verified content plugins at the game's ProjectModsDir boundary.

File installation is distinct from plugin discovery, mounting, asset registration
and Blueprint activity. Replacing resident Unreal packages requires a stopped
game; actor destruction alone does not establish that boundary.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Callable
import uuid

from .development import DevelopmentError, RuntimeService
from .content_projects import ContentProjectError
from .workspace import WorkspaceError, _checked, _destination_lock, _guard_directories, _parts, _read_bytes


_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_PLUGIN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
RECEIPT_KIND = "sn2_content_deployment_receipt"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8")


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _inventory_sha(inventory: list[dict]) -> str:
    return _hash(json.dumps(inventory, sort_keys=True, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8"))


class ContentDeployment:
    """Compose package verification and exact game process snapshots.

    The verifier is ContentProjects.verify. Its returned package_root and files
    are authoritative; only the plugin-prefixed payload is installed. Receipts
    remain in the bridge build directory, not in the game plugin directory.
    """

    def __init__(self, runtime_service: RuntimeService, *, verifier: Callable[[str], dict],
                 central_root: str | Path):
        self.runtime = runtime_service
        self.verifier = verifier
        try:
            self.game = _checked(Path(runtime_service.game), directory=True)
            self.repository = _checked(Path(runtime_service.repository), directory=True)
            self.central = _checked(Path(central_root), directory=True)
            self.project = _checked(self.game / "Subnautica2", self.game, directory=True)
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        # Confirmed by ProjectModsDir and ConvertRelativePathToFull in Shipping.
        self.mods = self.project / "Mods"
        self.records = self.repository / "build" / "content-deployment"
        self._lock = threading.RLock()

    @staticmethod
    def _plugin(value: str) -> str:
        if not isinstance(value, str) or not _PLUGIN.fullmatch(value):
            raise DevelopmentError("invalid_plugin_id", "A valid Unreal plugin identifier is required")
        try:
            _parts(value)
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        return value

    @staticmethod
    def _expected_sha(value: str, label: str) -> str:
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise DevelopmentError("invalid_hash", f"The expected {label} SHA-256 is required")
        return value.lower()

    @staticmethod
    def _mkdir(path: Path, boundary: Path) -> Path:
        if not path.is_relative_to(boundary):
            raise WorkspaceError("unsafe_path", "The directory is outside its boundary")
        current = boundary
        _checked(current, boundary, directory=True)
        for piece in path.relative_to(boundary).parts:
            current = current / piece
            if not os.path.lexists(current):
                current.mkdir()
            _checked(current, boundary, directory=True)
        return current

    def _stopped(self) -> dict:
        snapshot = self.runtime.process_state()
        if (not isinstance(snapshot, dict) or not isinstance(snapshot.get("processes"), list)
                or type(snapshot.get("running")) is not bool
                or snapshot["running"] != bool(snapshot["processes"])
                or snapshot.get("source") != "native_process_handle_snapshot"):
            raise DevelopmentError("unconfirmed_process_state", "The game process state is not confirmed")
        if snapshot["running"]:
            raise DevelopmentError("game_running", "Content installation or removal requires a stopped game",
                                   receipt={"process_snapshot": copy.deepcopy(snapshot)})
        return copy.deepcopy(snapshot)

    def _inventory(self, directory: Path, boundary: Path) -> list[dict]:
        root = _checked(directory, boundary, directory=True)
        entries: list[dict] = []
        seen: set[str] = set()
        def inaccessible(error: OSError) -> None:
            raise DevelopmentError("access_error", "Could not enumerate the complete plugin inventory") from error

        for parent, directories, files in os.walk(root, followlinks=False, onerror=inaccessible):
            for name in sorted(directories + files):
                path = _checked(Path(parent) / name, root)
                relative = path.relative_to(root).as_posix()
                _parts(relative)
                folded = relative.casefold()
                if folded in seen:
                    raise DevelopmentError("duplicate_path", "The inventory contains duplicate Windows paths")
                seen.add(folded)
                if path.is_dir():
                    entries.append({"path": relative, "type": "directory"})
                else:
                    if path.suffix.casefold() in {".dll", ".exe"}:
                        raise DevelopmentError("unsupported_payload", "Content plugins cannot contain DLL or executable payloads")
                    raw = _read_bytes(path, root, None)
                    entries.append({"path": relative, "type": "file", "sha256": _hash(raw), "bytes": len(raw)})
        return sorted(entries, key=lambda item: item["path"])

    def _write(self, path: Path, value: dict) -> None:
        parent = self._mkdir(path.parent, self.repository)
        temporary = parent / ("." + path.name + "." + uuid.uuid4().hex)
        with _guard_directories(parent, self.repository):
            with temporary.open("xb") as stream:
                stream.write(_json_bytes(value))
                stream.flush()
                os.fsync(stream.fileno())
            if os.path.lexists(path):
                _checked(path, self.repository)
            temporary.replace(path)

    def _save(self, receipt: dict) -> None:
        self._write(self.records / "operations" / (receipt["operation_id"] + ".json"), receipt)
        self._write(self.records / "current" / (receipt["plugin_id"] + ".json"), receipt)

    def _previous(self, plugin: str) -> dict | None:
        path = self.records / "current" / (plugin + ".json")
        if not os.path.lexists(path):
            return None
        raw = _read_bytes(path, self.repository, None)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise DevelopmentError("invalid_deployment_receipt", "The deployment receipt is unreadable") from None
        if (not isinstance(value, dict) or value.get("kind") != RECEIPT_KIND
                or value.get("schema") != 1 or value.get("plugin_id") != plugin
                or value.get("game_root") != str(self.game)
                or value.get("destination") != str(self.mods / plugin)):
            raise DevelopmentError("invalid_deployment_receipt", "The deployment receipt does not identify this target")
        return value

    def _owned(self, plugin: str, expected: str | None) -> tuple[dict | None, list[dict] | None]:
        previous = self._previous(plugin)
        destination = self.mods / plugin
        if previous and previous.get("state") not in {"installed", "removed", "failed_without_installation"}:
            raise DevelopmentError("deployment_unresolved", "Inspect the unresolved deployment before another operation",
                                   outcome_unknown=(previous.get("outcome_unknown", True)
                                                    or previous.get("state") in {"publish_requested", "replacement_requested", "removal_requested"}),
                                   receipt=previous)
        if not os.path.lexists(destination):
            if previous and previous.get("state") == "installed":
                raise DevelopmentError("installation_changed", "The recorded installation is missing", receipt=previous)
            if expected is not None:
                raise DevelopmentError("installation_changed", "There is no installed inventory matching the expected SHA-256")
            return previous, None
        if not previous or previous.get("state") != "installed":
            raise DevelopmentError("unknown_installation", "The destination is not an identified installation from this bridge")
        inventory = self._inventory(destination, self.mods)
        actual = _inventory_sha(inventory)
        if inventory != previous.get("installed_inventory") or actual != previous.get("installed_inventory_sha256"):
            raise DevelopmentError("installation_changed", "The installed plugin inventory changed", receipt=previous)
        if expected is None:
            raise DevelopmentError("expected_installation_required", "Replacing or removing a plugin requires its expected installed inventory SHA-256")
        if actual != self._expected_sha(expected, "installed inventory"):
            raise DevelopmentError("installation_changed", "The installed inventory does not match the expected SHA-256")
        return previous, inventory

    def _package(self, package_path: str, expected: str) -> tuple[dict, Path, str, list[dict]]:
        try:
            report = self.verifier(package_path)
        except ContentProjectError as error:
            raise DevelopmentError(error.code, str(error), receipt=error.receipt) from None
        if (not isinstance(report, dict) or report.get("valid") is not True
                or report.get("compiled") is not True or not isinstance(report.get("files"), list)
                or not isinstance(report.get("metadata"), dict)
                or report["metadata"].get("kind") != "sn2_content_mod_package"):
            raise DevelopmentError("invalid_content_package", "The package verifier did not establish a compiled content package")
        if report.get("manifest_sha256") != self._expected_sha(expected, "package manifest"):
            raise DevelopmentError("package_changed", "The package manifest changed")
        plugin = self._plugin(report.get("plugin_id"))
        root = _checked(Path(report.get("package_root", "")), directory=True)
        files = []
        seen: set[str] = set()
        for entry in report["files"]:
            if not isinstance(entry, dict):
                raise DevelopmentError("invalid_content_package", "The package inventory entry is invalid")
            parts = _parts(entry.get("path"))
            if parts[0] != plugin:
                continue
            if len(parts) < 2 or "/".join(parts).casefold() in seen:
                raise DevelopmentError("invalid_content_package", "The plugin payload path is invalid or duplicated")
            seen.add("/".join(parts).casefold())
            if Path(parts[-1]).suffix.casefold() in {".dll", ".exe"}:
                raise DevelopmentError("unsupported_payload", "Content plugins cannot contain DLL or executable payloads")
            if type(entry.get("bytes")) is not int or entry["bytes"] < 0:
                raise DevelopmentError("invalid_content_package", "The payload byte count is invalid")
            self._expected_sha(entry.get("sha256"), "payload")
            files.append(copy.deepcopy(entry))
        if f"{plugin}/{plugin}.uplugin".casefold() not in seen:
            raise DevelopmentError("invalid_content_package", "The plugin descriptor is missing")
        return report, root, plugin, sorted(files, key=lambda entry: entry["path"])

    def _move_to_trash(self, source: Path, receipt: dict, label: str) -> Path:
        checked = _checked(source, self.game, directory=True)
        inventory = self._inventory(checked, self.game)
        relative = checked.relative_to(self.game)
        trash = self._mkdir(self.central / ".trash" / "Subnautica 2 MCP" / "content-deployment" /
                            receipt["operation_id"] / label / "game", self.central)
        destination = trash / relative
        self._mkdir(destination.parent, self.central)
        if os.path.lexists(destination):
            raise DevelopmentError("trash_collision", "The selected trash destination already exists")
        receipt.setdefault("moves", []).append({"source": str(source), "destination": str(destination),
                                               "state": "requested"})
        self._save(receipt)
        with _guard_directories(source.parent, self.game), _guard_directories(destination.parent, self.central):
            checked = _checked(source, self.game, directory=True)
            if self._inventory(checked, self.game) != inventory:
                raise DevelopmentError("installation_changed", "The plugin inventory changed before its move")
            if os.path.lexists(destination):
                raise DevelopmentError("trash_collision", "The selected trash destination appeared before its move")
            if label in {"replaced", "removed"}:
                self._stopped()
            shutil.move(str(checked), str(destination))
        _checked(destination, self.central, directory=True)
        if self._inventory(destination, self.central) != inventory:
            raise DevelopmentError("move_incomplete", "The moved plugin inventory differs from its source")
        if os.path.lexists(source):
            raise DevelopmentError("move_incomplete", "The original plugin directory still exists after the move")
        receipt["moves"][-1]["state"] = "moved"
        self._save(receipt)
        return destination

    def _receipt(self, plugin: str, operation: str, snapshot: dict) -> dict:
        return {"kind": RECEIPT_KIND, "schema": 1, "operation_id": uuid.uuid4().hex,
                "operation": operation, "plugin_id": plugin, "game_root": str(self.game),
                "destination": str(self.mods / plugin), "state": "prepared",
                "installed": False, "outcome_unknown": False, "process_snapshot": snapshot,
                "plugin_discovery": "not_performed", "plugin_mount": "not_performed",
                "asset_registration": "not_performed", "blueprint_activity": "not_performed"}

    def install(self, package_path: str, expected_manifest_sha256: str, *,
                expected_installed_inventory_sha256: str | None = None) -> dict:
        """Stage and publish a fully verified plugin; never overwrite unknown files."""
        receipt: dict | None = None
        stage: Path | None = None
        effects_started = False
        previous = None
        try:
            with self._lock:
                report, root, plugin, files = self._package(package_path, expected_manifest_sha256)
                destination = self.mods / plugin
                with _destination_lock(destination):
                    snapshot = self._stopped()
                    previous, old_inventory = self._owned(plugin, expected_installed_inventory_sha256)
                    receipt = self._receipt(plugin, "replace" if old_inventory is not None else "install", snapshot)
                    receipt.update({"package_path": package_path, "package_manifest_sha256": report["manifest_sha256"],
                                    "activation": copy.deepcopy(report.get("activation")),
                                    "previous_operation_id": previous.get("operation_id") if previous else None})
                    stage = self.project / (".sn2-mcp-content-stage-" + receipt["operation_id"])
                    receipt["stage"] = str(stage)
                    self._save(receipt)
                    with _guard_directories(self.project, self.game), _guard_directories(root, root):
                        stage.mkdir()
                        for entry in files:
                            parts = _parts(entry["path"])
                            raw = _read_bytes(root.joinpath(*parts), root, None)
                            if len(raw) != entry["bytes"] or _hash(raw) != entry["sha256"]:
                                raise DevelopmentError("package_changed", "A verified payload changed during staging")
                            target = stage.joinpath(*parts[1:])
                            self._mkdir(target.parent, stage)
                            with target.open("xb") as stream:
                                stream.write(raw)
                        inventory = self._inventory(stage, self.game)
                        expected_files = [{"path": entry["path"].split("/", 1)[1], "type": "file",
                                           "sha256": entry["sha256"], "bytes": entry["bytes"]} for entry in files]
                        if [entry for entry in inventory if entry["type"] == "file"] != sorted(expected_files, key=lambda entry: entry["path"]):
                            raise DevelopmentError("stage_changed", "The staged payload inventory changed")
                        again, _, again_plugin, again_files = self._package(package_path, expected_manifest_sha256)
                        if again_plugin != plugin or again_files != files or again != report:
                            raise DevelopmentError("package_changed", "The verified package changed during staging")
                    receipt.update({"state": "staged", "installed_inventory": inventory,
                                    "installed_inventory_sha256": _inventory_sha(inventory)})
                    self._save(receipt)
                    self._stopped()
                    # Recheck the previous receipt's inventory after staging; the current
                    # prepared receipt deliberately records this operation already.
                    if old_inventory is not None and self._inventory(destination, self.mods) != old_inventory:
                        raise DevelopmentError("installation_changed", "The installed plugin changed during staging")
                    if old_inventory is None and os.path.lexists(destination):
                        raise DevelopmentError("unknown_installation", "The destination appeared during staging")
                    self._mkdir(self.mods, self.game)
                    if old_inventory is not None:
                        effects_started = True
                        receipt.update({"state": "replacement_requested", "outcome_unknown": True})
                        self._save(receipt)
                        self._move_to_trash(destination, receipt, "replaced")
                        self._stopped()
                    if self._inventory(stage, self.game) != inventory:
                        raise DevelopmentError("stage_changed", "The staged plugin changed before publication")
                    receipt.update({"state": "publish_requested", "outcome_unknown": True})
                    self._save(receipt)
                    effects_started = True
                    with _guard_directories(self.mods, self.game):
                        if os.path.lexists(destination):
                            raise DevelopmentError("destination_collision", "The destination appeared before publication")
                        self._stopped()
                        stage.rename(destination)
                    stage = None
                    if self._inventory(destination, self.mods) != inventory:
                        raise DevelopmentError("installation_changed", "The published inventory differs from the staged inventory")
                    receipt.update({"state": "installed", "installed": True, "outcome_unknown": False, "restart_required": True,
                                    "process_snapshot_after": self._stopped()})
                    self._save(receipt)
                    return copy.deepcopy(receipt)
        except (DevelopmentError, WorkspaceError, OSError) as error:
            self._failed(error, receipt, stage, effects_started, previous)
            raise AssertionError("unreachable")

    def _failed(self, error: Exception, receipt: dict | None, stage: Path | None,
                effects_started: bool, previous: dict | None) -> None:
        if receipt is None:
            if isinstance(error, DevelopmentError):
                raise error
            raise DevelopmentError(getattr(error, "code", "access_error"), str(error)) from None
        with self._lock, _destination_lock(self.mods / receipt["plugin_id"]):
            current = self._previous(receipt["plugin_id"])
            if current is not None and current.get("operation_id") != receipt["operation_id"]:
                receipt.update({"state": "deployment_unresolved", "outcome_unknown": True,
                                "receipt_persistence_or_cleanup": "current_operation_changed"})
                self._write(self.records / "operations" / (receipt["operation_id"] + ".json"), receipt)
                raise DevelopmentError(getattr(error, "code", "access_error"), str(error),
                                       outcome_unknown=True, receipt=copy.deepcopy(receipt)) from None
            self._failed_locked(error, receipt, stage, effects_started, previous)

    def _failed_locked(self, error: Exception, receipt: dict, stage: Path | None,
                       effects_started: bool, previous: dict | None) -> None:
        receipt.update({"state": "deployment_unresolved" if effects_started else "failed_without_installation",
                        "outcome_unknown": effects_started, "error_code": getattr(error, "code", "access_error"),
                        "error_message": str(error)})
        if isinstance(error, DevelopmentError) and error.receipt is not None:
            receipt["failure_receipt"] = copy.deepcopy(error.receipt)
        # An interrupted cross-volume move may leave both paths. Preserve them and
        # the requested move receipt; never retry or remove either automatically.
        try:
            self._save(receipt)
            if stage is not None and os.path.lexists(stage):
                self._move_to_trash(stage, receipt, "failed-stage")
            if not effects_started and previous is not None:
                self._write(self.records / "current" / (receipt["plugin_id"] + ".json"), previous)
        except (DevelopmentError, WorkspaceError, OSError):
            receipt["state"] = "deployment_unresolved"
            receipt["outcome_unknown"] = True
            receipt["receipt_persistence_or_cleanup"] = "incomplete"
            try:
                self._save(receipt)
            except (DevelopmentError, WorkspaceError, OSError):
                # The returned receipt still identifies every known path. A
                # persistence failure cannot be presented as a durable result.
                pass
        raise DevelopmentError(getattr(error, "code", "access_error"), str(error),
                               outcome_unknown=receipt["outcome_unknown"], receipt=copy.deepcopy(receipt)) from None

    def uninstall(self, plugin_id: str, expected_installed_inventory_sha256: str) -> dict:
        """Move one unchanged bridge installation to central project trash."""
        receipt = None
        effects_started = False
        previous = None
        try:
            plugin = self._plugin(plugin_id)
            self._expected_sha(expected_installed_inventory_sha256, "installed inventory")
            with self._lock, _destination_lock(self.mods / plugin):
                snapshot = self._stopped()
                previous, inventory = self._owned(plugin, expected_installed_inventory_sha256)
                if inventory is None:
                    raise DevelopmentError("not_installed", "The selected plugin is not installed")
                receipt = self._receipt(plugin, "uninstall", snapshot)
                receipt.update({"state": "removal_requested", "previous_operation_id": previous["operation_id"],
                                "removed_inventory": inventory,
                                "removed_inventory_sha256": _inventory_sha(inventory)})
                self._save(receipt)
                self._stopped()
                if self._inventory(self.mods / plugin, self.mods) != inventory:
                    raise DevelopmentError("installation_changed", "The installed plugin changed before removal")
                effects_started = True
                receipt["outcome_unknown"] = True
                self._move_to_trash(self.mods / plugin, receipt, "removed")
                receipt.update({"state": "removed", "installed": False, "outcome_unknown": False,
                                "process_snapshot_after": self._stopped()})
                self._save(receipt)
                return copy.deepcopy(receipt)
        except (DevelopmentError, WorkspaceError, OSError) as error:
            self._failed(error, receipt, None, effects_started, previous)
            raise AssertionError("unreachable")

    def status(self, plugin_id: str | None = None) -> dict:
        """Inspect installed inventory without inferring runtime activity."""
        try:
            with self._lock:
                if plugin_id is None:
                    current = self.records / "current"
                    plugins = []
                    if os.path.lexists(current):
                        current = _checked(current, self.repository, directory=True)
                        plugins = [self._plugin(path.stem) for path in current.iterdir()
                                   if path.suffix == ".json" and not path.name.startswith(".")]
                    return {"source": "content_deployment_inventory", "mods_directory": str(self.mods),
                            "plugins": [self.status(plugin) for plugin in sorted(plugins)],
                            "runtime_validation": "not_performed"}
                plugin = self._plugin(plugin_id)
                previous = self._previous(plugin)
                destination = self.mods / plugin
                inventory = self._inventory(destination, self.mods) if os.path.lexists(destination) else None
                intact = bool(previous and previous.get("state") == "installed" and inventory is not None
                              and inventory == previous.get("installed_inventory")
                              and _inventory_sha(inventory) == previous.get("installed_inventory_sha256"))
                return {"source": "content_deployment_inventory", "plugin_id": plugin,
                        "destination": str(destination), "installed_files": inventory is not None,
                        "identified_installation": intact,
                        "installed_inventory": inventory,
                        "installed_inventory_sha256": _inventory_sha(inventory) if inventory is not None else None,
                        "receipt": copy.deepcopy(previous), "runtime_validation": "not_performed",
                        "plugin_discovery": "not_performed", "plugin_mount": "not_performed",
                        "asset_registration": "not_performed", "blueprint_activity": "not_performed"}
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
