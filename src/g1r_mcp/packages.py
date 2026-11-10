"""Local validation of an official Gothic package without compilation or loading.

The format was checked against G1R-Modkit/Documentation/getting-started/mod-package.md.
The validator reads GothicMod.json, counts .as files in Script/, and counts .patch files directly
in ScriptPatches/. Patch, Config, Content, and Story contents are not read. Loader
operation, patch application, version compatibility, and AngelScript are not verified.
"""

import json
import os
from pathlib import Path, PureWindowsPath
import re
import stat


MAX_DESCRIPTOR_BYTES = None
MAX_SCRIPT_FILES = None
MAX_ISSUES = None
COMPONENTS = ("Script", "Config", "Content", "Story", "ScriptPatches")
LOAD_ORDERS = {"first", "early", "normal", "late", "last"}
KNOWN_FIELDS = {
    "name", "title", "author", "description", "projectWebsiteURL",
    "modVersion", "dependencies", "loadOrder",
}


class PackageError(Exception):
    """Access error or unsafe path; the message does not disclose paths."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _checked_path(path: Path, boundary: Path | None = None) -> Path:
    """Reject links, junctions, and paths outside the explicitly selected workspace."""
    try:
        absolute = Path(os.path.abspath(path))
        for part in (absolute, *absolute.parents):
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise PackageError("unsafe_path", "The path contains a symbolic link or junction")
        resolved = absolute.resolve(strict=True)
        if boundary is not None and not resolved.is_relative_to(boundary):
            raise PackageError("unsafe_path", "The path is outside the workspace")
        return resolved
    except PackageError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise PackageError("access_error", "The path is missing or inaccessible") from None


def _relative_package(workspace_root: Path, relative_path: str) -> tuple[Path, str]:
    if not isinstance(relative_path, str) or not relative_path.strip() or "\0" in relative_path:
        raise PackageError("invalid_path", "A relative package path is required")
    if Path(relative_path).is_absolute() or PureWindowsPath(relative_path).anchor or ":" in relative_path:
        raise PackageError("unsafe_path", "Absolute package paths are not allowed")
    pieces = re.split(r"[/\\]", relative_path)
    if ".." in pieces:
        raise PackageError("unsafe_path", "Traversal to a parent directory is not allowed")
    pieces = [piece for piece in pieces if piece not in ("", ".")]
    root = _checked_path(Path(workspace_root))
    if not root.is_dir():
        raise PackageError("invalid_path", "The workspace must be a directory")
    package = _checked_path(root.joinpath(*pieces), root)
    if not package.is_dir():
        raise PackageError("invalid_path", "The package must be a directory")
    return package, "/".join(pieces) or "."


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValueError("non-JSON constant")


def _read_descriptor(path: Path, boundary: Path) -> tuple[object | None, str | None]:
    checked = _checked_path(path, boundary)
    if not checked.is_file():
        return None, "descriptor_not_file"
    try:
        before = checked.stat()
        with checked.open("rb") as stream:
            raw = stream.read()
        _checked_path(checked, boundary)
        after = checked.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise PackageError("file_changed", "The descriptor changed during validation")
        return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant), None
    except PackageError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, "invalid_json"
    except OSError:
        raise PackageError("access_error", "Could not read the descriptor") from None


def _script_count(script_root: Path, boundary: Path) -> int:
    total = 0

    def walk_error(error: OSError) -> None:
        raise PackageError("access_error", "Could not read the script directory")

    for directory, dirs, files in os.walk(script_root, followlinks=False, onerror=walk_error):
        parent = _checked_path(Path(directory), boundary)
        for name in dirs:
            _checked_path(parent / name, boundary)
        for name in files:
            if Path(name).suffix.casefold() != ".as":
                continue
            file = _checked_path(parent / name, boundary)
            if not file.is_file():
                raise PackageError("invalid_path", "The script must be a regular file")
            total += 1
    return total


def _patch_count(patch_root: Path, boundary: Path) -> int:
    """Count documented top-level .patch files without analyzing diff or UTF-8."""
    total = 0
    try:
        for candidate in patch_root.iterdir():
            file = _checked_path(candidate, boundary)
            if candidate.suffix.casefold() != ".patch":
                continue
            if not file.is_file():
                raise PackageError("invalid_path", "The patch must be a regular file")
            total += 1
    except PackageError:
        raise
    except OSError:
        raise PackageError("access_error", "Could not read the patch directory") from None
    return total


def validate_package(workspace_root: Path, relative_path: str) -> dict:
    """Validate the package only within the selected workspace without making changes."""
    package, normalized = _relative_package(workspace_root, relative_path)
    result = {
        "package_path": normalized,
        "valid_json": False,
        "valid_structure": False,
        "runtime_validation": "not_performed",
        "compiled": False,
        "metadata": {},
        "components": {},
        "script_count": 0,
        "patch_count": 0,
        "patch_validation": "not_performed",
        "errors": [],
        "warnings": [],
        "issues_truncated": False,
    }
    error_count = 0

    def issue(kind: str, code: str, message: str, field: str | None = None) -> None:
        nonlocal error_count
        if kind == "errors":
            error_count += 1
        item = {"code": code, "message": message}
        if field is not None:
            item["field"] = field
        result[kind].append(item)

    issue("warnings", "runtime_not_verified", "Compilation, loading, and game version comparisons were not verified")
    for component in COMPONENTS:
        path = package / component
        # A dangling symlink must also be rejected rather than treated as a missing path.
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise PackageError("unsafe_path", "The package component contains a link or junction")
        present = path.exists()
        is_directory = False
        if present:
            checked = _checked_path(path, package)
            is_directory = checked.is_dir()
            if not is_directory:
                issue("errors", "component_not_directory", "The package component must be a directory", component)
            elif component == "Script":
                result["script_count"] = _script_count(checked, package)
            elif component == "ScriptPatches":
                result["patch_count"] = _patch_count(checked, package)
        result["components"][component] = {"present": present, "is_directory": is_directory}
    if result["script_count"] == 0 and result["patch_count"] == 0:
        issue("warnings", "no_scripts", "The package has no new AngelScript files or documented patches; it may be a content package")

    descriptor_path = package / "GothicMod.json"
    if descriptor_path.is_symlink() or (hasattr(descriptor_path, "is_junction") and descriptor_path.is_junction()):
        raise PackageError("unsafe_path", "The descriptor contains a link or junction")
    if not descriptor_path.exists():
        issue("errors", "descriptor_missing", "GothicMod.json is missing from the package root")
        return result
    descriptor, failure = _read_descriptor(descriptor_path, package)
    if failure:
        messages = {
            "descriptor_not_file": "GothicMod.json must be a regular file",
            "invalid_json": "GothicMod.json is not valid UTF-8 JSON with unique keys",
        }
        issue("errors", failure, messages[failure])
        return result
    result["valid_json"] = True
    if not isinstance(descriptor, dict):
        issue("errors", "descriptor_not_object", "The GothicMod.json root must be an object")
        return result

    for field in ("name", "title", "author", "modVersion"):
        value = descriptor.get(field)
        if not isinstance(value, str) or not value.strip():
            issue("errors", "required_string", "The required field must be a nonempty string", field)
        else:
            result["metadata"][field] = value
    for field in ("description", "projectWebsiteURL"):
        if field in descriptor:
            if not isinstance(descriptor[field], str):
                issue("errors", "invalid_field_type", "The optional field must be a string", field)
            else:
                result["metadata"][field] = descriptor[field]
    if "loadOrder" in descriptor:
        if not isinstance(descriptor["loadOrder"], str) or descriptor["loadOrder"] not in LOAD_ORDERS:
            issue("errors", "invalid_load_order", "loadOrder must be first, early, normal, late, or last", "loadOrder")
        else:
            result["metadata"]["loadOrder"] = descriptor["loadOrder"]
    for field in sorted(descriptor.keys() - KNOWN_FIELDS):
        issue("warnings", "unknown_field", "The field is not described in the verified guide; its game support is unknown", field)
    if "dependencies" in descriptor:
        dependencies = descriptor["dependencies"]
        if not isinstance(dependencies, list):
            issue("errors", "invalid_dependencies", "dependencies must be an array", "dependencies")
        else:
            valid_dependencies = []
            for index, dependency in enumerate(dependencies):
                prefix = f"dependencies[{index}]"
                if not isinstance(dependency, dict):
                    issue("errors", "invalid_dependency", "A dependency must be an object", prefix)
                    continue
                name, version = dependency.get("name"), dependency.get("requiredVersion")
                if not isinstance(name, str) or not name.strip() or not isinstance(version, str):
                    issue("errors", "invalid_dependency", "A dependency requires a nonempty name and a string requiredVersion", prefix)
                else:
                    valid_dependencies.append({"name": name, "requiredVersion": version})
                for field in sorted(dependency.keys() - {"name", "requiredVersion"}):
                    issue("warnings", "unknown_field", "The dependency field is not described in the verified guide", f"{prefix}.{field}")
            result["metadata"]["dependencies"] = valid_dependencies
    result["valid_structure"] = error_count == 0
    return result
