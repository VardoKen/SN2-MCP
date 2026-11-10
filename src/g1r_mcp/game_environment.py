"""Inspect an explicitly selected game and Modkit environment without changing files.

Read only the Steam manifest, the official Modkit Script/, and the game G1R/Script/
directories; an explicit flag also selects the known game EXE. Saves, settings,
and adjacent directories are not inspected. All diagnostic errors omit
manifest contents and user paths.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re

from .source_identity import inspect as inspect_source_identity


MAX_MANIFEST_BYTES = None
MAX_MANIFEST_TOKENS = None
MAX_MANIFEST_DEPTH = None
MAX_SOURCE_FILES = None
HASH_CHUNK_BYTES = 1024 * 1024


class InspectionError(Exception):
    """Predictable error with a safe code and an English description."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class InspectionOptions:
    game_root: Path
    modkit_root: Path
    steam_manifest: Path
    hash_executable: bool = False
    max_list_entries: int | None = 100


def _invalid_manifest() -> InspectionError:
    return InspectionError("invalid_manifest", "The Steam manifest is corrupted or has an unsupported structure")


def _tokens(text: str) -> list[str | tuple[str, str]]:
    """Strictly parse ACF quotes, escapes, braces, and comments."""
    tokens: list[str | tuple[str, str]] = []
    offset = 0
    escapes = {"\\": "\\", '"': '"', "n": "\n", "r": "\r", "t": "\t"}
    while offset < len(text):
        char = text[offset]
        if char.isspace():
            offset += 1
            continue
        if text.startswith("//", offset):
            newline = text.find("\n", offset)
            offset = len(text) if newline < 0 else newline + 1
            continue
        if char in "{}":
            tokens.append(char)
            offset += 1
        elif char == '"':
            offset += 1
            value: list[str] = []
            while offset < len(text) and text[offset] != '"':
                char = text[offset]
                if char == "\\":
                    offset += 1
                    if offset >= len(text) or text[offset] not in escapes:
                        raise _invalid_manifest()
                    value.append(escapes[text[offset]])
                else:
                    if ord(char) < 32:
                        raise _invalid_manifest()
                    value.append(char)
                offset += 1
            if offset >= len(text):
                raise _invalid_manifest()
            tokens.append(("string", "".join(value)))
            offset += 1
        else:
            raise _invalid_manifest()
    return tokens


def parse_manifest(text: str) -> dict[str, str]:
    """Return only allowed metadata; exclude all other fields from the response."""
    tokens = _tokens(text.lstrip("\ufeff"))
    offset = 0
    tree: dict = {}
    stack = [tree]
    while offset < len(tokens):
        token = tokens[offset]
        if token == "}":
            if len(stack) == 1:
                raise _invalid_manifest()
            stack.pop()
            offset += 1
            continue
        if not isinstance(token, tuple):
            raise _invalid_manifest()
        result = stack[-1]
        key = token[1].casefold()
        if key in result:
            raise _invalid_manifest()
        offset += 1
        if offset >= len(tokens):
            raise _invalid_manifest()
        token = tokens[offset]
        offset += 1
        if token == "{":
            child: dict = {}
            result[key] = child
            stack.append(child)
        elif isinstance(token, tuple):
            result[key] = token[1]
        else:
            raise _invalid_manifest()
    if len(stack) != 1:
        raise _invalid_manifest()
    if set(tree) != {"appstate"} or not isinstance(tree["appstate"], dict):
        raise _invalid_manifest()
    state = tree["appstate"]
    selected: dict[str, str] = {}
    for key in ("appid", "buildid", "installdir", "lastupdated"):
        value = state.get(key)
        if value is None and key == "lastupdated":
            continue
        if not isinstance(value, str):
            raise _invalid_manifest()
        selected["LastUpdated" if key == "lastupdated" else key] = value
    for key in ("appid", "buildid", "LastUpdated"):
        if key in selected and not re.fullmatch(r"[0-9]+", selected[key]):
            raise _invalid_manifest()
    if not selected["appid"].lstrip("0") or not selected["buildid"].lstrip("0"):
        raise _invalid_manifest()
    install_dir = selected["installdir"]
    if not install_dir or install_dir in (".", "..") or any(char in install_dir for char in "/\\:\0"):
        raise _invalid_manifest()
    if any(ord(char) < 32 for char in install_dir):
        raise _invalid_manifest()
    user_config = state.get("userconfig", {})
    if not isinstance(user_config, dict):
        raise _invalid_manifest()
    beta = user_config.get("betakey")
    if beta is not None:
        if not isinstance(beta, str) or any(ord(char) < 32 for char in beta):
            raise _invalid_manifest()
        selected["betakey"] = beta
    return selected


def _safe_path(path: Path, kind: str, boundary: Path | None = None) -> Path:
    """Reject links/junctions, missing paths, and paths outside the selected root."""
    try:
        absolute = Path(os.path.abspath(path))
        for component in (absolute, *absolute.parents):
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                raise InspectionError("unsafe_path", "The path contains a symbolic link or junction")
        resolved = absolute.resolve(strict=True)
        if boundary is not None and not resolved.is_relative_to(boundary):
            raise InspectionError("unsafe_path", "The path is outside the selected root")
        if (kind == "directory" and not resolved.is_dir()) or (kind == "file" and not resolved.is_file()):
            raise InspectionError("invalid_path", "The selected path has an invalid type")
        return resolved
    except InspectionError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise InspectionError("invalid_path", "The selected path is missing or inaccessible") from None


