"""Capture an existing mod specification and compile one asset in the live game.

This service uses the native Unreal compiler backend, never an Editor worker.
In-memory compilation, runtime publication and persistent package output remain
separate results. Native channel failures retain their original effect semantics.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable
import uuid

from .content_authoring import ContentAuthoringError, _specification
from .content_cooker import _write_json
from .content_projects import ContentProjectError, _json, _stream_identity, source_manifest_sha256, validate_content_project
from .native_client import NativeError, _timeout
from .workspace import SourceWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories


RECEIPT_KIND = "sn2_runtime_authoring_receipt"
BUILD_RECEIPT_KIND = "sn2_runtime_build_receipt"
Request = Callable[[str, dict[str, Any], float | None], dict[str, Any]]


class RuntimeAuthoringError(Exception):
    def __init__(self, code: str, message: str, receipt: dict[str, Any] | None = None,
                 *, may_have_executed: bool = False):
        super().__init__(message)
        self.code = code
        self.receipt = receipt
        self.details = {"executed": may_have_executed, "may_have_executed": may_have_executed,
                        "process_event_returned": False}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeAuthoring:
    """Source correlation and receipts for the inherited schema's native backend."""

    def __init__(self, workspace_root: Path, request: Request, *, workspace: SourceWorkspace | None = None):
        self.root = _checked(Path(workspace_root).absolute(), directory=True)
        self.workspace = workspace or SourceWorkspace(self.root)
        if self.workspace.root != self.root:
            raise RuntimeAuthoringError("workspace_mismatch", "Runtime authoring must share the source workspace")
        self.request = request
        self.receipts_root = self.root / "artifacts/runtime-authoring"
        self.builds_root = self.root / "artifacts/runtime-builds"

    def _capture(self, project_path: str, spec_path: str, asset_path: str | None) -> tuple[dict, bytes]:
        try:
            project = validate_content_project(self.root, project_path, workspace=self.workspace)
            issues = [item for item in project["issues"] if item["code"] != "entry_asset_missing"]
            if issues:
                raise RuntimeAuthoringError("invalid_content_project", "The selected content project failed validation",
                                            {"project": project, "issues": issues})
            source = self.workspace.read_file_exact(spec_path)
            raw = source["text"].encode("utf-8")
            if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise RuntimeAuthoringError("specification_changed", "The captured specification does not match its source hash")
            spec_file = self.root.joinpath(*self.workspace._source_parts(source["path"]))
            project_root = Path(project["project_root"])
            if spec_file.is_relative_to(project_root):
                relative_spec = spec_file.relative_to(project_root).as_posix()
                inventoried = next((item for item in project["files"]
                                    if item["path"].casefold() == relative_spec.casefold()), None)
                if (inventoried is None or inventoried["sha256"] != source["sha256"]
                        or inventoried["bytes"] != len(raw)):
                    raise RuntimeAuthoringError("specification_changed",
                                                "The specification changed after project source inventory capture")
            specification, asset_files = _specification(raw, project["plugin_id"])
            if (project.get("entry_asset_path") and not project.get("entry_asset_present")
                    and project["entry_asset_path"] not in asset_files):
                raise RuntimeAuthoringError("entry_asset_unresolved", "The missing declared entry asset is absent from the specification")
            targets = [asset for asset in specification["assets"]
                       if asset_path is None or asset["path"] == asset_path]
            if len(targets) != 1:
                raise RuntimeAuthoringError("runtime_asset_selection_required",
                                            "Select exactly one declared asset for this runtime compile operation")
            asset = targets[0]
            if asset["kind"] != "blueprint":
                raise RuntimeAuthoringError("runtime_asset_kind_unsupported", "This native operation compiles Blueprint assets")
            return {"project_path": project_path, "plugin_id": project["plugin_id"],
                    "spec_path": spec_path, "specification_sha256": source["sha256"],
                    "source_manifest_sha256": project["project_source_manifest_sha256"],
                    "source_inputs": project["files"], "asset": asset,
                    "project_root": project["project_root"],
                    "selected_asset_path": asset["path"], "declared_asset_count": len(specification["assets"]),
                    "whole_project_compiled": False}, raw
        except (ContentAuthoringError, ContentProjectError, WorkspaceError) as error:
            raise RuntimeAuthoringError(error.code, str(error), getattr(error, "receipt", None)) from None

    def recipe(self, project_path: str, spec_path: str, asset_path: str | None = None) -> dict[str, Any]:
        captured, _ = self._capture(project_path, spec_path, asset_path)
        return {"source": "runtime_authoring_recipe", "backend": "native_unreal_runtime",
                **captured, "compiled": False, "persisted": False,
                "engine_execution_accepted": False,
                "backend_readiness_checked": False, "execution_performed": False}

    def _directory(self, path: Path) -> Path:
        if not (path.is_relative_to(self.receipts_root) or path.is_relative_to(self.builds_root)):
            raise RuntimeAuthoringError("unsafe_receipt_path", "Runtime authoring receipts must remain in their artifacts directory")
        self.workspace._unprotected(path, forbid_ancestor=True)
        with _destination_lock(path):
            if not path.parent.exists():
                if path.parent.is_relative_to(self.receipts_root) or path.parent.is_relative_to(self.builds_root):
                    self._directory(path.parent)
                else:
                    self.workspace._unprotected(path.parent, forbid_ancestor=True)
                    with _guard_directories(self.root, self.root):
                        path.parent.mkdir(exist_ok=True)
            parent = _checked(path.parent, self.root, directory=True)
            with _guard_directories(parent, self.root):
                path.mkdir(exist_ok=True)
            return _checked(path, self.root, directory=True)

    def _save(self, directory: Path, receipt: dict[str, Any]) -> None:
        with _destination_lock(self.receipts_root), _guard_directories(directory, self.root):
            _write_json(directory / "receipt.json", receipt)
            _write_json(self.receipts_root / "latest.json", {"authoring_id": receipt["authoring_id"]})

    def _capture_files(self, captured: dict[str, Any], directory: Path) -> Path:
        source = self._directory(directory / "source" / captured["plugin_id"])
        project = self.workspace._selected(captured["project_path"])
        for item in captured["source_inputs"]:
            original = _checked(project / item["path"], self.root)
            destination = source / item["path"]
            self._directory(destination.parent)
            with original.open("rb") as incoming, destination.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
            if _stream_identity(destination, source) != {key: item[key] for key in ("sha256", "bytes")}:
                raise RuntimeAuthoringError("source_changed", "A runtime authoring input changed during capture")
        observed = validate_content_project(source.parent, captured["plugin_id"])
        if observed["project_source_manifest_sha256"] != captured["source_manifest_sha256"]:
            raise RuntimeAuthoringError("source_changed", "The complete captured project differs from its input inventory")
        return source

    @staticmethod
    def _captured_inventory(source: Path, inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{**item, "source_path": str(source / item["path"])} for item in inputs]

    def compile(self, project_path: str, spec_path: str, asset_path: str | None = None,
                timeout_seconds: float | None = None) -> dict[str, Any]:
        if timeout_seconds is not None:
            try:
                timeout_seconds = _timeout(timeout_seconds)
            except ValueError as error:
                raise RuntimeAuthoringError("invalid_timeout", str(error)) from None
        captured, raw = self._capture(project_path, spec_path, asset_path)
        authoring_id = uuid.uuid4().hex
        directory = self._directory(self.receipts_root / authoring_id)
        staged_source = self._capture_files(captured, directory)
        leaf = captured["selected_asset_path"].rpartition(".")[2]
        candidate_path = "/" + captured["plugin_id"] + "/__Revision_" + authoring_id + "/" + leaf + "." + leaf
        candidate_asset = {**captured["asset"], "path": candidate_path}
        with _guard_directories(directory, self.root):
            (directory / "specification.json").write_bytes(raw)
            _write_json(directory / "source-manifest.json", {
                "source_manifest_sha256": captured["source_manifest_sha256"],
                "inputs": captured["source_inputs"],
            })
        specification_evidence = {"source_path": str(directory / "specification.json"),
                                  "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
        receipt = {"kind": RECEIPT_KIND, "schema": 1, "authoring_id": authoring_id,
                   "backend": "native_unreal_runtime", "started_at": _now(), "state": "captured",
                   **captured, "compiled": False, "runtime_published": False,
                   "staged_source_root": str(staged_source),
                   "compiler_specification": specification_evidence,
                   "runtime_asset_path": candidate_path, "runtime_asset": candidate_asset,
                   "runtime_identity_role": "Unique live candidate; persistence retains the declared canonical package",
                   "engine_execution_accepted": False,
                   "disk_assets_published": False, "persisted": False, "packaged": False,
                   "source_current": None, "receipt_path": str(directory / "receipt.json"),
                   "automatic_retry": False, "native_response": None}
        self._save(directory, receipt)
        try:
            response = self.request("runtime_compile_blueprint", {"asset": candidate_asset}, timeout_seconds)
        except NativeError as error:
            receipt.update(state="outcome_unknown" if error.may_have_executed else "failed",
                           finished_at=_now(), error={"code": error.code, "message": str(error),
                               "request_id": error.request_id, "may_have_executed": error.may_have_executed,
                               "details": error.details})
            try:
                self._save(directory, receipt)
            except Exception as save_error:
                error.add_note("The native failure receipt could not be persisted: " + str(save_error))
            raise
        except Exception as error:
            receipt.update(state="outcome_unknown", finished_at=_now(),
                           error={"code": "runtime_request_failed", "message": str(error), "may_have_executed": True})
            try:
                self._save(directory, receipt)
            except Exception as save_error:
                error.add_note("The uncertain request receipt could not be persisted: " + str(save_error))
            raise
        receipt["native_response"] = response
        receipt["compiled"] = (isinstance(response, dict)
                               and response.get("source") == "runtime_blueprint_compilation"
                               and response.get("compiled") is True
                               and response.get("runtime_published") is True
                               and response.get("asset_path") == candidate_path
                               and isinstance(response.get("class"), dict)
                               and response["class"].get("path") == candidate_path + "_C"
                               and isinstance(response["class"].get("handle"), str)
                               and bool(response["class"]["handle"]))
        receipt["runtime_published"] = (isinstance(response, dict) and response.get("runtime_published") is True)
        receipt["state"] = "compiled_in_memory" if receipt["compiled"] else "returned_unverified"
        try:
            current, _ = self._capture(project_path, spec_path, captured["selected_asset_path"])
            receipt["source_current"] = (current["source_manifest_sha256"] == captured["source_manifest_sha256"]
                                         and current["specification_sha256"] == captured["specification_sha256"])
        except RuntimeAuthoringError as error:
            receipt["source_current"] = False
            receipt["source_recheck_error"] = {"code": error.code, "message": str(error)}
        receipt["finished_at"] = _now()
        self._save(directory, receipt)
        if receipt["compiled"]:
            with _destination_lock(self.receipts_root), _guard_directories(self.receipts_root, self.root):
                _write_json(self.receipts_root / "working.json", {"authoring_id": authoring_id})
        return receipt

    def save(self, authoring_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        if timeout_seconds is not None:
            try:
                timeout_seconds = _timeout(timeout_seconds)
            except ValueError as error:
                raise RuntimeAuthoringError("invalid_timeout", str(error)) from None
        compiled = self.status(authoring_id)
        if compiled.get("compiled") is not True or compiled.get("runtime_published") is not True:
            raise RuntimeAuthoringError("runtime_compile_unverified", "Select a confirmed in-memory compiled revision")
        source = _checked(Path(compiled["staged_source_root"]), self.root, directory=True)
        specification = compiled.get("compiler_specification")
        if (not isinstance(specification, dict) or specification.get("sha256") != compiled["specification_sha256"]
                or not isinstance(specification.get("source_path"), str)):
            raise RuntimeAuthoringError("compiled_specification_unverified", "The compiled revision has no exact captured compiler specification")
        specification_path = _checked(Path(specification["source_path"]), self.root)
        specification_identity = _stream_identity(specification_path, self.root)
        if specification_identity != {key: specification[key] for key in ("sha256", "bytes")}:
            raise RuntimeAuthoringError("captured_specification_changed", "The captured compiler specification changed before serialization")
        specification_raw = specification_path.read_bytes()
        if {"sha256": hashlib.sha256(specification_raw).hexdigest(), "bytes": len(specification_raw)} != specification_identity:
            raise RuntimeAuthoringError("captured_specification_changed", "The captured compiler specification changed while reading it")
        parsed, _ = _specification(specification_raw, compiled["plugin_id"])
        selected = [asset for asset in parsed["assets"] if asset["path"] == compiled["selected_asset_path"]]
        if len(selected) != 1 or selected[0] != compiled["asset"]:
            raise RuntimeAuthoringError("compiled_specification_changed", "The captured compiler input no longer identifies the compiled asset")
        captured = validate_content_project(source.parent, compiled["plugin_id"])
        if captured["project_source_manifest_sha256"] != compiled["source_manifest_sha256"]:
            raise RuntimeAuthoringError("captured_source_changed", "The compiled revision's captured project changed")
        identifier = uuid.uuid4().hex
        directory = self._directory(self.builds_root / identifier)
        native_output = self._directory(directory / "native-output")
        plugin = compiled["plugin_id"]
        asset = compiled["selected_asset_path"]
        package_name = asset.rpartition(".")[0]
        params = {"class_handle": compiled["native_response"]["class"]["handle"],
                  "output_directory": str(native_output), "package_name": package_name, "plugin_id": plugin}
        receipt = {"kind": BUILD_RECEIPT_KIND, "schema": 1, "method": "runtime_blueprint",
                   "build_id": identifier, "authoring_id": authoring_id, "state": "saving",
                   "started_at": _now(), "receipt_path": str(directory / "receipt.json"),
                   "project_path": compiled["project_path"], "plugin_id": plugin,
                   "source_manifest_sha256": compiled["source_manifest_sha256"],
                   "specification_sha256": compiled["specification_sha256"],
                   "compiler_specification": specification,
                   "inputs": self._captured_inventory(source, compiled["source_inputs"]),
                   "staged_source_root": str(source), "compiled": True, "exact_source_compiled": True,
                   "persisted": False, "containerized": False, "packaged": False, "installed": False,
                   "fresh_load_verified": False, "outputs": [], "intermediate_outputs": [],
                   "compiled_asset_paths": [asset], "generated_class_paths": [asset + "_C"],
                   "native_output_directory": str(native_output), "automatic_retry": False,
                   "build_identity": {"build_id": identifier, "source_manifest_sha256": compiled["source_manifest_sha256"],
                                      "specification_sha256": compiled["specification_sha256"],
                                      "target_platform": "Win64", "configuration": "Shipping"},
                   "runtime_provenance": {"compile_receipt": compiled, "save_request": params},
                   "native_response": None}
        self._save_build(directory, receipt)
        try:
            response = self.request("runtime_save_package", params, timeout_seconds)
        except NativeError as error:
            receipt.update(state="outcome_unknown" if error.may_have_executed else "failed", finished_at=_now(),
                           error={"code": error.code, "message": str(error), "request_id": error.request_id,
                                  "may_have_executed": error.may_have_executed, "details": error.details})
            try:
                self._save_build(directory, receipt)
            except Exception as save_error:
                error.add_note("The native save failure receipt could not be persisted: " + str(save_error))
            raise
        except Exception as error:
            receipt.update(state="outcome_unknown", finished_at=_now(),
                           error={"code": "runtime_request_failed", "message": str(error), "may_have_executed": True})
            try:
                self._save_build(directory, receipt)
            except Exception as save_error:
                error.add_note("The uncertain save receipt could not be persisted: " + str(save_error))
            raise
        receipt["native_response"] = response
        try:
            if (not isinstance(response, dict) or response.get("source") != "runtime_package_serialization"
                    or response.get("serialized") is not True or response.get("persisted") is not True
                    or response.get("package_name") != package_name):
                raise RuntimeAuthoringError("runtime_save_unverified", "The native response did not identify successful serialization",
                                            may_have_executed=True)
            receipt["persisted"] = True
            for suffix in ("uasset", "uexp"):
                path = _checked(Path(response[suffix + "_path"]), native_output)
                if path.name != package_name.rsplit("/", 1)[-1] + "." + suffix:
                    raise RuntimeAuthoringError("runtime_output_identity_mismatch", "A serialized package filename differs from its asset",
                                                may_have_executed=True)
                identity = _stream_identity(path, native_output)
                if identity["bytes"] != response[suffix + "_bytes"] or identity["bytes"] <= 0:
                    raise RuntimeAuthoringError("runtime_output_identity_mismatch", "A serialized package differs from its reported extent",
                                                may_have_executed=True)
                receipt["intermediate_outputs"].append({"path": path.name, "source_path": str(path), **identity,
                                                        "role": suffix, "authoritative_runtime_output": False})
            receipt["state"] = "serialized_raw_package"
            if response.get("containerized") is True:
                outputs = response.get("outputs")
                if (not isinstance(outputs, list) or len(outputs) != 3
                        or not all(isinstance(item, dict) for item in outputs)
                        or {item.get("role") for item in outputs} != {"pak", "utoc", "ucas"}):
                    raise RuntimeAuthoringError("incomplete_runtime_containers", "The runtime writer must identify a complete container triplet",
                                                may_have_executed=True)
                target = self._directory(directory / "output" / plugin)
                descriptor = source / (plugin + ".uplugin")
                descriptor_target = target / descriptor.name
                with descriptor.open("rb") as incoming, descriptor_target.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                receipt["outputs"] = [{"path": str(descriptor_target), "relative_path": descriptor.name, "source_path": str(descriptor_target),
                                       **_stream_identity(descriptor_target, target), "role": "plugin_descriptor",
                                       "authoritative_runtime_output": True}]
                stems = set()
                for item in outputs:
                    path = _checked(Path(item["path"]), native_output)
                    if path.suffix.casefold() != "." + item["role"]:
                        raise RuntimeAuthoringError("runtime_output_identity_mismatch", "A container extension differs from its reported role",
                                                    may_have_executed=True)
                    identity = _stream_identity(path, native_output)
                    if identity["bytes"] != item["bytes"] or identity["bytes"] <= 0:
                        raise RuntimeAuthoringError("runtime_output_identity_mismatch", "A container differs from its reported extent",
                                                    may_have_executed=True)
                    stems.add(path.stem.casefold())
                    destination = target / "Content/Paks/Windows" / path.name
                    self._directory(destination.parent)
                    with path.open("rb") as incoming, destination.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    if _stream_identity(destination, target) != identity:
                        raise RuntimeAuthoringError("runtime_output_changed", "A runtime container changed while capturing its output",
                                                    may_have_executed=True)
                    receipt["outputs"].append({"path": str(destination), "relative_path": destination.relative_to(target).as_posix(),
                                              "source_path": str(destination), **identity, "role": item["role"],
                                              "authoritative_runtime_output": True})
                if len(stems) != 1:
                    raise RuntimeAuthoringError("incomplete_runtime_containers", "The container triplet must have one matching basename",
                                                may_have_executed=True)
                if validate_content_project(source.parent, plugin)["project_source_manifest_sha256"] != receipt["source_manifest_sha256"]:
                    raise RuntimeAuthoringError("captured_source_changed", "The captured compiled source changed during serialization",
                                                may_have_executed=True)
                if _stream_identity(specification_path, self.root) != specification_identity:
                    raise RuntimeAuthoringError("captured_specification_changed", "The captured compiler specification changed during serialization",
                                                may_have_executed=True)
                receipt.update(state="succeeded", containerized=True, output_root=str(target))
        except (RuntimeAuthoringError, WorkspaceError, ContentProjectError, OSError, ValueError, KeyError, TypeError) as error:
            receipt.update(state="output_unverified", error={"code": getattr(error, "code", "runtime_output_evidence_error"),
                           "message": str(error) if hasattr(error, "code") else "The runtime output could not be fully verified",
                           "may_have_executed": True})
        receipt["finished_at"] = _now()
        self._save_build(directory, receipt)
        return receipt

    def _save_build(self, directory: Path, receipt: dict[str, Any]) -> None:
        with _destination_lock(self.builds_root), _guard_directories(directory, self.root):
            _write_json(directory / "receipt.json", receipt)
            _write_json(self.builds_root / "latest.json", {"build_id": receipt["build_id"]})

    def build_status(self, build_id: str | None = None) -> dict[str, Any]:
        if build_id is None:
            pointer = self.builds_root / "latest.json"
            if not pointer.is_file():
                return {"state": "not_saved", "compiled": False, "persisted": False, "containerized": False}
            build_id = _json(_checked(pointer, self.root).read_bytes())["build_id"]
        if not isinstance(build_id, str) or len(build_id) != 32 or any(character not in "0123456789abcdef" for character in build_id):
            raise RuntimeAuthoringError("invalid_build_id", "Use an actual runtime save receipt identity")
        receipt = _json(_checked(self.builds_root / build_id / "receipt.json", self.root).read_bytes())
        if receipt.get("kind") != BUILD_RECEIPT_KIND or receipt.get("build_id") != build_id:
            raise RuntimeAuthoringError("receipt_identity_mismatch", "The runtime build receipt has a different identity")
        return receipt

    def status(self, authoring_id: str | None = None, *, working: bool = False) -> dict[str, Any]:
        if authoring_id is None:
            pointer = self.receipts_root / ("working.json" if working else "latest.json")
            if not pointer.is_file():
                return {"state": "not_compiled", "compiled": False, "persisted": False}
            authoring_id = _json(_checked(pointer, self.root).read_bytes())["authoring_id"]
        if not isinstance(authoring_id, str) or len(authoring_id) != 32 or any(
                character not in "0123456789abcdef" for character in authoring_id):
            raise RuntimeAuthoringError("invalid_authoring_id", "Use an actual runtime authoring receipt identity")
        path = _checked(self.receipts_root / authoring_id / "receipt.json", self.root)
        receipt = _json(path.read_bytes())
        if receipt.get("kind") != RECEIPT_KIND or receipt.get("authoring_id") != authoring_id:
            raise RuntimeAuthoringError("receipt_identity_mismatch", "The runtime authoring receipt has a different identity")
        return receipt

    def report(self, authoring_id: str) -> dict[str, Any]:
        receipt = self.status(authoring_id)
        return {"authoring_id": authoring_id, "state": receipt["state"],
                "source_manifest_sha256": receipt["source_manifest_sha256"],
                "specification_sha256": receipt["specification_sha256"],
                "native_response": receipt.get("native_response"), "error": receipt.get("error"),
                "compiled": receipt["compiled"], "engine_execution_accepted": False,
                "persisted": False, "packaged": False}
