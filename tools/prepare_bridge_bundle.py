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
from sn2_mcp.content_projects import ContentProjectError, verify_content_package

TOOLS = ("start_shared_mcp.ps1", "stop_shared_mcp.ps1", "game_process_common.ps1", "game_process_state.ps1", "load_native.ps1", "native_load_result.ps1", "start_sn2_development.ps1", "stop_sn2_development.ps1", "build_native.ps1", "prepare_bridge_bundle.py", "build_development_services.py", "accept_development_services.py")
FIXTURE_DIRS = ()
SOURCE_FILES = ("__init__.py", "server.py", "native_client.py", "development.py", "tool_catalog.py", "packages.py", "package_export.py", "patches.py", "sources.py", "source_functions.py", "source_identity.py", "game_environment.py", "workspace.py", "snapshots.py", "session_ownership.py", "compile_diagnostics.py", "process_observer.py", "window_input.py", "build_workflow.py", "runtime_lifecycle.py", "content_projects.py", "content_cooker.py", "content_deployment.py", "content_authoring.py", "service_mod.py")
HELPER_FILES = ()
FIXTURE_SCRIPTS = {}
DOCUMENTS = ("README.md", "pyproject.toml", "uv.lock")
BINARIES = ("build/native/Release/SN2_MCP.dll", "build/native/Release/sn2_mcp_loader.exe")
SDK_FILES = ("native/CMakeLists.txt", "native/src/entry.cpp",
             "native/src/game_access.cpp", "native/src/game_access_array.cpp",
             "native/src/game_access_containers.cpp", "native/src/game_access_struct.cpp",
             "native/src/game_access_vector.cpp", "native/src/game_access_transform.cpp",
             "native/src/game_access_object_array.cpp", "native/src/game_access_linear_color.cpp",
             "native/src/game_access_assets.cpp", "native/src/game_access_mods.cpp",
             "native/src/observations.cpp", "native/src/transport.cpp",
             "native/include/sn2_mcp/game_access.hpp", "native/include/sn2_mcp/process_identity.hpp",
             "native/include/sn2_mcp/asset_path.hpp", "native/include/sn2_mcp/blueprint_mod_record.hpp",
             "native/tools/load_bridge.cpp")
TEMPLATE_FILES = tuple("development/mod_templates/content_plugin/" + name for name in
                       ("Plugin.uplugin", "mod.json", "README.md"))
AUTHORING_FILES = tuple("development/authoring/SN2MCPAuthoring/" + name for name in
                       ("SN2MCPAuthoring.uplugin",
                        "Source/SN2MCPAuthoring/SN2MCPAuthoring.Build.cs",
                        "Source/SN2MCPAuthoring/Private/SN2MCPAuthoringCommandlet.cpp",
                        "Source/SN2MCPAuthoring/Private/SN2MCPAuthoringModule.cpp",
                        "Source/SN2MCPAuthoring/Public/SN2MCPAuthoringCommandlet.h"))
SERVICE_ID = "SN2MCPDevelopmentServices"
SERVICE_ENTRY_CLASS = "/SN2MCPDevelopmentServices/BP_DevelopmentServices.BP_DevelopmentServices_C"
SERVICE_DIRECTORY = "services/" + SERVICE_ID
SERVICE_SOURCE_FILES = tuple("development/services/" + SERVICE_ID + "/" + name for name in
    (SERVICE_ID + ".uplugin", "mod.json", "README.md", "Authoring/service.json"))
LEGACY_NATIVE_FILES = {"native/src/mod_entry.cpp", "native/tools/mod_control.cpp"}


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
    paths = (list(DOCUMENTS) + ["tools/" + name for name in TOOLS] + list(BINARIES)
             + list(HELPER_FILES) + ["src/sn2_mcp/" + name for name in SOURCE_FILES]
             + list(TEMPLATE_FILES) + list(AUTHORING_FILES) + list(SERVICE_SOURCE_FILES))

    def failure(error):
        raise BundleError("access_error", "Failed to enumerate all allowed bundle sources")

    for root, allowed_suffix in ((repository / "native", {".cpp", ".hpp", ".h", ".cmake"}),
                                 *[(repository / "tests/fixtures" / name, {".json", ".as", ".patch"}) for name in FIXTURE_DIRS]):
        _checked(root, repository, directory=True)
        for directory, directories, files in os.walk(root, followlinks=False, onerror=failure):
            parent = _checked(Path(directory), repository, directory=True)
            directories[:] = [name for name in directories if name not in {"__pycache__", "nppBackup", "build", "_deps", "CMakeFiles", "sn2_mod"}]
            for name in directories:
                _checked(parent / name, repository, directory=True)
            for name in files:
                path = _checked(parent / name, repository)
                if not path.is_file():
                    raise BundleError("unsafe_file", "The allowed area contains a non-regular file")
                if path.relative_to(repository).as_posix() in LEGACY_NATIVE_FILES:
                    continue
                if path.suffix.lower() in allowed_suffix or path.name == "CMakeLists.txt":
                    paths.append(path.relative_to(repository).as_posix())
    if len(set(paths)) != len(paths):
        raise BundleError("bundle_limit", "The bundle contains duplicate paths")
    return sorted(paths)


