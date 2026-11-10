"""Prepare a verifiable internal bridge bundle with its layout preserved.

This is a working folder for another Windows PC, not a public release or installer.
The Python environment and game are not included; game launch and installation
are not performed. Environments, logs, caches, and local settings are excluded.
"""

from __future__ import annotations
import argparse
import ast
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_MARKER = b"SN2_MCP_DEVELOPMENT_PROBES=0"
DEVELOPMENT_MARKER = b"SN2_MCP_DEVELOPMENT_PROBES=1"
sys.path.insert(0, str(ROOT / "src"))
from sn2_mcp.workspace import WorkspaceError, _checked, _destination_lock, _guard_directories, _read_bytes

TOOLS = ("start_shared_mcp.ps1", "stop_shared_mcp.ps1", "game_process_common.ps1", "game_process_state.ps1", "load_native.ps1", "native_load_result.ps1", "prepare_bridge_bundle.py")
FIXTURE_DIRS = ()
SOURCE_FILES = ("__init__.py", "server.py", "native_client.py", "development.py", "tool_catalog.py", "packages.py", "package_export.py", "patches.py", "sources.py", "source_functions.py", "source_identity.py", "game_environment.py", "workspace.py", "snapshots.py", "session_ownership.py", "compile_diagnostics.py", "process_observer.py", "window_input.py")
HELPER_FILES = ()
FIXTURE_SCRIPTS = {}
DOCUMENTS = ("README.md", "pyproject.toml", "uv.lock")
BINARIES = ("build/native/Release/SN2_MCP.dll", "build/native/Release/sn2_mcp_loader.exe")


class BundleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _identity(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino


def _read(path: Path, boundary: Path) -> bytes:
    return _read_bytes(_checked(path, boundary), boundary, None)


def _version(repository: Path) -> str:
    source = _read(repository / "src/sn2_mcp/__init__.py", repository).decode("utf-8-sig")
    parsed = ast.parse(source)
    versions = [node.value.value for node in parsed.body
                if isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__version__"
                and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)]
    if len(versions) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", versions[0]):
        raise BundleError("invalid_version", "The bundle requires exactly one numeric __version__")
    metadata = tomllib.loads(_read(repository / "pyproject.toml", repository).decode("utf-8-sig"))
    if metadata.get("project", {}).get("version") != versions[0]:
        raise BundleError("version_mismatch", "The Python source and pyproject.toml versions differ")
    lock = tomllib.loads(_read(repository / "uv.lock", repository).decode("utf-8-sig"))
    packages = lock.get("package", [])
    local = [item for item in packages if isinstance(item, dict) and item.get("name") == "sn2-mcp"]
    sdk = [item for item in packages if isinstance(item, dict) and item.get("name") == "mcp"]
    if (len(local) != 1 or local[0].get("version") != versions[0]
            or local[0].get("source") != {"editable": "."} or len(sdk) != 1 or sdk[0].get("version") != "2.3.0"
            or metadata.get("project", {}).get("dependencies") != ["mcp==2.3.0"]):
        raise BundleError("lock_mismatch", "uv.lock does not match the version, editable root, or pinned MCP SDK")
    return versions[0]


def _compatibility(repository: Path, version: str, dll: bytes, loader: bytes) -> dict:
    source = _read(repository / "native/src/game_access.cpp", repository).decode("utf-8-sig")
    method = "runtime_image_patterns"
    if f'"{method}"' not in source or version.encode("ascii") not in dll or method.encode("ascii") not in dll:
        raise BundleError("native_version_mismatch", "The built DLL does not match the current version or runtime binding contract")
    _production_binary(dll)
    for label, raw in (("DLL", dll), ("loader", loader)):
        if len(raw) < 64 or raw[:2] != b"MZ":
            raise BundleError("invalid_binary", f"{label} is not a PE x64 binary")
        offset = struct.unpack_from("<I", raw, 60)[0]
        if offset < 64 or offset + 6 > len(raw) or raw[offset:offset + 4] != b"PE\0\0" or struct.unpack_from("<H", raw, offset + 4)[0] != 0x8664:
            raise BundleError("invalid_binary", f"{label} is not a PE x64 binary")
    return {"platform": "Windows x64", "binding_method": method,
            "supported_game_exe_name": "Subnautica2/Binaries/Win64/Subnautica2-Win64-Shipping.exe",
            "source": "native/src/game_access.cpp:bindings_source", "native_version_marker": version,
            "development_probes": False, "development_build_marker": PRODUCTION_MARKER.decode("ascii"),
            "runtime_validation": "not_performed_by_packager"}


