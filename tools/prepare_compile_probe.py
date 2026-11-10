"""Prepare an isolated AngelScript package and recipe without launching the game.

Syntax source: official G1R-Modkit, Script/Camera/VisionSettings.as
(UCLASS/UObject) and Script/AI/States/FightAI/CombatState/CombatMoves.as
(UFUNCTION(BlueprintCallable)). This does not confirm fixture compilation.
After enabling the package, explicit reload preparation may trigger game hot reload.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
from sn2_mcp.packages import PackageError, validate_package
from sn2_mcp.workspace import PackageWorkspace, WorkspaceError, _checked, _destination_lock, _guard_directories, _read_bytes

PACKAGE_NAME = "G1RMCPProbe"
CLASS_NAME = "UG1RMCPCompileProbe"
METHOD_NAME = "ReadProbeValue"
SCRIPT_RELATIVE = "Script/G1RMCPProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "angelscript_probe"


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _file_identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _cleanup_own_stage(stage: Path, workspace: Path, files: dict, directories: dict) -> None:
    """Remove only owned unchanged files; preserve a suspicious tree."""
    try:
        with _guard_directories(stage, workspace):
            for relative, identity in directories.items():
                path = _checked(stage if relative == "." else stage / relative, stage, directory=True)
                if _file_identity(path) != identity:
                    return
            for relative in files:
                _checked((stage / relative).parent, stage, directory=True)
            for relative, (identity, digest) in files.items():
                path = stage / relative
                with _guard_directories(path.parent, workspace):
                    checked = _checked(path, stage)
                    if _file_identity(checked) == identity and _hash(_read_bytes(checked, stage, 2 * 1024 * 1024)) == digest:
                        checked.unlink()
            # The stage guard remains active; the Script guard is already closed.
            for relative, identity in reversed(tuple(directories.items())):
                if relative == ".":
                    continue
                path = _checked(stage / relative, stage, directory=True)
                if _file_identity(path) == identity and not any(path.iterdir()):
                    path.rmdir()
        checked_stage = _checked(stage, workspace, directory=True)
        if _file_identity(checked_stage) == directories.get(".") and not any(checked_stage.iterdir()):
            checked_stage.rmdir()
    except (WorkspaceError, OSError):
        pass


def _checked_artifacts_path(path: Path, repository_root: Path) -> Path:
    """Validate the absolute target within artifacts and the absence of links."""
    absolute = Path(os.path.abspath(path))
    repository = Path(os.path.abspath(repository_root))
    boundary = repository / "artifacts"
    if absolute == boundary or not absolute.is_relative_to(boundary):
        raise PackageError("unsafe_path", "Choose a separate directory within the subproject artifacts directory")
    for component in (absolute, *absolute.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise PackageError("unsafe_path", "The path contains a link or junction")
        if component.exists() and not component.is_dir():
            raise PackageError("invalid_path", "The parent path must be a directory")
    if not absolute.resolve().is_relative_to(boundary.resolve()):
        raise PackageError("unsafe_path", "The resolved path is outside artifacts")
    return absolute


def _fixture_bytes() -> tuple[bytes, bytes]:
    checked = validate_package(FIXTURE_ROOT, ".")
    if not checked["valid_structure"] or checked["script_count"] != 1:
        raise PackageError("fixture_invalid", "The probe fixture has an invalid structure")
    descriptor = (FIXTURE_ROOT / "GothicMod.json").read_bytes()
    source = (FIXTURE_ROOT / SCRIPT_RELATIVE).read_bytes()
    if checked["metadata"].get("name") != PACKAGE_NAME or source.count(b"return 1;") != 1:
        raise PackageError("fixture_invalid", "The probe fixture does not match the recipe")
    return descriptor, source


def prepare_probe(workspace_root: Path, *, phase: str = "baseline",
                  expected_sha256: str | None = None,
                  repository_root: Path = REPOSITORY_ROOT) -> dict:
    """Prepare a baseline or replace a single constant during reload.

    The operation does not open the game, load the DLL, or install the package.
    An existing baseline is not overwritten. Reload requires the exact
    baseline hash and validates both files before changing the function body.
    """
    if phase not in {"baseline", "reload"}:
        raise PackageError("invalid_phase", "Allowed phases are baseline and reload")
    if phase == "baseline" and expected_sha256 is not None:
        raise PackageError("invalid_hash", "Baseline is created without an expected hash")
    if phase == "reload" and (not isinstance(expected_sha256, str)
                               or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256)):
        raise PackageError("invalid_hash", "Reload requires the baseline SHA-256 from the preparation result")
    workspace = _checked_artifacts_path(workspace_root, repository_root)
    package = _checked_artifacts_path(workspace / PACKAGE_NAME, repository_root)
    descriptor, baseline = _fixture_bytes()
    source = baseline if phase == "baseline" else baseline.replace(b"return 1;", b"return 2;", 1)
    if phase == "baseline":
        if package.exists():
            raise PackageError("already_exists", "The probe package already exists; choose a new directory")
        workspace.mkdir(parents=True, exist_ok=True)
        _checked_artifacts_path(workspace, repository_root)
        stage = None
        owned_files = {}
        owned_directories = {}
        with _destination_lock(package), _guard_directories(workspace, Path(repository_root)):
            try:
                if package.exists():
                    raise PackageError("already_exists", "The probe package already exists")
                stage = Path(tempfile.mkdtemp(prefix=".sn2-mcp-probe-", dir=workspace))
                _checked_artifacts_path(stage, repository_root)
                owned_directories["."] = _file_identity(stage)
                with _guard_directories(stage, workspace):
                    (stage / "Script").mkdir()
                    owned_directories["Script"] = _file_identity(stage / "Script")
                    writer = PackageWorkspace(workspace)
                    with _guard_directories(stage / "Script", stage):
                        for relative, raw in (("GothicMod.json", descriptor), (SCRIPT_RELATIVE, source)):
                            result = writer.write_package_file(stage.name, relative, raw.decode("utf-8"))
                            owned_files[relative] = (_file_identity(_checked(stage / relative, stage)), result["sha256"])
                        validation = validate_package(workspace, stage.name)
                        if not validation["valid_structure"] or validation["script_count"] != 1:
                            raise PackageError("fixture_invalid", "The fixture copy failed structural validation")
                        if package.exists():
                            raise PackageError("already_exists", "The probe package appeared before preparation completed")
                    _checked(stage / "Script", workspace, directory=True)
                stage.rename(package)
                stage = None
            finally:
                if stage is not None:
                    _cleanup_own_stage(stage, workspace, owned_files, owned_directories)
    else:
        if not package.is_dir():
            raise PackageError("invalid_path", "The baseline package was not found")
        before = validate_package(workspace, PACKAGE_NAME)
        if not before["valid_structure"] or before["script_count"] != 1:
            raise PackageError("fixture_changed", "The prepared package structure changed")
        actual_files = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()}
        actual_directories = {path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_dir()}
        if actual_files != {"GothicMod.json", SCRIPT_RELATIVE} or actual_directories != {"Script"}:
            raise PackageError("fixture_changed", "The probe package contains additional files or directories")
        source_path = package / SCRIPT_RELATIVE
        current = source_path.read_bytes()
        if current != baseline or _hash(current) != expected_sha256:
            raise PackageError("file_changed", "The source differs from the baseline or expected hash")
        if (package / "GothicMod.json").read_bytes() != descriptor:
            raise PackageError("fixture_changed", "The probe package descriptor changed")
        try:
            PackageWorkspace(workspace).write_package_file(PACKAGE_NAME, SCRIPT_RELATIVE,
                                                           source.decode("utf-8"), expected_sha256)
        except WorkspaceError as error:
            raise PackageError(error.code, str(error)) from None
    verified = validate_package(workspace, PACKAGE_NAME)
    return {
        "phase": phase,
        "workspace_root": str(workspace),
        "package_root": str(package),
        "package_name": PACKAGE_NAME,
        "valid_structure": verified["valid_structure"],
        "script_count": verified["script_count"],
        "files": {
            "GothicMod.json": {"sha256": _hash(descriptor), "bytes": len(descriptor)},
            SCRIPT_RELATIVE: {"sha256": _hash(source), "bytes": len(source)},
        },
        "game_launched": False,
        "dll_loaded": False,
        "runtime_validation": "not_performed",
        "compiled": False,
        "reloaded": False,
        "launch_options": [f"-ModsDirectory={workspace.as_posix()}", f"-Mods={PACKAGE_NAME}"],
        "launch_options_effect": "Only the probe package from an external directory is selected in the current session",
        "existing_launcher": str(REPOSITORY_ROOT / "tools" / "start_sn2_development.ps1"),
        "launcher_accepts_package_options": True,
        "launcher_parameters": {"ModsDirectory": str(workspace), "Mods": [PACKAGE_NAME]},
        "expected": {
            "class_name": CLASS_NAME,
            "object_name": "Default__G1RMCPCompileProbe",
            "function_name": METHOD_NAME,
            "signature": {"input_parameters": [], "return_type": "IntProperty", "return_bytes": 4},
            "value": 1 if phase == "baseline" else 2,
        },
        "verification_recipe": [
            "Enable the package using the specified launch_options during development launch through Steam",
            "After the main menu, attach the existing DLL and confirm ready through sn2_native_status",
            "Find the exact CDO by the expected object_name and class; absence means loading is unverified",
            "Read sn2_object_functions and verify the name, absence of inputs, and an IntProperty return of size 4 before calling",
            "Call only ReadProbeValue with empty arguments; compare the result with expected.value",
            "For the explicit reload phase, replace only return 1 with return 2 using the baseline hash, then repeat signature/call verification",
        ],
        "limitations": [
            "Structural validation and a hash do not prove compilation or package loading",
            "A nonzero game exit without diagnostics does not distinguish a compiler error from another failure",
            "Structured compilation diagnostics and the debugger for the current build are unconfirmed",
            "After enabling the package, a reload write may automatically trigger hot reload",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare a probe package within artifacts without launching the game")
    parser.add_argument("--workspace-root", required=True, type=Path, help="A separate directory within the subproject artifacts directory")
    parser.add_argument("--phase", choices=("baseline", "reload"), default="baseline", help="Create a baseline or change only the function body")
    parser.add_argument("--expected-sha256", help="Baseline source SHA-256, required for reload")
    arguments = parser.parse_args(argv)
    try:
        result = prepare_probe(arguments.workspace_root, phase=arguments.phase,
                               expected_sha256=arguments.expected_sha256)
    except (PackageError, WorkspaceError, OSError) as error:
        print(json.dumps({"code": getattr(error, "code", "access_error"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