def _required_native_inputs(root: Path, selected: set[str]) -> set[str]:
    """Resolve local CMake sources and internal quoted/angle include closure.

    The bundle carries the declared native tests because CMake configures those
    targets even when the caller selects only the bridge target.
    Third-party includes remain normal FetchContent dependencies.
    """
    required = set(SDK_FILES) | set(TEMPLATE_FILES)
    cmake = _read(root / "native/CMakeLists.txt", root).decode("utf-8-sig")
    for relative in re.findall(r"(?<![A-Za-z0-9_])((?:src|tools|tests|include)/[A-Za-z0-9_./-]+\.(?:cpp|hpp|h|cmake))", cmake):
        required.add("native/" + relative)
    checked = set()
    pending = [path for path in required if path.endswith((".cpp", ".hpp", ".h"))]
    while pending:
        relative = pending.pop()
        if relative in checked or relative not in selected:
            continue
        checked.add(relative)
        text = _read(root / relative, root).decode("utf-8-sig")
        for delimiter, include in re.findall(r'^\s*#\s*include\s*(["<])([^">\r\n]+)[">]', text, re.M):
            normalized = include.replace("\\", "/")
            if normalized.startswith(("sn2_mcp/", "sn2_mod/")):
                target = "native/include/" + normalized
            elif delimiter == "<":
                continue
            else:
                resolved = Path(os.path.abspath(root / relative / ".." / normalized))
                if not resolved.is_relative_to(root):
                    raise BundleError("unsafe_source", "A local native include escapes the bundle")
                target = resolved.relative_to(root).as_posix()
                if target not in selected and "/" not in normalized:
                    # Nonlocal third-party include such as MinHook.h resolves
                    # through its declared dependency include directories.
                    continue
            if target not in required:
                required.add(target)
                pending.append(target)
    return required


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


def _service_package(package_root: Path | None) -> tuple[dict, dict[str, bytes]]:
    """Capture a verified cooked companion without claiming runtime acceptance."""
    if package_root is None:
        return {"state": "missing", "package_included": False,
                "runtime_validation": "not_performed_by_packager"}, {}
    package = _checked(package_root, directory=True)
    verified = verify_content_package(package)
    if (verified["plugin_id"] != SERVICE_ID
            or verified["activation"].get("entry_class") != SERVICE_ENTRY_CLASS):
        raise BundleError("service_identity_mismatch", "The selected package is not the bridge's development service mod")
    paths = {entry["path"] for entry in verified["files"]} | {"manifest.json"}
    raw_files = {SERVICE_DIRECTORY + "/" + path: _read(package / path, package) for path in sorted(paths)}
    again = verify_content_package(package)
    if again["manifest_sha256"] != verified["manifest_sha256"]:
        raise BundleError("service_package_changed", "The service package changed during capture")
    for entry in verified["files"]:
        raw = raw_files[SERVICE_DIRECTORY + "/" + entry["path"]]
        if len(raw) != entry["bytes"] or _sha(raw) != entry["sha256"]:
            raise BundleError("service_package_changed", "A service package file changed during capture")
    if _sha(raw_files[SERVICE_DIRECTORY + "/manifest.json"]) != verified["manifest_sha256"]:
        raise BundleError("service_package_changed", "The service package manifest changed during capture")
    return {"state": "included", "package_included": True, "package_verified": True,
            "package_directory": SERVICE_DIRECTORY, "plugin_id": SERVICE_ID,
            "entry_class": SERVICE_ENTRY_CLASS, "manifest_sha256": verified["manifest_sha256"],
            "source_revision": verified["source_revision"],
            "runtime_validation": "not_performed_by_packager"}, raw_files


