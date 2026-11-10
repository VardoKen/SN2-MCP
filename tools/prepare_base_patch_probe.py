"""Prepare a complete external Script base and an isolated ScriptPatch.

Modkit/Script and the explicitly selected Binds.Cache from the current game are read. Sources
are preserved byte for byte, then a fixture UObject with a constant marker is added. The package contains only
ScriptPatches. Patch application and compilation require game verification.
"""

from __future__ import annotations
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sn2_mcp.packages import PackageError, validate_package
from sn2_mcp.patches import ScriptPatchBuilder, PatchError
from sn2_mcp.workspace import (PackageWorkspace, WorkspaceError, _checked,
                               _destination_lock, _guard_directories, _read_bytes)

PACKAGE_NAME = "G1RMCPBasePatchProbe"
MARKER_NAME = "G1RMCPBasePatchProbe.as"
ATTACK_SOURCE_PATH = "Script/AI/States/FightAI/CombatState/CombatMoves.as"
MARKER_SOURCE = """// Isolated marker in a copy of the main Script base.
UCLASS()
class UG1RMCPBasePatchProbe : UObject
{
    UFUNCTION(BlueprintCallable)
    int ReadBasePatchMarker()
    {
        return 10;
    }
}
"""
PATCH_SOURCE = """diff --git a/Script/G1RMCPBasePatchProbe.as b/Script/G1RMCPBasePatchProbe.as
--- a/Script/G1RMCPBasePatchProbe.as
+++ b/Script/G1RMCPBasePatchProbe.as
@@ -6,5 +6,5 @@ class UG1RMCPBasePatchProbe : UObject
     int ReadBasePatchMarker()
     {
-        return 10;
+        return 11;
     }
 }
"""


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_git_patch(source_path: str, before: bytes, after: bytes) -> bytes:
    """Obtain the same Git format shown in the official instructions."""
    with tempfile.TemporaryDirectory(prefix="g1r-script-patch-") as temporary:
        directory = Path(temporary)
        target = directory / source_path
        target.parent.mkdir(parents=True)
        target.write_bytes(before)
        prefix = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                  "-c", "core.attributesfile=NUL" if os.name == "nt" else "core.attributesfile=/dev/null"]
        try:
            for arguments in (["init", "-q"], ["add", "--", source_path]):
                result = subprocess.run([*prefix, *arguments], cwd=directory, capture_output=True, timeout=10, check=False)
                if result.returncode != 0:
                    raise PackageError("patch_generation_failed", "Git failed while preparing the isolated patch")
            target.write_bytes(after)
            result = subprocess.run([*prefix, "diff", "--no-ext-diff", "--no-color", "--no-textconv", "--", source_path],
                                    cwd=directory, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise PackageError("patch_generation_failed", "Failed to obtain a bounded Git diff") from None
        raw = result.stdout
        if result.returncode != 0 or not raw.startswith(("diff --git a/" + source_path).encode("utf-8")):
            raise PackageError("patch_generation_failed", "The resulting Git diff is invalid")
        return raw


def _patch_probe(modkit: Path, target: str) -> tuple[bytes, dict]:
    if target == "own_marker":
        return PATCH_SOURCE.encode("utf-8"), {"target": target, "class_name": "UG1RMCPBasePatchProbe",
                "object_name": "Default__G1RMCPBasePatchProbe", "function_name": "ReadBasePatchMarker",
                "signature": {"input_parameters": [], "return_type": "IntProperty", "return_bytes": 4},
                "source_value": 10, "patched_value": 11}
    if target != "existing_attack":
        raise PackageError("invalid_probe_target", "Allowed targets are own_marker and existing_attack")
    builder = ScriptPatchBuilder(modkit)
    info = builder.source_info(ATTACK_SOURCE_PATH)
    raw = _read_bytes(modkit / ATTACK_SOURCE_PATH, modkit, None)
    text = raw.decode("utf-8-sig")
    expression = r"(bool ShouldInvalidateAttack\(\)\s*\{\s*)return false;(\s*\})"
    if len(re.findall(expression, text)) != 1:
        raise PackageError("source_contract_changed", "The official source does not contain exactly one expected ShouldInvalidateAttack body")
    replacement = re.sub(expression, lambda match: match[1] + "return true;" + match[2], text, count=1)
    built = builder.build_script_patch(ATTACK_SOURCE_PATH, replacement, info["source_sha256"])
    if built["hunks"] != 1:
        raise PackageError("source_contract_changed", "The probe patch must change exactly one function body")
    canonical = _canonical_git_patch(ATTACK_SOURCE_PATH, raw, replacement.encode("utf-8"))
    return canonical, {"target": target, "patch_source_format": "git_diff", "class_name": "UCM_Attack",
            "object_name": "Default__CM_Attack", "function_name": "ShouldInvalidateAttack",
            "signature": {"input_parameters": [], "return_type": "BoolProperty", "return_bytes": 1},
            "source_value": False, "patched_value": True, "source_path": ATTACK_SOURCE_PATH,
            "source_sha256": info["source_sha256"], "replacement_sha256": built["replacement_sha256"],
            "test_effect": "Only the test call return changes in the isolated Script base; gameplay progress and saved games are not used"}


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _target(path: Path, repository: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    artifacts = _checked(repository / "artifacts", repository, directory=True)
    if absolute == artifacts or not absolute.is_relative_to(artifacts):
        raise PackageError("unsafe_path", "The probe requires a separate directory within the subproject artifacts directory")
    _checked(absolute.parent, artifacts, directory=True)
    if os.path.lexists(absolute):
        raise PackageError("already_exists", "The probe directory already exists")
    return absolute


def _source_manifest(script: Path) -> list[dict]:
    files = []

    def failure(error):
        raise PackageError("access_error", "Failed to enumerate all official Script sources")

    for directory, directories, names in os.walk(script, followlinks=False, onerror=failure):
        parent = _checked(Path(directory), script, directory=True)
        for name in directories:
            _checked(parent / name, script, directory=True)
        for name in names:
            file = _checked(parent / name, script)
            if not file.is_file():
                raise PackageError("unsafe_path", "Script contains a non-regular file")
            raw = _read_bytes(file, script, None)
            if file.suffix.lower() == ".as":
                raw.decode("utf-8-sig", errors="strict")
            files.append({"path": file.relative_to(script).as_posix(), "bytes": len(raw), "sha256": _sha(raw)})
    files.sort(key=lambda item: item["path"])
    if not files or not any(item["path"].endswith(".as") for item in files):
        raise PackageError("empty_source", "The main Script base contains no .as files")
    return files


def _revision(modkit: Path) -> dict:
    try:
        revision = subprocess.run(["git", "-C", str(modkit), "rev-parse", "HEAD"], capture_output=True,
                                  encoding="utf-8", timeout=10, check=False)
        status = subprocess.run(["git", "-C", str(modkit), "status", "--porcelain=v1", "--", "Script"],
                                capture_output=True, encoding="utf-8", timeout=10, check=False)
        if revision.returncode == status.returncode == 0:
            return {"git_revision": revision.stdout.strip(), "script_worktree_clean": not status.stdout.strip()}
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        pass
    return {"git_revision": None, "script_worktree_clean": None}


def _cleanup(stage: Path, parent: Path, own_files: dict, own_directories: dict) -> None:
    """Remove only unchanged owned objects in the validated tree."""
    try:
        with _guard_directories(stage, parent):
            for relative, identity in own_directories.items():
                path = _checked(stage if relative == "." else stage / relative, stage, directory=True)
                if _identity(path) != identity:
                    return
            for relative, (identity, digest) in own_files.items():
                file = _checked(stage / relative, stage)
                with _guard_directories(file.parent, stage):
                    if _identity(file) == identity and _sha(_read_bytes(file, stage, None)) == digest:
                        file.unlink()
            for relative, identity in sorted(own_directories.items(), key=lambda item: item[0].count("/"), reverse=True):
                if relative == ".":
                    continue
                path = _checked(stage / relative, stage, directory=True)
                if _identity(path) == identity and not any(path.iterdir()):
                    path.rmdir()
        checked = _checked(stage, parent, directory=True)
        if _identity(checked) == own_directories.get(".") and not any(checked.iterdir()):
            checked.rmdir()
    except (WorkspaceError, OSError):
        pass


def _exact_destination(stage: Path, expected_files: set[str], expected_directories: set[str]) -> None:
    files = set()
    directories = {"."}

    def failure(error):
        raise PackageError("copy_changed", "Failed to validate the entire prepared tree")

    for directory, children, names in os.walk(stage, followlinks=False, onerror=failure):
        parent = _checked(Path(directory), stage, directory=True)
        for child in children:
            path = _checked(parent / child, stage, directory=True)
            directories.add(path.relative_to(stage).as_posix())
        for name in names:
            path = _checked(parent / name, stage)
            if not path.is_file():
                raise PackageError("copy_changed", "The prepared copy contains a non-regular file")
            files.add(path.relative_to(stage).as_posix())
    if files != expected_files or directories != expected_directories:
        raise PackageError("copy_changed", "The prepared copy contains unrelated files or directories")


def prepare_probe(workspace_root: Path, *, modkit_root: Path | None = None,
                  binds_cache: Path, expected_binds_sha256: str,
                  probe_target: str = "existing_attack",
                  repository_root: Path = ROOT) -> dict:
    repository = _checked(repository_root, directory=True)
    modkit = _checked(modkit_root or repository.parent / "G1R-Modkit", directory=True)
    scripts = _checked(modkit / "Script", modkit, directory=True)
    cache = _checked(binds_cache)
    if cache.name != "Binds.Cache" or not cache.is_file() or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_binds_sha256):
        raise PackageError("invalid_binds_cache", "The exact Binds.Cache file and its expected SHA-256 from the current game are required")
    cache_raw = _read_bytes(cache, cache.parent, None)
    cache_hash = _sha(cache_raw)
    if cache_hash != expected_binds_sha256.lower():
        raise PackageError("cache_changed", "The Binds.Cache SHA-256 does not match the verified game file")
    target = _target(workspace_root, repository)
    if target.is_relative_to(modkit) or modkit.is_relative_to(target):
        raise PackageError("unsafe_path", "The destination directory overlaps the official Modkit")
    parent = _checked(target.parent, repository / "artifacts", directory=True)
    manifest = _source_manifest(scripts)
    if any(item["path"].casefold() in {MARKER_NAME.casefold(), "binds.cache"} for item in manifest):
        raise PackageError("conflict", "The official base already contains the probe marker name")
    provenance = _revision(modkit)
    patch_raw, expected = _patch_probe(modkit, probe_target)
    stage = None
    own_files = {}
    own_directories = {}
    with _destination_lock(target), _guard_directories(parent, repository / "artifacts"):
        try:
            if os.path.lexists(target):
                raise PackageError("already_exists", "The probe directory appeared before writing started")
            stage = Path(tempfile.mkdtemp(prefix=".g1r-base-patch-", dir=parent))
            own_directories["."] = _identity(stage)
            with _guard_directories(stage, parent):
                for relative in ("Script", "Mods", f"Mods/{PACKAGE_NAME}", f"Mods/{PACKAGE_NAME}/ScriptPatches"):
                    path = stage / relative
                    path.mkdir()
                    own_directories[relative] = _identity(path)
                with contextlib.ExitStack() as guards:
                    guards.enter_context(_guard_directories(stage / "Script", stage))

                    def write(relative: str, raw: bytes) -> None:
                        file = stage / relative
                        missing = []
                        cursor = file.parent
                        while not cursor.exists():
                            missing.append(cursor)
                            cursor = cursor.parent
                        for directory in reversed(missing):
                            _checked(directory.parent, stage, directory=True)
                            directory.mkdir()
                            own_directories[directory.relative_to(stage).as_posix()] = _identity(directory)
                            guards.enter_context(_guard_directories(directory, stage))
                        with _guard_directories(file.parent, stage):
                            partial_identity = None
                            try:
                                with file.open("xb") as stream:
                                    info = os.fstat(stream.fileno())
                                    partial_identity = (info.st_dev, info.st_ino)
                                    stream.write(raw)
                                own_files[relative] = (_identity(_checked(file, stage)), _sha(raw))
                            except OSError:
                                if partial_identity is not None:
                                    checked = _checked(file, stage)
                                    current = _read_bytes(checked, stage, None)
                                    if _identity(checked) == partial_identity and raw.startswith(current):
                                        checked.unlink()
                                raise

                    for entry in manifest:
                        raw = _read_bytes(scripts / entry["path"], scripts, None)
                        if _sha(raw) != entry["sha256"]:
                            raise PackageError("source_changed", "Official Script sources changed while copying")
                        write("Script/" + entry["path"], raw)
                    write("Script/Binds.Cache", cache_raw)
                    write("Script/" + MARKER_NAME, MARKER_SOURCE.encode("utf-8"))
                    descriptor = {"name": PACKAGE_NAME, "title": "Main Script Base Patch Probe", "author": "G1R MCP",
                                  "description": "An isolated patch to a fixture UObject marker in an external Script copy.", "modVersion": "1.0.0"}
                    # Only these two files belong to the enabled official package.
                    writer = PackageWorkspace(stage / "Mods")
                    for relative, raw in (("GothicMod.json", json.dumps(descriptor, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"),
                                          ("ScriptPatches/Marker.patch", patch_raw)):
                        result = writer.write_package_file(PACKAGE_NAME, relative, raw.decode("utf-8"))
                        own_files[f"Mods/{PACKAGE_NAME}/{relative}"] = (_identity(stage / "Mods" / PACKAGE_NAME / relative), result["sha256"])
                    if _source_manifest(scripts) != manifest:
                        raise PackageError("source_changed", "The official Script contents changed while copying")
                    if _sha(_read_bytes(cache, cache.parent, None)) != cache_hash:
                        raise PackageError("cache_changed", "Binds.Cache changed during preparation")
                    for relative, (identity, digest) in own_files.items():
                        if _identity(_checked(stage / relative, stage)) != identity or _sha(_read_bytes(stage / relative, stage, None)) != digest:
                            raise PackageError("copy_changed", "The prepared copy changed before completion")
                    _exact_destination(stage, set(own_files), set(own_directories))
                    package = validate_package(stage / "Mods", PACKAGE_NAME)
                    if not package["valid_structure"] or package["script_count"] != 0 or package.get("patch_count") != 1:
                        raise PackageError("fixture_invalid", "The package must contain exactly one ScriptPatch")
                source_manifest_hash = _sha(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                recipe = {"source_root": str(scripts), "source_file_count": len(manifest), "source_bytes": sum(item["bytes"] for item in manifest),
                          "source_manifest_sha256": source_manifest_hash, "source_provenance": provenance,
                          "binds_cache": {"source": str(cache), "bytes": len(cache_raw), "sha256": cache_hash, "copied_to": str(target / "Script" / "Binds.Cache")},
                          "source_files": manifest, "marker_sha256": _sha(MARKER_SOURCE.encode("utf-8")),
                          "patch_sha256": _sha(patch_raw), "game_launched": False, "compiled": False, "patch_applied": False,
                          "script_directory": str(target / "Script"), "mods_directory": str(target / "Mods"), "package_name": PACKAGE_NAME,
                          "expected": expected,
                          "control": {"object_name": "Default__G1RMCPBasePatchProbe", "function_name": "ReadBasePatchMarker", "value": 10},
                          "launch_options": [f"-ScriptDirectory={(target / 'Script').as_posix()}", f"-ModsDirectory={(target / 'Mods').as_posix()}", f"-Mods={PACKAGE_NAME}"],
                          "limitations": ["A complete copy and its SHA-256 do not prove compilation or patch application", "Mapping a/Script/ to the overridden main base requires game verification"]}
                raw = json.dumps(recipe, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
                write("source-manifest.json", raw)
                _exact_destination(stage, set(own_files), set(own_directories))
                if _sha(_read_bytes(stage / "source-manifest.json", stage, None)) != _sha(raw):
                    raise PackageError("copy_changed", "The copy manifest changed before publication")
            if os.path.lexists(target):
                raise PackageError("already_exists", "The probe directory appeared before publishing the copy")
            stage.rename(target)
            stage = None
            recipe.pop("source_files")
            recipe["source_manifest_file"] = str(target / "source-manifest.json")
            return recipe
        finally:
            if stage is not None:
                _cleanup(stage, parent, own_files, own_directories)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare a complete external Script base and a package with an isolated patch")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--modkit-root", type=Path)
    parser.add_argument("--binds-cache", type=Path, required=True, help="Binds.Cache from Script in the currently installed game")
    parser.add_argument("--expected-binds-sha256", required=True, help="Verified Binds.Cache SHA-256")
    parser.add_argument("--probe-target", choices=("existing_attack", "own_marker"), default="existing_attack",
                        help="Patch an existing main-base method or the fixture marker")
    args = parser.parse_args()
    try:
        print(json.dumps(prepare_probe(args.workspace_root, modkit_root=args.modkit_root,
                                      binds_cache=args.binds_cache, expected_binds_sha256=args.expected_binds_sha256,
                                      probe_target=args.probe_target), ensure_ascii=False, indent=2))
        return 0
    except (PackageError, WorkspaceError, PatchError, OSError, UnicodeError) as error:
        print(json.dumps({"code": getattr(error, "code", "preparation_failed"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