def _production_binary(dll: bytes) -> None:
    if PRODUCTION_MARKER not in dll or DEVELOPMENT_MARKER in dll:
        raise BundleError("development_binary", "The runtime bundle requires a production DLL without development acceptance fixtures")


def _enumerate(repository: Path) -> list[str]:
    paths = list(DOCUMENTS) + ["tools/" + name for name in TOOLS] + list(BINARIES) + list(HELPER_FILES)

    def failure(error):
        raise BundleError("access_error", "Failed to enumerate all allowed bundle sources")

    for root, allowed_suffix in ((repository / "src/sn2_mcp", {".py"}),
                                 *[(repository / "tests/fixtures" / name, {".json", ".as", ".patch"}) for name in FIXTURE_DIRS]):
        _checked(root, repository, directory=True)
        for directory, directories, files in os.walk(root, followlinks=False, onerror=failure):
            parent = _checked(Path(directory), repository, directory=True)
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in directories:
                _checked(parent / name, repository, directory=True)
            for name in files:
                path = _checked(parent / name, repository)
                if not path.is_file():
                    raise BundleError("unsafe_file", "The allowed area contains a non-regular file")
                if path.suffix.lower() in allowed_suffix:
                    paths.append(path.relative_to(repository).as_posix())
    if len(set(paths)) != len(paths):
        raise BundleError("bundle_limit", "The bundle contains duplicate paths")
    return sorted(paths)


def _target(destination: Path, repository: Path) -> Path:
    artifacts = _checked(repository / "artifacts", repository, directory=True)
    target = Path(os.path.abspath(destination))
    if target == artifacts or not target.is_relative_to(artifacts):
        raise BundleError("unsafe_path", "The bundle requires a new separate directory within artifacts")
    _checked(target.parent, artifacts, directory=True)
    if os.path.lexists(target):
        raise BundleError("already_exists", "The destination directory already exists")
    return target


def _cleanup(stage: Path, parent: Path, own_files: dict, own_directories: dict) -> None:
    try:
        with _guard_directories(stage, parent):
            for relative, identity in own_directories.items():
                path = _checked(stage if relative == "." else stage / relative, stage, directory=True)
                if _identity(path) != identity:
                    return
            for relative, (identity, digest) in own_files.items():
                path = _checked(stage / relative, stage)
                with _guard_directories(path.parent, stage):
                    if _identity(path) == identity and _sha(_read(path, stage)) == digest:
                        path.unlink()
            for relative, identity in sorted(own_directories.items(), key=lambda item: item[0].count("/"), reverse=True):
                if relative == ".":
                    continue
                path = _checked(stage / relative, stage, directory=True)
                if _identity(path) == identity and not any(path.iterdir()):
                    path.rmdir()
        if _identity(_checked(stage, parent, directory=True)) == own_directories.get(".") and not any(stage.iterdir()):
            stage.rmdir()
    except (WorkspaceError, OSError):
        pass


def _file_set(root: Path) -> set[str]:
    paths = set()

    def failure(error):
        raise BundleError("copy_changed", "Failed to verify the complete bundle contents")

    for directory, directories, files in os.walk(root, followlinks=False, onerror=failure):
        parent = _checked(Path(directory), root, directory=True)
        for name in directories:
            _checked(parent / name, root, directory=True)
        for name in files:
            path = _checked(parent / name, root)
            if not path.is_file():
                raise BundleError("unsafe_file", "The bundle contains a non-regular file")
            paths.add(path.relative_to(root).as_posix())
    return paths