def _verify_service(root: Path, manifest: dict, expected: set[str]) -> dict:
    declaration = manifest.get("service_mod")
    packaged = {path for path in expected if path.startswith(SERVICE_DIRECTORY + "/")}
    if declaration is None:
        if packaged:
            raise BundleError("invalid_manifest", "An included service package lacks its declaration")
        return {"state": "missing", "package_included": False,
                "runtime_validation": "not_performed_by_packager"}
    if not isinstance(declaration, dict) or declaration.get("state") not in {"missing", "included"}:
        raise BundleError("invalid_manifest", "The service package declaration is invalid")
    if declaration["state"] == "missing":
        if (packaged or declaration.get("package_included") is not False
                or declaration.get("runtime_validation") != "not_performed_by_packager"):
            raise BundleError("invalid_manifest", "A missing service declaration conflicts with bundle contents")
        return declaration
    if (declaration.get("package_included") is not True or declaration.get("package_verified") is not True
            or declaration.get("package_directory") != SERVICE_DIRECTORY
            or declaration.get("plugin_id") != SERVICE_ID or declaration.get("entry_class") != SERVICE_ENTRY_CLASS
            or declaration.get("runtime_validation") != "not_performed_by_packager"):
        raise BundleError("invalid_manifest", "The service package identity or verification declaration is invalid")
    verified = verify_content_package(root / SERVICE_DIRECTORY)
    required = {SERVICE_DIRECTORY + "/" + entry["path"] for entry in verified["files"]}
    required.add(SERVICE_DIRECTORY + "/manifest.json")
    if (packaged != required or verified["plugin_id"] != SERVICE_ID
            or verified["activation"].get("entry_class") != SERVICE_ENTRY_CLASS
            or declaration.get("manifest_sha256") != verified["manifest_sha256"]
            or declaration.get("source_revision") != verified["source_revision"]):
        raise BundleError("service_identity_mismatch", "The bundled service package differs from its declared contract")
    return declaration


def _service_source_correlation(service_mod: dict, raw_files: dict[str, bytes]) -> dict:
    """Observe selected cooked-source correspondence; this does not gate runtime use."""
    if service_mod.get("package_included") is not True:
        return {"state": "package_missing", "matches_current_sources": None, "files": []}
    packaged = json.loads(raw_files[SERVICE_DIRECTORY + "/source-manifest.json"].decode("utf-8-sig"))
    inputs = {item["path"]: item for item in packaged["inputs"]}
    prefix = "development/services/" + SERVICE_ID + "/"
    files = []
    for path in SERVICE_SOURCE_FILES:
        relative = path[len(prefix):]
        raw = raw_files[path]
        selected = inputs.get(relative)
        observed = {"path": relative, "current_source_sha256": _sha(raw), "current_source_bytes": len(raw),
                    "packaged_source_sha256": selected.get("sha256") if selected else None,
                    "packaged_source_bytes": selected.get("bytes") if selected else None}
        observed["matches"] = (observed["current_source_sha256"], observed["current_source_bytes"]) == (
            observed["packaged_source_sha256"], observed["packaged_source_bytes"])
        files.append(observed)
    matches = all(item["matches"] for item in files)
    return {"state": "matched" if matches else "different_revision", "matches_current_sources": matches,
            "method": "packaged_cook_source_inventory_vs_bundled_service_build_sources",
            "startup_gate": False, "files": files}


