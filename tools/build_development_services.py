"""Build the bridge's cooked in-game companion; do not install or launch the game."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sn2_mcp.content_authoring import ContentAuthoring, ContentAuthoringError
from sn2_mcp.content_cooker import ContentCooker, ContentCookError
from sn2_mcp.content_projects import ContentProjects, ContentProjectError, verify_content_package
from sn2_mcp.workspace import SourceWorkspace, WorkspaceError, _checked, _parts, _read_bytes

SERVICE_ID = "SN2MCPDevelopmentServices"
SERVICE_ENTRY_CLASS = "/SN2MCPDevelopmentServices/BP_DevelopmentServices.BP_DevelopmentServices_C"
SERVICE_FILES = (SERVICE_ID + ".uplugin", "mod.json", "README.md", "Authoring/service.json")


def _json_write(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _source(repository: Path) -> tuple[Path, dict[str, bytes], list[dict]]:
    source = _checked(repository / "development/services" / SERVICE_ID, repository, directory=True)
    files = {name: _read_bytes(_checked(source / name, source), source, None) for name in SERVICE_FILES}
    metadata = json.loads(files["mod.json"].decode("utf-8-sig"))
    specification = json.loads(files["Authoring/service.json"].decode("utf-8-sig"))
    if (metadata.get("mod_id") != SERVICE_ID or metadata.get("plugin_name") != SERVICE_ID
            or metadata.get("entry_class") != SERVICE_ENTRY_CLASS
            or specification.get("plugin_id") != SERVICE_ID):
        raise ContentProjectError("service_identity_mismatch", "The service source does not match the bridge companion identity")
    inventory = [{"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
                 for name, raw in sorted(files.items())]
    return source, files, inventory


def build_services(*, workspace_root: Path, modkit_root: Path, engine_root: Path,
                   game_root: Path | None = None, repository_root: Path = ROOT,
                   project_path: str = "projects/" + SERVICE_ID,
                   package_path: str | None = None, timeout_seconds: float = 3600) -> dict:
    repository = _checked(repository_root, directory=True)
    workspace = SourceWorkspace(_checked(workspace_root, directory=True))
    source, source_files, inventory = _source(repository)
    parts = workspace._source_parts(project_path)
    project = workspace._unprotected(workspace.root.joinpath(*parts), forbid_ancestor=True)
    authoring = ContentAuthoring(repository, workspace_root=workspace.root,
                                modkit_root=modkit_root, engine_root=engine_root,
                                game_root=game_root, workspace=workspace)
    run_id = uuid.uuid4().hex
    reports = authoring._directory(workspace.root / "artifacts/development-services" / run_id)
    receipt_path = reports / "receipt.json"
    receipt = {"schema": 1, "kind": "sn2_development_services_build", "build_id": run_id,
               "plugin_id": SERVICE_ID, "entry_class": SERVICE_ENTRY_CLASS,
               "project_path": project_path, "source_root": str(source), "source_files": inventory,
               "receipt_path": str(receipt_path), "state": "preparing", "stages": {},
               "installed": False, "game_launched": False, "runtime_validation": "not_performed"}
    _json_write(receipt_path, receipt)
    projects = ContentProjects(workspace, repository)
    try:
        if os.path.lexists(project):
            _checked(project, workspace.root, directory=True)
            for name, raw in source_files.items():
                existing = _read_bytes(_checked(project / name, workspace.root), workspace.root, None)
                if existing != raw:
                    raise ContentProjectError("service_source_conflict",
                                              "The existing service project differs from the selected source; preserve it and select a new project path")
            receipt["project_preparation"] = "reused_identical_source"
        else:
            projects._publish(project_path, source_files, package=False)
            receipt["project_preparation"] = "copied_source"
        receipt["state"] = "authoring_service"
        _json_write(receipt_path, receipt)
        authored = authoring.author(project_path, project_path + "/Authoring/service.json", timeout_seconds)
        receipt["stages"]["authoring"] = authored
        if authored.get("state") != "succeeded" or authored.get("compiled") is not True or authored.get("published") is not True:
            raise ContentProjectError("service_authoring_failed", "Service authoring did not compile and publish the selected assets")
        receipt["state"] = "cooking_service"
        _json_write(receipt_path, receipt)
        cooker = ContentCooker(repository, workspace_root=workspace.root,
                              modkit_root=modkit_root, engine_root=engine_root,
                              game_root=game_root, workspace=workspace)
        cooked = cooker.build(project_path, timeout_seconds)
        receipt["stages"]["cook"] = cooked
        if cooked.get("state") != "succeeded" or cooked.get("exact_source_compiled") is not True:
            raise ContentProjectError("service_cook_failed", "Service cooking did not produce exact-source compiled outputs")
        receipt["state"] = "packaging_service"
        _json_write(receipt_path, receipt)
        destination = package_path or "packages/" + SERVICE_ID + "-" + run_id
        _parts(destination)
        packaged = projects.package(cooked["receipt_path"], destination)
        receipt["stages"]["package"] = packaged
        verified = verify_content_package(Path(packaged["package_root"]))
        if verified["plugin_id"] != SERVICE_ID or verified["activation"].get("entry_class") != SERVICE_ENTRY_CLASS:
            raise ContentProjectError("service_identity_mismatch", "The cooked package does not identify the bridge companion")
        if any(_read_bytes(_checked(source / name, source), source, None) != raw for name, raw in source_files.items()):
            raise ContentProjectError("service_source_changed", "The service build specification changed during preparation")
        receipt.update(state="succeeded", package_root=verified["package_root"],
                       package_manifest_sha256=verified["manifest_sha256"], source_revision=verified["source_revision"],
                       compiled=True, package_verified=True)
    except (ContentProjectError, ContentAuthoringError, ContentCookError, WorkspaceError, OSError, ValueError, KeyError) as error:
        receipt.update(state="failed", code=getattr(error, "code", "service_build_failed"), message=str(error))
        if getattr(error, "receipt", None) is not None:
            receipt["failure_receipt"] = error.receipt
    finally:
        _json_write(receipt_path, receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--modkit-root", type=Path, required=True)
    parser.add_argument("--engine-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path)
    parser.add_argument("--project-path", default="projects/" + SERVICE_ID)
    parser.add_argument("--package-path")
    parser.add_argument("--timeout-seconds", type=float, default=3600)
    args = parser.parse_args()
    try:
        receipt = build_services(**vars(args))
    except (ContentProjectError, ContentAuthoringError, ContentCookError, WorkspaceError, OSError, ValueError) as error:
        print(json.dumps({"state": "failed", "code": getattr(error, "code", "service_build_failed"),
                          "message": str(error)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0 if receipt["state"] == "succeeded" else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