def _hash_file(path: Path, boundary: Path) -> tuple[str, int]:
    """Stream the SHA-256 calculation and detect file changes during reading."""
    path = _safe_path(path, "file", boundary)
    try:
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(HASH_CHUNK_BYTES), b""):
                digest.update(chunk)
        _safe_path(path, "file", boundary)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise InspectionError("file_changed", "The inspected file changed during reading")
        return digest.hexdigest(), after.st_size
    except InspectionError:
        raise
    except OSError:
        raise InspectionError("read_error", "Could not read the inspected file") from None


def _source_files(root: Path) -> list[Path]:
    """Traverse only Script/ without following links."""
    result: list[Path] = []

    def fail_walk(error: OSError) -> None:
        raise InspectionError("read_error", "Could not read the script directory")

    for directory, dirs, files in os.walk(root, followlinks=False, onerror=fail_walk):
        parent = _safe_path(Path(directory), "directory", root)
        for name in dirs:
            _safe_path(parent / name, "directory", root)
        for name in files:
            if Path(name).suffix.casefold() != ".as":
                continue
            result.append(_safe_path(parent / name, "file", root))
    if not result:
        raise InspectionError("no_source_files", "No AngelScript files were found in the Modkit Script/ directory")
    return sorted(result, key=lambda file: file.relative_to(root).as_posix())


def inspect_environment(options: InspectionOptions) -> dict:
    """Inspect three explicitly selected sources and return a safe JSON result."""
    if options.max_list_entries is not None and options.max_list_entries < 0:
        raise InspectionError("invalid_arguments", "The list limit must be nonnegative")
    game_root = _safe_path(options.game_root, "directory")
    modkit_root = _safe_path(options.modkit_root, "directory")
    manifest = _safe_path(options.steam_manifest, "file")
    try:
        with manifest.open("rb") as stream:
            raw_manifest = stream.read()
        metadata = parse_manifest(raw_manifest.decode("utf-8-sig", errors="strict"))
    except InspectionError:
        raise
    except (OSError, UnicodeDecodeError):
        raise _invalid_manifest() from None
    if metadata["installdir"].casefold() != game_root.name.casefold():
        raise InspectionError("manifest_game_mismatch", "The Steam manifest belongs to another installation directory")

    source_root = _safe_path(modkit_root / "Script", "directory", modkit_root)
    game_script_root = _safe_path(game_root / "G1R" / "Script", "directory", game_root)
    sources = _source_files(source_root)
    compared = identical = different = missing = 0
    different_files: list[str] = []
    missing_files: list[str] = []
    for source in sources:
        relative = source.relative_to(source_root)
        target = game_script_root / relative
        # Check links first: a dangling symlink must not be treated as a missing path.
        absolute = Path(os.path.abspath(target))
        for component in (absolute, *absolute.parents):
            if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
                raise InspectionError("unsafe_path", "The path contains a symbolic link or junction")
        if not target.exists():
            missing += 1
            if options.max_list_entries is None or len(missing_files) < options.max_list_entries:
                missing_files.append(relative.as_posix())
            continue
        source_hash, _ = _hash_file(source, source_root)
        target_hash, _ = _hash_file(target, game_script_root)
        compared += 1
        if source_hash == target_hash:
            identical += 1
        else:
            different += 1
            if options.max_list_entries is None or len(different_files) < options.max_list_entries:
                different_files.append(relative.as_posix())

    result = {
        "ok": True,
        "manifest": metadata,
        "scripts": {
            "source_count": len(sources), "compared": compared, "identical": identical,
            "different": different, "missing": missing,
            "different_files": different_files, "missing_files": missing_files,
            "max_list_entries": options.max_list_entries,
            "lists_truncated": different > len(different_files) or missing > len(missing_files),
        },
    }
    if options.hash_executable:
        executable = game_root / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe"
        digest, size = _hash_file(executable, game_root)
        result["executable"] = {"sha256": digest, "size_bytes": size}
    return result


def verify_source_game(modkit_root: Path, game_root: Path, hash_executable: bool = False,
                       *, configured_revision: str | None = None) -> dict:
    """Compare a Modkit Script subset with installed scripts without starting the game."""
    started_at = datetime.now(timezone.utc).isoformat()
    source_identity = inspect_source_identity(modkit_root, configured_revision)
    result = inspect_environment(InspectionOptions(
        game_root=game_root,
        modkit_root=modkit_root,
        steam_manifest=game_root.parent.parent / "appmanifest_1297900.acf",
        hash_executable=hash_executable,
        max_list_entries=None,
    ))
    scripts = result["scripts"]
    result.update(
        inspection_started_at=started_at,
        inspection_finished_at=datetime.now(timezone.utc).isoformat(),
        snapshot_kind="sequential_file_reads",
        source_identity=source_identity,
        source_identity_observation="before_file_comparison",
        correlation_scope="modkit_script_subset_byte_equality",
        subset_matches_game_scripts=(scripts["different"] == 0 and scripts["missing"] == 0
                                     and scripts["compared"] == scripts["source_count"]),
        game_only_scripts_checked=False,
        runtime_compatibility_checked=False,
    )
    return result


