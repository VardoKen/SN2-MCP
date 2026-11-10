"""Prepare a safe ScriptPatches fixture without launching the game.

The official Modkit documents patches to base Script/. Application to new
sources in the same package is unconfirmed and is the subject of this probe.
"""
from __future__ import annotations

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
from sn2_mcp.workspace import (PackageWorkspace, WorkspaceError, _checked,
                               _destination_lock, _guard_directories, _read_bytes)

PACKAGE_NAME = "G1RMCPPatchProbe"
SCRIPT_RELATIVE = "Script/G1RMCPPatchProbe.as"
PATCH_RELATIVE = "ScriptPatches/Marker.patch"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/patch_probe"


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _cleanup_owned(stage: Path, workspace: Path, files: dict, directories: dict) -> None:
    """Remove only our unchanged files; preserve a suspicious tree."""
    try:
        with _guard_directories(stage, workspace):
            if _identity(stage) != directories.get("."):
                return
            # Validate each complete parent chain first: cleanup does not follow links.
            for relative, identity in directories.items():
                directory = _checked(stage if relative == "." else stage / relative, stage, directory=True)
                if _identity(directory) != identity:
                    return
            for relative in files:
                _checked((stage / relative).parent, stage, directory=True)
            for relative, (identity, digest) in files.items():
                path = stage / relative
                with _guard_directories(path.parent, workspace):
                    checked = _checked(path, stage)
                    if _identity(checked) != identity:
                        continue
                    if hashlib.sha256(_read_bytes(checked, stage, 2 * 1024 * 1024)).hexdigest() != digest:
                        continue
                    checked.unlink()
            for relative, identity in reversed(tuple(directories.items())):
                if relative == ".":
                    continue
                directory = _checked(stage / relative, stage, directory=True)
                if _identity(directory) == identity and not any(directory.iterdir()):
                    directory.rmdir()
        if _identity(_checked(stage, workspace, directory=True)) == directories.get(".") and not any(stage.iterdir()):
            stage.rmdir()
    except (WorkspaceError, OSError):
        # Preserve changed or unexpected content without hiding the original error.
        return


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    repository = _checked(repository_root, directory=True)
    artifacts = _checked(repository / "artifacts", repository, directory=True)
    workspace = _checked(workspace_root, artifacts, directory=True)
    if workspace == artifacts:
        raise PackageError("unsafe_path", "A separate existing directory within artifacts is required")
    fixture = _checked(FIXTURE_ROOT, directory=True)
    validation = validate_package(fixture, ".")
    source_files = ("GothicMod.json", SCRIPT_RELATIVE, PATCH_RELATIVE)
    if (not validation["valid_structure"] or validation["script_count"] != 1
            or validation["patch_count"] != 1 or validation["metadata"].get("name") != PACKAGE_NAME):
        raise PackageError("fixture_invalid", "The patch fixture does not match the recipe")
    contents = {name: _checked(fixture / name, fixture).read_bytes() for name in source_files}
    source = contents[SCRIPT_RELATIVE]
    diff = contents[PATCH_RELATIVE]
    if (source.count(b"return 10;") != 1 or b"+        return 11;" not in diff
            or b"a/" + SCRIPT_RELATIVE.encode() not in diff
            or not re.search(rb"UCLASS\(\)\s+class UG1RMCPPatchProbe\s*:\s*UObject\s*\{", source)
            or b"@@ -5,6 +5,6 @@ class UG1RMCPPatchProbe : UObject" not in diff):
        raise PackageError("fixture_invalid", "The patch marker or path does not match the recipe")
    target = workspace / PACKAGE_NAME
    stage: Path | None = None
    owned_files: dict = {}
    owned_directories: dict = {}
    with _destination_lock(target), _guard_directories(workspace, artifacts):
        if os.path.lexists(target):
            raise PackageError("already_exists", "The probe package already exists")
        try:
            stage = Path(tempfile.mkdtemp(prefix=".sn2-mcp-patch-probe-", dir=workspace))
            owned_directories["."] = _identity(stage)
            with _guard_directories(stage, workspace):
                for relative in ("Script", "ScriptPatches"):
                    (stage / relative).mkdir()
                    owned_directories[relative] = _identity(stage / relative)
                writer = PackageWorkspace(workspace)
                for relative, raw in contents.items():
                    with _guard_directories((stage / relative).parent, workspace):
                        result = writer.write_package_file(stage.name, relative, raw.decode("utf-8"))
                        owned_files[relative] = (_identity(_checked(stage / relative, stage)), result["sha256"])
                copied = validate_package(stage, ".")
                if not copied["valid_structure"] or copied["script_count"] != 1 or copied["patch_count"] != 1:
                    raise PackageError("fixture_invalid", "The fixture copy failed structural validation")
            if os.name != "nt":
                raise PackageError("unsupported_environment", "Atomic preparation is supported on Windows")
            stage.rename(target)
            stage = None
        finally:
            if stage is not None:
                _cleanup_owned(stage, workspace, owned_files, owned_directories)
    return {
        "package": PACKAGE_NAME, "package_path": str(target),
        "validation": copied, "verification": "not_performed",
        "files_sha256": {name: hashlib.sha256(raw).hexdigest() for name, raw in contents.items()},
        "launch_options": [f"-ModsDirectory={workspace.as_posix()}", f"-Mods={PACKAGE_NAME}"],
        "runtime_probe": {"class": "UG1RMCPPatchProbe", "object_name": "Default__G1RMCPPatchProbe",
                          "function": "ReadPatchMarker", "arguments": {},
                          "expected_signature": {"input_count": 0, "return": "IntProperty", "return_bytes": 4}},
        "interpretation": {"11": "The patch was applied to the new source in this package",
                           "10": "The patch did not change the new function body; the cause requires diagnostics",
                           "missing_class_or_failure": "Order, path, or loading is unconfirmed"},
        "limitations": ["Documentation describes patches to base Script/; handling new package sources is unconfirmed",
                        "The fixture does not change official sources or use gameplay mechanics",
                        "Preparation and a standard unified diff do not prove application or compilation by the game"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare an isolated ScriptPatches probe without launching the game")
    parser.add_argument("--workspace-root", type=Path, required=True, help="An existing separate directory within artifacts")
    arguments = parser.parse_args(argv)
    try:
        result = prepare_probe(arguments.workspace_root)
    except (PackageError, WorkspaceError, OSError) as error:
        message = str(error) if isinstance(error, (PackageError, WorkspaceError)) else "Failed to prepare the probe package"
        print(json.dumps({"code": getattr(error, "code", "access_error"), "message": message}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