def _directory_set(root: Path) -> set[str]:
    directories = {"."}
    for directory, children, _ in os.walk(root, followlinks=False,
                                          onerror=lambda _: (_ for _ in ()).throw(BundleError("copy_changed", "The bundle directory is inaccessible"))):
        parent = _checked(Path(directory), root, directory=True)
        for name in children:
            path = _checked(parent / name, root, directory=True)
            directories.add(path.relative_to(root).as_posix())
    return directories


def _instructions(version: str, native_version: str | None = None) -> bytes:
    return f"""# Subnautica 2 MCP runtime bundle {version}

Python version: {version}. Native version: {native_version or version}.
This folder is not an installer or a public release. Hash verification does not
establish Subnautica runtime acceptance, load the DLL or launch the game.

1. Keep the installed game separate. Verify from this folder with
   `python -B tools/prepare_bridge_bundle.py --verify-root .`.
2. With Python 3.11+ and uv, choose an external environment using
   `UV_PROJECT_ENVIRONMENT`, then run `uv sync --locked`. Runtime dependencies
   are locked; the declared Hatchling build backend is pinned. A native
   consumer needs the Microsoft Visual C++ x64 runtime.
3. Start the frontend with the external environment's Python:
   `python -B -m sn2_mcp.server --transport streamable-http --host 127.0.0.1
   --port 27224 --native-port 27223 --workspace-root <workspace>
   --runtime-root <bundle> --game-root <installed game>`.
   Connect MCP to `http://127.0.0.1:27224/mcp`. Explicit stdio is also supported.
4. Inspect `sn2_status`, `sn2_capabilities`, and `sn2_target_processes`.
   Loading is an explicit action with exact process identity and DLL hash.
   Native status separates module/transport, discovered bindings, observed
   Tick and validated layouts. Version differences do not alone block startup.
5. Inspect objects and function signatures before invoking. Handles and
   observations are native-session values. Refresh after reconnect. Never
   automatically replay an operation with an uncertain effect.

The runtime bundle includes the inherited Python sources, declared dependencies,
selected runtime tools and native DLL/loader. Gothic helper packages and probes
remain source provenance in the development Git and are not Subnautica runtime
fixtures. Server and game lifetimes are separate; finish temporary development
processes when their work is complete. Installation and publication are separate
operator assignments. Use a writable working copy for results so the immutable
bundle continues to pass strict manifest verification.
""".encode("utf-8")


