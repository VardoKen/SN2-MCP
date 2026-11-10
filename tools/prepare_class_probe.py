"""Prepare a class fixture within artifacts without launching the game or DLL."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from sn2_mcp.packages import PackageError, validate_package
from sn2_mcp.workspace import PackageWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories, _read_bytes
from prepare_object_probe import _workspace_path, _record_owned, _assert_owned

PACKAGE_NAME = "G1RMCPClassProbe"
SCRIPT_RELATIVE = "Script/G1RMCPClassProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "class_probe"


def _cleanup(stage: Path, workspace: Path, created: dict) -> None:
    _assert_owned(created, stage, workspace, directory=True)
    package = stage / PACKAGE_NAME
    script = package / "Script"
    descriptor = package / "GothicMod.json"
    source = package / SCRIPT_RELATIVE
    with _guard_directories(stage, workspace):
        contents = set(stage.iterdir())
        if contents not in (set(), {package}):
            raise WorkspaceError("unsafe_cleanup", "The temporary workspace contains unrelated paths; remaining files were preserved")
        if contents:
            for directory in (package, script):
                _assert_owned(created, directory, stage, directory=True)
            files = [path for path in (descriptor, source) if os.path.lexists(path)]
            if set(package.iterdir()) != {script, descriptor} or set(script.iterdir()) != ({source} if source in files else set()):
                raise WorkspaceError("unsafe_cleanup", "The temporary package contains unrelated paths; remaining files were preserved")
            for path in files:
                _assert_owned(created, path, stage)
            with _guard_directories(script, stage):
                for path in files:
                    _assert_owned(created, path, stage)
                    path.unlink()
            _assert_owned(created, script, stage, directory=True)
            script.rmdir()
            _assert_owned(created, package, stage, directory=True)
            package.rmdir()
    _assert_owned(created, stage, workspace, directory=True)
    stage.rmdir()


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    fixture = _checked(FIXTURE_ROOT, REPOSITORY_ROOT, directory=True)
    validation = validate_package(fixture, ".")
    if (not validation["valid_structure"] or validation["script_count"] != 1
            or validation["metadata"].get("name") != PACKAGE_NAME):
        raise PackageError("fixture_invalid", "The class fixture failed structural validation")
    source = _read_bytes(fixture / SCRIPT_RELATIVE, fixture, 2 * 1024 * 1024)
    workspace = _workspace_path(Path(workspace_root), Path(repository_root))
    repository = _checked(Path(repository_root), directory=True)
    target = workspace / PACKAGE_NAME
    stage = None
    created = {}
    with _destination_lock(target), _guard_directories(workspace, repository):
        if os.path.lexists(target):
            raise PackageError("already_exists", "The class fixture already exists; choose a new workspace")
        try:
            stage = Path(tempfile.mkdtemp(prefix=".g1r-class-probe-", dir=workspace))
            _record_owned(created, stage, workspace, directory=True)
            with _guard_directories(stage, workspace):
                modkit = repository.parent / "G1R-Modkit"
                writer = PackageWorkspace(stage, protected_roots=(modkit,) if modkit.is_dir() else ())
                writer.create_package(PACKAGE_NAME, validation["metadata"])
                package = stage / PACKAGE_NAME
                for directory in (package, package / "Script"):
                    _record_owned(created, directory, stage, directory=True)
                _record_owned(created, package / "GothicMod.json", stage)
                writer.write_package_file(PACKAGE_NAME, SCRIPT_RELATIVE, source.decode("utf-8"))
                _record_owned(created, package / SCRIPT_RELATIVE, stage)
                copied = validate_package(stage, PACKAGE_NAME)
                if not copied["valid_structure"] or copied["script_count"] != 1:
                    raise PackageError("fixture_invalid", "The class fixture copy failed validation")
                if os.path.lexists(target):
                    raise PackageError("already_exists", "The destination appeared before atomic completion")
                package.rename(target)
        finally:
            if stage is not None:
                failure = sys.exc_info()[1]
                try:
                    _cleanup(stage, workspace, created)
                except (WorkspaceError, OSError) as error:
                    message = "Unsafe cleanup was skipped; remaining files were preserved: " + (f"The cleanup file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))
                    if failure is None:
                        raise WorkspaceError("unsafe_cleanup", message) from error
                    failure.add_note(message)
    files = {}
    for relative in ("GothicMod.json", SCRIPT_RELATIVE):
        raw = _read_bytes(target / relative, target, 2 * 1024 * 1024)
        files[relative] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    return {
        "package_name": PACKAGE_NAME, "workspace_root": str(workspace), "package_path": str(target),
        "valid_structure": True, "script_count": 1, "files": files,
        "compiled": False, "runtime_validation": "not_performed", "game_launched": False, "dll_loaded": False,
        "startup_parameters": {"ModsDirectory": str(workspace), "Mods": [PACKAGE_NAME]},
        "expected": {"verification": "unverified_until_runtime_reflection_and_calls",
                     "host_cdo": "Default__G1RMCPClassProbe", "marker": 61,
                     "class_objects": ["G1RMCPClassBase", "G1RMCPClassDerived", "G1RMCPClassOther"],
                     "properties": [{"name": "StoredAnyClass", "source_type": "UClass", "initial_class": "UG1RMCPClassBase"},
                                    {"name": "StoredTypedClass", "source_type": "TSubclassOf<UG1RMCPClassBase>", "initial_class": "UG1RMCPClassBase"}],
                     "methods": [
                         {"name": "ReadMarker", "inputs": [], "result": "int"},
                         {"name": "ReadBaseClass", "inputs": [], "result": "UClass"},
                         {"name": "EchoAnyClass", "inputs": [{"name": "Input", "type": "UClass"}], "result": "UClass"},
                         {"name": "EchoTypedClass", "inputs": [{"name": "Input", "type": "TSubclassOf<UG1RMCPClassBase>"}], "result": "TSubclassOf<UG1RMCPClassBase>"},
                         {"name": "StoreAnyClass", "inputs": [{"name": "Input", "type": "UClass"}], "result": "void"},
                         {"name": "StoreTypedClass", "inputs": [{"name": "Input", "type": "TSubclassOf<UG1RMCPClassBase>"}], "result": "void"}]},
        "limitations": ["Preparation does not prove compilation, type application, or class loading",
                        "Calls change only the fixture fields; callbacks and game object creation are absent"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an isolated class fixture only within artifacts")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts" / "class-probe-dev6")
    args = parser.parse_args()
    try:
        result = prepare_probe(args.workspace_root)
    except (PackageError, WorkspaceError, OSError) as error:
        print(json.dumps({"code": getattr(error, "code", "access_error"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