def _instructions(version: str, native_version: str | None = None, *, service_included: bool = False) -> bytes:
    service_status = (f"The verified cooked service package is included at `{SERVICE_DIRECTORY}`. "
                      "Use its manifest identity for installation and activation; packaging has not tested its runtime functions."
                      if service_included else
                      "The cooked development service package is missing. This folder is an incomplete development artifact, "
                      "not the complete bridge and in-game service delivery.")
    return f"""# Subnautica 2 MCP development bundle {version}

Python version: {version}. Native version: {native_version or version}.
This folder is not an installer or a public release. Hash verification does not
establish Subnautica runtime acceptance, load the DLL or launch the game.

{service_status}

The companion service is part of the bridge's runtime delivery. Custom Engine,
Modkit, FMOD and the Editor helper are build-time tools for producing that
component. Its in-game calls do not launch an external Editor. Use
`sn2_service_status`, `sn2_service_enable`, `sn2_service_call` and
`sn2_service_disable` with the installed companion. Inspect actual function
contracts before invoking. Creating a new cooked Content/Blueprint mod inside
Shipping remains a separate capability; wrappers do not establish a compiler.

1. Keep the installed game separate. Verify from this folder with
   `python -B tools/prepare_bridge_bundle.py --verify-root .`.
2. With Python 3.11+ and uv, choose an external environment using
   `UV_PROJECT_ENVIRONMENT`, then run `uv sync --locked`. Runtime dependencies
   are locked; the declared Hatchling build backend is pinned. A native
   consumer needs the Microsoft Visual C++ x64 runtime. Building requires
   Visual Studio 2022/MSVC x64, CMake 3.24+ and Git on PATH. CMake obtains the
   declared nlohmann JSON v3.12.0 and MinHook 8af6b4acae5a9388fd742b56fa79ece89d96f823
   dependencies; their source checkout identities are recorded by the build.
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

6. In a writable working copy, inspect `sn2_build_recipe` before `sn2_build`.
   The fixed native workflow captures exact source inputs and returns complete
   compiler reports, source correlation, process cleanup and artifact hashes.
   Read `sn2_build_status` and `sn2_compile_diagnostics` for recorded results.
   `tools/build_native.ps1 -Json` uses the same native build workflow.
7. The retained external build-time workflow can build the companion service
   and editable content projects. `sn2_create_mod_project` prepares an editable content-only Unreal plugin in
   the configured workspace. Inspect `sn2_authoring_recipe`, then use
   `sn2_author_mod` with a workspace JSON specification to create, compile and
   save real assets and executable Blueprint graphs. Read the complete
   `sn2_authoring_status`/`sn2_authoring_report` evidence. The service builds
   the included Editor-only helper sources against the configured custom
   engine and reuses its identified build. That helper is authoring tooling;
   it is not a native DLL mod included in a Content package. Project creation
   alone does not author a Blueprint or establish a working mod.
   Inspect `sn2_content_environment` and `sn2_cook_recipe`, then run
   `sn2_cook_mod` and read `sn2_cook_status`/`sn2_cook_report`. The compatible
   engine and Modkit are build-time dependencies, not requirements for invoking
   the cooked service and are not included in this bundle.
   Configure `--modkit-root` and `--engine-root`; defaults are
   `D:/SN2Modding/Modkit` and `D:/SN2Modding/Engine`.
8. Package a successful source-correlated cook using `sn2_package_artifact`
   and verify it with `sn2_verify_artifact_package`. The package contains the
   plugin descriptor and complete `.pak/.utoc/.ucas` containers. A source-only
   export or synthetic container fixture is not successful Unreal cooking.
   `sn2_mod_install`/`sn2_mod_uninstall` manage verified owned inventories
   separately from runtime activation; inspect `sn2_mod_deployment_status`.
   Installation and replacement require the selected stopped-game boundary.
9. Start/select the game explicitly, load/enable the exact bridge and inspect
   Tick/reflection readiness. `sn2_mod_load` validates the package/installation
   and activates its optional Blueprint entry actor. Direct `sn2_load_asset`,
   `sn2_load_class` and `sn2_blueprint_mod_enable/status/disable` support focused
   runtime investigation. Actor creation, plugin mounting and execution of
   authored behavior are separate facts; inspect actual effects independently.
   Disable uses natural actor teardown; it does not prove arbitrary effect
   reversal or unload cooked classes/packages. `sn2_mod_reload` replaces a
   cooked revision through a controlled game restart; hot package unload is
   not implemented.
10. Finish the exact test process with `sn2_development_stop` and confirm its
    exit. The frontend and game have separate lifetimes. Remove the selected
    test plugin through its verified inventory when that installation action
    is authorized.
Source tools support search/exact editing and source identity. A source ZIP
preserves bytes but is separate from compiled artifact packaging.

This bundle includes Python services, declared dependency locks, lifecycle/build
scripts, the production bridge and loader, native CMake sources/tests,
headers, the editable content-plugin template and Editor authoring helper
sources and the companion's substantive build specification. The custom engine,
Modkit, FMOD and compiled Editor helper modules are external build-time
dependencies, not bundle files. Prebuilt runtime use does
not require a compiler; native bridge and Editor helper source builds do.
Native tests retained by CMake include
donor algorithms and do not claim Gothic game services as Subnautica tools.
Gothic helper packages and game probes remain outside this bundle. PDB debug
symbols are not required by this Release bundle and no symbol availability is
claimed. Project creation, authoring, cook/package, runtime verification,
installation and publication are separate outcomes. Keep the verified bundle
as an immutable copy and use a writable working copy for builds and workspace
results so strict manifest verification remains meaningful.
""".encode("utf-8")