def prepare_bundle(destination: Path, *, repository_root: Path = ROOT, native_version: str | None = None) -> dict:
    repository = _checked(repository_root, directory=True)
    target = _target(destination, repository)
    version = _version(repository)
    native_version = version if native_version is None else native_version
    if not isinstance(native_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", native_version):
        raise BundleError("invalid_version", "The native version must be a complete numeric version")
    selected = _enumerate(repository)
    raw_files = {}
    for relative in selected:
        raw = _read(repository / relative, repository)
        raw_files[relative] = raw
    compatibility = _compatibility(repository, native_version, raw_files[BINARIES[0]], raw_files[BINARIES[1]])
    compatibility_source = _sha(_read(repository / "native/src/game_access.cpp", repository))
    raw_files["README.md"] = _instructions(version, native_version)
    raw_files["START.md"] = raw_files["README.md"]
    # Revalidate the source before writing; reject subsequent changes.
    if selected != _enumerate(repository) or any(_sha(_read(repository / path, repository)) != _sha(raw_files[path]) for path in selected if path != "README.md"):
        raise BundleError("source_changed", "Sources or binaries changed during preparation")
    if _sha(_read(repository / "native/src/game_access.cpp", repository)) != compatibility_source:
        raise BundleError("source_changed", "The native compatibility contract changed during preparation")
    files = [{"path": relative, "bytes": len(raw), "sha256": _sha(raw)} for relative, raw in sorted(raw_files.items())]
    manifest = {"schema_version": 1, "kind": "internal_bridge_folder", "version": version,
                "compatibility": compatibility, "files": files,
                "file_count": len(files), "total_bytes": sum(item["bytes"] for item in files),
                "includes": ["python_sources", "locked_dependencies", "runtime_tools", "prebuilt_native_bridge", "instructions"],
                "excludes": ["virtual_environment", "game_files", "game_caches", "user_mods", "saved_games", "logs", "local_mcp_settings"],
                "runtime_validation": "not_performed_by_packager", "installed": False}
    manifest["compatibility_source_sha256"] = compatibility_source
    raw_files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    stage = None
    own_files = {}
    own_directories = {}
    with _destination_lock(target), _guard_directories(target.parent, repository / "artifacts"):
        try:
            if os.path.lexists(target):
                raise BundleError("already_exists", "The bundle directory appeared before writing")
            stage = Path(tempfile.mkdtemp(prefix=".sn2-bridge-bundle-", dir=target.parent))
            own_directories["."] = _identity(stage)
            with _guard_directories(stage, target.parent), contextlib.ExitStack() as guards:
                for relative, raw in sorted(raw_files.items()):
                    file = stage / relative
                    missing = []
                    parent = file.parent
                    while not parent.exists():
                        missing.append(parent)
                        parent = parent.parent
                    for directory in reversed(missing):
                        _checked(directory.parent, stage, directory=True)
                        directory.mkdir()
                        own_directories[directory.relative_to(stage).as_posix()] = _identity(directory)
                        guards.enter_context(_guard_directories(directory, stage))
                    with _guard_directories(file.parent, stage):
                        partial = None
                        try:
                            with file.open("xb") as stream:
                                info = os.fstat(stream.fileno())
                                partial = (info.st_dev, info.st_ino)
                                stream.write(raw)
                            own_files[relative] = (_identity(_checked(file, stage)), _sha(raw))
                        except OSError:
                            if partial is not None:
                                checked = _checked(file, stage)
                                if _identity(checked) == partial and raw.startswith(_read(checked, stage)):
                                    checked.unlink()
                            raise
                if _file_set(stage) != set(raw_files) or _directory_set(stage) != set(own_directories):
                    raise BundleError("copy_changed", "The bundle contains unrelated files")
                for relative, (identity, digest) in own_files.items():
                    checked = _checked(stage / relative, stage)
                    if _identity(checked) != identity or _sha(_read(checked, stage)) != digest:
                        raise BundleError("copy_changed", "A prepared bundle file changed")
                verified = verify_bundle(stage)
            if os.path.lexists(target):
                raise BundleError("already_exists", "The bundle directory appeared before publication")
            stage.rename(target)
            stage = None
            return {"destination": str(target), "version": version, "native_version": native_version, "file_count": verified["file_count"],
                    "total_bytes": verified["total_bytes"], "manifest_sha256": _sha(raw_files["manifest.json"]),
                    "binding_method": compatibility["binding_method"],
                    "verified_files": True, "installed": False, "game_launched": False,
                    "runtime_validation": "not_performed_by_packager"}
        finally:
            if stage is not None:
                _cleanup(stage, target.parent, own_files, own_directories)


def verify_bundle(bundle_root: Path) -> dict:
    root = _checked(bundle_root, directory=True)
    raw = _read(root / "manifest.json", root)
    try:
        manifest = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeError):
        raise BundleError("invalid_manifest", "The bundle manifest is not valid JSON") from None
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("kind") != "internal_bridge_folder":
        raise BundleError("invalid_manifest", "Unsupported bundle manifest")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise BundleError("invalid_manifest", "Invalid bundle file list")
    expected = {"manifest.json"}
    expected_directories = {"."}
    total = 0
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BundleError("invalid_manifest", "Invalid file entry")
        path = entry["path"]
        if path.startswith(("/", "\\")) or "\\" in path or any(part in {"", ".", ".."} for part in path.split("/")) or ":" in path:
            raise BundleError("invalid_manifest", "A manifest path is outside the bundle")
        if path in expected:
            raise BundleError("invalid_manifest", "The manifest contains a duplicate path")
        expected.add(path)
        parent = Path(path).parent
        while parent.as_posix() != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent
        content = _read(root / path, root)
        if type(entry.get("bytes")) is not int or entry["bytes"] != len(content) or entry.get("sha256") != _sha(content):
            raise BundleError("integrity_mismatch", "A bundle file hash or size does not match")
        total += len(content)
    if (_file_set(root) != expected or _directory_set(root) != expected_directories
            or manifest.get("file_count") != len(files) or manifest.get("total_bytes") != total):
        raise BundleError("integrity_mismatch", "The complete bundle contents differ from the manifest")
    required = set(DOCUMENTS) | {"tools/" + name for name in TOOLS} | set(BINARIES) | {"START.md"}
    required |= {"src/sn2_mcp/" + name for name in SOURCE_FILES}
    required |= set(HELPER_FILES)
    required |= {f"tests/fixtures/{name}/GothicMod.json" for name in FIXTURE_DIRS}
    required |= {f"tests/fixtures/{name}/Script/{script}" for name, script in FIXTURE_SCRIPTS.items()}
    if not required.issubset(expected):
        raise BundleError("incomplete_bundle", "The bundle is missing a required runtime file")
    version = _version(root)
    compatibility = manifest.get("compatibility")
    native_version = compatibility.get("native_version_marker") if isinstance(compatibility, dict) else None
    if (manifest.get("version") != version or not isinstance(compatibility, dict)
            or not isinstance(native_version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", native_version)
            or compatibility.get("platform") != "Windows x64"):
        raise BundleError("invalid_manifest", "The bundle version or compatibility contract is invalid")
    dll = _read(root / BINARIES[0], root)
    loader = _read(root / BINARIES[1], root)
    method = compatibility.get("binding_method")
    if method == "runtime_image_patterns":
        marker = method
    elif method is None and isinstance(compatibility.get("supported_game_exe_sha256"), str) and re.fullmatch(
            r"[0-9a-f]{64}", compatibility["supported_game_exe_sha256"]):
        marker = compatibility["supported_game_exe_sha256"]
    else:
        raise BundleError("invalid_manifest", "The bundle compatibility binding method is invalid")
    if native_version.encode("ascii") not in dll or marker.encode("ascii") not in dll:
        raise BundleError("native_version_mismatch", "The DLL does not match the version or binding contract in the manifest")
    if (compatibility.get("development_probes") is not False
            or compatibility.get("development_build_marker") != PRODUCTION_MARKER.decode("ascii")):
        raise BundleError("invalid_manifest", "The bundle must declare production-only native contents")
    _production_binary(dll)
    for content in (dll, loader):
        if len(content) < 64 or content[:2] != b"MZ":
            raise BundleError("invalid_binary", "The bundle binary is not a PE x64 binary")
        offset = struct.unpack_from("<I", content, 60)[0]
        if offset < 64 or offset + 6 > len(content) or content[offset:offset + 4] != b"PE\0\0" or struct.unpack_from("<H", content, offset + 4)[0] != 0x8664:
            raise BundleError("invalid_binary", "The bundle binary is not a PE x64 binary")
    return {"version": manifest.get("version"), "native_version": native_version, "file_count": len(files), "total_bytes": total,
            "verified_files": True, "runtime_validation": "not_performed_by_packager"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or verify a portable internal Subnautica 2 MCP bundle")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--destination", type=Path, help="A new directory within artifacts in the current Git workspace")
    group.add_argument("--verify-root", type=Path, help="Verify all unpacked bundle hashes without launching the game")
    parser.add_argument("--native-version", help="Explicit DLL version when it differs from the Python package version")
    args = parser.parse_args()
    try:
        if args.native_version is not None and args.destination is None:
            parser.error("--native-version requires --destination; verification reads the manifest version")
        result = prepare_bundle(args.destination, native_version=args.native_version) if args.destination else verify_bundle(args.verify_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BundleError, WorkspaceError, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"code": getattr(error, "code", "bundle_failed"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