def prepare_bundle(destination: Path, *, repository_root: Path = ROOT, native_version: str | None = None,
                   service_package: Path | None = None) -> dict:
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
    compatibility = _compatibility(repository, native_version, *(raw_files[path] for path in BINARIES))
    compatibility_source = _sha(_read(repository / "native/src/game_access.cpp", repository))
    service_mod, service_files = _service_package(service_package)
    raw_files.update(service_files)
    service_source_correlation = _service_source_correlation(service_mod, raw_files)
    raw_files["README.md"] = _instructions(version, native_version, service_included=service_mod["package_included"])
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
                "includes": ["python_sources", "locked_dependencies", "runtime_tools", "prebuilt_native_bridge",
                             "native_build_sources_and_tests", "content_plugin_template", "editor_authoring_sources",
                             "development_service_sources", "instructions"],
                "excludes": ["virtual_environment", "game_files", "game_caches", "user_mods", "saved_games", "logs", "local_mcp_settings",
                             "custom_engine", "modkit", "compiled_editor_helper_modules"],
                "runtime_validation": "not_performed_by_packager", "installed": False,
                "service_mod": service_mod, "service_source_correlation": service_source_correlation,
                "complete_service_delivery": "not_established_by_packager"}
    if service_mod["package_included"]:
        manifest["includes"].append("cooked_development_service_package")
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
                    "runtime_validation": "not_performed_by_packager", "service_mod": verified["service_mod"],
                    "service_source_correlation": verified["service_source_correlation"],
                    "complete_service_delivery": "not_established_by_packager"}
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
    required |= set(SDK_FILES) | set(TEMPLATE_FILES) | set(AUTHORING_FILES)
    if manifest.get("service_mod") is not None:
        required |= set(SERVICE_SOURCE_FILES)
    else:
        # Older development bundles predate companion tooling and sources.
        required.discard("tools/build_development_services.py")
        required.discard("tools/accept_development_services.py")
        required.discard("src/sn2_mcp/service_mod.py")
    if not required.issubset(expected):
        raise BundleError("incomplete_bundle", "The bundle is missing a required runtime or development file")
    if not _required_native_inputs(root, expected).issubset(expected):
        raise BundleError("incomplete_bundle", "The bundle is missing a declared native build input")
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
    service_mod = _verify_service(root, manifest, expected)
    if "service_source_correlation" in manifest:
        service_raw = {path: _read(root / path, root) for path in SERVICE_SOURCE_FILES}
        if service_mod.get("package_included"):
            service_raw[SERVICE_DIRECTORY + "/source-manifest.json"] = _read(
                root / SERVICE_DIRECTORY / "source-manifest.json", root)
        correlation = _service_source_correlation(service_mod, service_raw)
        if manifest["service_source_correlation"] != correlation:
            raise BundleError("service_source_correlation_mismatch", "The service source-correlation observation differs from bundled inputs")
    else:
        correlation = {"state": "not_recorded", "matches_current_sources": None, "files": []}
    return {"version": manifest.get("version"), "native_version": native_version, "file_count": len(files), "total_bytes": total,
            "verified_files": True, "runtime_validation": "not_performed_by_packager", "service_mod": service_mod,
            "service_source_correlation": correlation,
            "complete_service_delivery": "not_established_by_packager"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare or verify a portable internal Subnautica 2 MCP bundle")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--destination", type=Path, help="A new directory within artifacts in the current Git workspace")
    group.add_argument("--verify-root", type=Path, help="Verify all unpacked bundle hashes without launching the game")
    parser.add_argument("--native-version", help="Explicit DLL version when it differs from the Python package version")
    parser.add_argument("--service-package", type=Path, help="Verified cooked SN2MCPDevelopmentServices package to include")
    args = parser.parse_args()
    try:
        if args.native_version is not None and args.destination is None:
            parser.error("--native-version requires --destination; verification reads the manifest version")
        if args.service_package is not None and args.destination is None:
            parser.error("--service-package requires --destination; verification reads the packaged service")
        result = prepare_bundle(args.destination, native_version=args.native_version,
                                service_package=args.service_package) if args.destination else verify_bundle(args.verify_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (BundleError, ContentProjectError, WorkspaceError, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"code": getattr(error, "code", "bundle_failed"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
