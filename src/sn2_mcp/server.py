"""Subnautica 2 runtime MCP frontend adapted from the G1R implementation."""

import argparse
import asyncio
import json
import hashlib
import logging
import math
import time
from pathlib import Path
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import StrictBool, StrictInt, StrictStr

from . import __version__
from .native_client import NativeClient, NativeError
from .snapshots import SnapshotError, compare_snapshots
from .development import DevelopmentError, RuntimeService
from .tool_catalog import build_tool_contracts
from .workspace import SourceWorkspace, WorkspaceError
from .source_identity import inspect as inspect_source_identity
from .package_export import ExportError, export_sources, verify_source_export
from .build_workflow import BuildError, BuildWorkflow
from .runtime_lifecycle import RuntimeLifecycle
from .content_projects import ContentProjects, ContentProjectError
from .content_cooker import ContentCooker, ContentCookError
from .content_deployment import ContentDeployment
from .content_authoring import ContentAuthoring, ContentAuthoringError
from .service_mod import ServiceMod, ServiceModError
from .runtime_authoring import RuntimeAuthoring, RuntimeAuthoringError


def _tool_failure(error: SnapshotError | DevelopmentError | WorkspaceError | ExportError) -> ToolError:
    return ToolError(json.dumps({"code": error.code, "message": str(error)}, ensure_ascii=False))

class _EnglishOperatingSystemLog(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info:
            display = traceback.TracebackException(*record.exc_info)

            def normalize(shown: traceback.TracebackException, error: BaseException) -> None:
                if isinstance(error, OSError) and type(error).__module__ == "builtins":
                    shown._str = f"Operating system error (errno={error.errno}, winerror={getattr(error, 'winerror', None)})"
                if shown.__cause__ is not None:
                    normalize(shown.__cause__, error.__cause__)
                elif shown.__context__ is not None and not shown.__suppress_context__:
                    normalize(shown.__context__, error.__context__)
                if shown.exceptions is not None:
                    for child, original in zip(shown.exceptions, error.exceptions):
                        normalize(child, original)

            normalize(display, record.exc_info[1])
            record.exc_text = "".join(display.format())
        return True


def create_server(workspace_root: Path, native_port: int = 27223,
                  game_root: Path | None = None, *, transport: str = "stdio",
                  runtime_root: Path | None = None, modkit_root: Path | None = None,
                  engine_root: Path | None = None) -> MCPServer:
    """Create the inherited runtime surface without Gothic package services."""
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("Transport must be stdio or streamable-http")
    workspace_root = workspace_root.absolute()
    if not workspace_root.is_dir():
        raise ValueError("Workspace directory does not exist")
    configured_runtime_root = runtime_root.resolve() if runtime_root is not None else Path(__file__).resolve().parents[2]
    configured_game_root = (game_root or Path(r"E:\Games\Subnautica 2")).absolute()
    configured_modkit_root = (modkit_root or Path(r"D:\SN2Modding\Modkit")).absolute()
    configured_engine_root = (engine_root or Path(r"D:\SN2Modding\Engine")).absolute()
    native = NativeClient(port=native_port)
    native_lock = threading.Lock()
    service_lock = threading.Lock()
    runtime = None
    selected_target = None
    sources = None
    builds = None
    mods = None
    cooker = None
    authoring = None
    runtime_authoring = None
    deployment = None

    def mod_projects() -> ContentProjects:
        nonlocal mods
        if mods is None:
            mods = ContentProjects(source_workspace(), configured_runtime_root)
        return mods

    def mod_project_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(mod_projects(), operation)(*args, **kwargs)
        except (ContentProjectError, WorkspaceError) as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": getattr(error, "receipt", None)}, ensure_ascii=False)) from None

    def cook_service() -> ContentCooker:
        nonlocal cooker
        if cooker is None:
            cooker = ContentCooker(configured_runtime_root, workspace_root=workspace_root,
                modkit_root=configured_modkit_root, engine_root=configured_engine_root,
                game_root=configured_game_root, workspace=source_workspace())
        return cooker

    def cook_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(cook_service(), operation)(*args, **kwargs)
        except (ContentCookError, WorkspaceError) as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": getattr(error, "receipt", None)}, ensure_ascii=False)) from None

    def authoring_service() -> ContentAuthoring:
        nonlocal authoring
        if authoring is None:
            authoring = ContentAuthoring(configured_runtime_root, workspace_root=workspace_root,
                modkit_root=configured_modkit_root, engine_root=configured_engine_root,
                game_root=configured_game_root, workspace=source_workspace())
        return authoring

    def authoring_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(authoring_service(), operation)(*args, **kwargs)
        except (ContentAuthoringError, ContentProjectError, BuildError, WorkspaceError) as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": getattr(error, "receipt", None)}, ensure_ascii=False)) from None

    def runtime_authoring_service() -> RuntimeAuthoring:
        nonlocal runtime_authoring
        if runtime_authoring is None:
            runtime_authoring = RuntimeAuthoring(workspace_root, runtime_authoring_request,
                                                workspace=source_workspace())
        return runtime_authoring

    def runtime_authoring_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(runtime_authoring_service(), operation)(*args, **kwargs)
        except NativeError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "may_have_executed": error.may_have_executed, "request_id": error.request_id,
                "effect": error.details}, ensure_ascii=False)) from None
        except (RuntimeAuthoringError, ContentProjectError, WorkspaceError) as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": getattr(error, "receipt", None), "effect": getattr(error, "details", None)},
                ensure_ascii=False)) from None

    def deployment_service() -> ContentDeployment:
        nonlocal deployment
        if deployment is None:
            deployment = ContentDeployment(runtime_service(), verifier=mod_projects().verify,
                central_root=configured_runtime_root.parent)
        return deployment

    def deployment_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(deployment_service(), operation)(*args, **kwargs)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None

    def build_service() -> BuildWorkflow:
        nonlocal builds
        with service_lock:
            if builds is None:
                builds = BuildWorkflow(configured_runtime_root, workspace_root=workspace_root)
            return builds

    def build_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        if operation in {"recipe", "build"} and kwargs.get("project_path", "native") != "native":
            raise ToolError(json.dumps({"code": "unsupported_project",
                "message": "Native builds support the repository native bridge project; use sn2_cook_mod for Content/Blueprint mods",
                "receipt": None}))
        try:
            return getattr(build_service(), operation)(*args, **kwargs)
        except BuildError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "receipt": error.receipt}, ensure_ascii=False)) from None

    def source_workspace() -> SourceWorkspace:
        nonlocal sources
        with service_lock:
            if sources is None:
                protected = (configured_game_root,) if configured_game_root.is_dir() else ()
                sources = SourceWorkspace(workspace_root, protected_roots=protected)
            return sources

    def source_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(source_workspace(), operation)(*args, **kwargs)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    def runtime_service() -> RuntimeService:
        nonlocal runtime
        with service_lock:
            if runtime is None:
                runtime = RuntimeService(repository_root=configured_runtime_root, game_root=configured_game_root)
            return runtime

    @asynccontextmanager
    async def server_lifespan(_server: MCPServer):
        try:
            yield
        finally:
            try:
                native.close()
            finally:
                with service_lock:
                    if runtime is not None:
                        runtime.close()

    server = MCPServer("Subnautica 2 MCP", version=__version__, log_level="WARNING", lifespan=server_lifespan,
        instructions=("Research game interfaces and develop, prototype and debug mods using the G1R-based native bridge. "
                      "Source tools operate on the configured workspace without assuming game source or a mod format. "
                      "Begin with sn2_status and sn2_capabilities. "
                      "Inspect sn2_target_processes before selecting an exact target. DLL loading, transport, "
                      "discovery and observed Tick are distinct states. "
                      "Use sn2_service_status and sn2_service_call for the cooked in-game development service. "
                      "Read current object/function metadata "
                      "before an explicit sn2_invoke. Handles and observations belong to a native session; "
                      "refresh after reconnect. Investigate uncertain effects; never automatically replay commands."))
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    source_write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    @server.tool(annotations=read_only)
    def sn2_build_recipe(target: str = "all", configuration: str = "Release",
                         run_tests: StrictBool = True, project_path: str = "native") -> dict[str, Any]:
        """Identify the fixed CMake/MSVC build, source inputs and toolchain without compiling."""
        return build_call("recipe", target=target, configuration=configuration, run_tests=run_tests,
                          project_path=project_path)

    @server.tool(annotations=source_write)
    def sn2_build(target: str = "all", configuration: str = "Release",
                  run_tests: StrictBool = True, timeout_seconds: float = 900,
                  parallel: StrictInt | None = None, project_path: str = "native") -> dict[str, Any]:
        """Build captured native inputs and return complete stage, diagnostic and artifact receipts."""
        return build_call("build", target=target, configuration=configuration, run_tests=run_tests,
                          timeout_seconds=timeout_seconds, parallel=parallel, project_path=project_path)

    @server.tool(annotations=read_only)
    def sn2_build_status(build_id: str | None = None) -> dict[str, Any]:
        """Read an actual recorded build receipt; no resident worker is implied."""
        return build_call("status", build_id=build_id)

    @server.tool(annotations=read_only)
    def sn2_compile_diagnostics(build_id: str | None = None) -> dict[str, Any]:
        """Read complete recorded MSVC/CMake diagnostics correlated to captured source bytes."""
        return build_call("diagnostics", build_id=build_id)

    @server.tool(annotations=source_write)
    def sn2_create_mod_project(project_path: str, mod_id: str, title: str | None = None,
                               entry_class: str | None = None) -> dict[str, Any]:
        """Create an editable content-only plugin; Blueprint/assets still require actual authoring and cooking."""
        return mod_project_call("create", project_path, mod_id, title, entry_class)

    @server.tool(annotations=read_only)
    def sn2_validate_mod_project(project_path: str) -> dict[str, Any]:
        """Validate content plugin descriptor, authored inputs and generated-class identity; do not compile."""
        return mod_project_call("validate", project_path)

    @server.tool(annotations=source_write)
    def sn2_package_artifact(cook_receipt_path: str, destination: str) -> dict[str, Any]:
        """Package a successful exact cook with descriptor, complete containers and source/activation identities."""
        return mod_project_call("package", cook_receipt_path, destination)

    @server.tool(annotations=read_only)
    def sn2_verify_artifact_package(package_path: str) -> dict[str, Any]:
        """Verify content package bytes, container pairs and exact cook provenance without installation."""
        return mod_project_call("verify", package_path)

    @server.tool(annotations=read_only)
    def sn2_authoring_recipe(project_path: str, spec_path: str) -> dict[str, Any]:
        """Identify captured editable-asset inputs, JSON graph specification and Editor-only helper without execution."""
        return authoring_call("recipe", project_path, spec_path)

    @server.tool(annotations=source_write)
    def sn2_author_mod(project_path: str, spec_path: str, timeout_seconds: float = 3600) -> dict[str, Any]:
        """Compile and save specified editable Blueprint/data assets; publish only after success and source-revision checks."""
        return authoring_call("author", project_path, spec_path, timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_authoring_status(authoring_id: str | None = None) -> dict[str, Any]:
        """Read captured authoring, compilation, output publication and process-completion receipts."""
        return authoring_call("status", authoring_id)

    @server.tool(annotations=read_only)
    def sn2_authoring_report(authoring_id: str, stage: str = "authoring", stream: str = "stdout") -> dict[str, Any]:
        """Read complete recorded authoring stdout/stderr or structured asset/graph diagnostics with exact byte identity."""
        return authoring_call("read_report", authoring_id, stage, stream)

    @server.tool(annotations=read_only)
    def sn2_runtime_authoring_recipe(project_path: str, spec_path: str, asset_path: str | None = None) -> dict[str, Any]:
        """Capture one declared Blueprint revision for native runtime compilation without running an Editor or game operation."""
        return runtime_authoring_call("recipe", project_path, spec_path, asset_path)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_runtime_compile_blueprint(project_path: str, spec_path: str, asset_path: str | None = None,
                                      timeout_seconds: float | None = None) -> dict[str, Any]:
        """Compile captured graph definitions into a unique live Unreal class; persistence and actual execution remain separate."""
        return runtime_authoring_call("compile", project_path, spec_path, asset_path, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_runtime_save_package(authoring_id: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Serialize a confirmed live revision and verify native package/container outputs; no external cooker or installation runs."""
        return runtime_authoring_call("save", authoring_id, timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_runtime_authoring_status(authoring_id: str | None = None, working: bool = False) -> dict[str, Any]:
        """Read exact runtime compile receipts or the preceding verified working candidate without native calls."""
        return runtime_authoring_call("status", authoring_id, working=working)

    @server.tool(annotations=read_only)
    def sn2_runtime_authoring_report(authoring_id: str) -> dict[str, Any]:
        """Read complete captured native compile diagnostics, identities and uncertainty for one runtime revision."""
        return runtime_authoring_call("report", authoring_id)

    @server.tool(annotations=read_only)
    def sn2_runtime_build_status(build_id: str | None = None) -> dict[str, Any]:
        """Read native serialization and verified container receipts without claiming installation or fresh loading."""
        return runtime_authoring_call("build_status", build_id)

    @server.tool(annotations=source_write)
    def sn2_package_runtime_artifact(runtime_build_receipt: str, destination: str) -> dict[str, Any]:
        """Publish a verified complete runtime Content/Blueprint package from its own producer receipt, without cooking or installation."""
        return mod_project_call("runtime_package", runtime_build_receipt, destination)

    @server.tool(annotations=read_only)
    def sn2_content_environment(project_path: str | None = None) -> dict[str, Any]:
        """Inspect configured Modkit/custom engine prerequisites; presence does not prove cooker compatibility."""
        return cook_call("inspect", project_path)

    @server.tool(annotations=read_only)
    def sn2_cook_recipe(project_path: str) -> dict[str, Any]:
        """Identify the exact content project and fixed Modkit/UAT cook contract without executing it."""
        return cook_call("recipe", project_path)

    @server.tool(annotations=source_write)
    def sn2_cook_mod(project_path: str, timeout_seconds: float = 3600) -> dict[str, Any]:
        """Capture inputs and run content/Blueprint cooking; persist failures or blocked prerequisites, never install."""
        return cook_call("build", project_path, timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_cook_status(build_id: str | None = None) -> dict[str, Any]:
        """Read an exact content cook receipt and full recorded reports; no resident worker is implied."""
        return cook_call("status", build_id)

    @server.tool(annotations=read_only)
    def sn2_cook_report(build_id: str, stage: str = "cook", stream: str = "stdout") -> dict[str, Any]:
        """Read a complete recorded cooker report with its byte/hash correlation and no invented graph locations."""
        return cook_call("read_report", build_id, stage, stream)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_install(package_path: str, expected_manifest_sha256: str,
                        expected_installed_inventory_sha256: str | None = None) -> dict[str, Any]:
        """Install or replace a verified content plugin while the game is stopped; discovery/activity remain unchecked."""
        return deployment_call("install", package_path, expected_manifest_sha256,
            expected_installed_inventory_sha256=expected_installed_inventory_sha256)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_uninstall(plugin_id: str, expected_installed_inventory_sha256: str) -> dict[str, Any]:
        """Remove only the identified installed plugin inventory at the stopped-game boundary."""
        return deployment_call("uninstall", plugin_id, expected_installed_inventory_sha256)

    @server.tool(annotations=read_only)
    def sn2_mod_deployment_status(plugin_id: str | None = None) -> dict[str, Any]:
        """Inspect installed file inventory independently of runtime plugin mount, asset or Blueprint activity."""
        return deployment_call("status", plugin_id)

    def watch_owner(ctx: Context | None) -> str:
        return "shared_native_connection"

    def native_request(operation: str, params: dict[str, Any],
                       timeout_seconds: float | None = None, *,
                       target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            with native_lock:
                client = native
                target = target_process if target_process is not None else selected_target
                target = None if target is None else dict(target)
            if target is not None:
                params = {**params, "expected_process_id": target["pid"],
                          "expected_creation_filetime": target["creation_time"]}
            return client.request(operation, params) if timeout_seconds is None else client.request(operation, params, timeout=timeout_seconds)
        except NativeError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "may_have_executed": error.may_have_executed, "request_id": error.request_id,
                "effect": error.details}, ensure_ascii=False)) from None

    def runtime_authoring_request(operation: str, params: dict[str, Any], timeout: float | None) -> dict[str, Any]:
        with native_lock:
            client = native
            target = None if selected_target is None else dict(selected_target)
        if target is not None:
            params = {**params, "expected_process_id": target["pid"],
                      "expected_creation_filetime": target["creation_time"]}
        return client.request(operation, params) if timeout is None else client.request(operation, params, timeout=timeout)

    def select_target(target_process: dict[str, Any]) -> dict[str, Any]:
        nonlocal selected_target
        try:
            target = runtime_service().validate_target(target_process)
        except DevelopmentError as error:
            raise _tool_failure(error) from None
        with native_lock:
            selected_target = target
        return target

    def invalidate_native() -> None:
        nonlocal selected_target, native
        with native_lock:
            native.close()
            native = NativeClient(port=native_port)
            selected_target = None

    def lifecycle_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(RuntimeLifecycle(runtime_service()), operation)(*args, **kwargs)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None

    def terminating_lifecycle_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            result = getattr(RuntimeLifecycle(runtime_service()), operation)(*args, **kwargs)
        except DevelopmentError as error:
            receipt = error.receipt if isinstance(error.receipt, dict) else {}
            # Validation/snapshot failures and a known not-started command leave
            # the current connection, handles and watches intact. A completed
            # stop or uncertain command must invalidate the previous session.
            if error.outcome_unknown or receipt.get("stop_receipt") or receipt.get("cleanup"):
                invalidate_native()
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt}, ensure_ascii=False)) from None
        invalidate_native()
        return result

    def checked_mod_package(package_path: str, expected_manifest_sha256: str | None) -> dict[str, Any]:
        package = mod_project_call("verify", package_path)
        if expected_manifest_sha256 is not None and package["manifest_sha256"] != expected_manifest_sha256:
            raise ToolError(json.dumps({"code": "package_changed", "message": "The verified package manifest differs from the selected revision"}))
        return package

    def blueprint_request(operation: str, params: dict[str, Any], timeout_seconds: float | None,
                          target_process: dict[str, Any] | None) -> dict[str, Any]:
        target = select_target(target_process) if target_process is not None else None
        return native_request(operation, params, timeout_seconds, target_process=target)

    def validate_spawn_transform(value: dict[str, Any] | None) -> None:
        if value is None:
            return
        parts = {"Rotation": {"X", "Y", "Z", "W"}, "Translation": {"X", "Y", "Z"},
                 "Scale3D": {"X", "Y", "Z"}}
        if not isinstance(value, dict) or set(value) != set(parts):
            raise ToolError(json.dumps({"code": "invalid_argument",
                "message": "spawn_transform requires only Rotation, Translation and Scale3D"}))
        for name, keys in parts.items():
            values = value[name]
            if not isinstance(values, dict) or set(values) != keys:
                raise ToolError(json.dumps({"code": "invalid_argument",
                    "message": "spawn_transform components must match the exact Transform shape"}))
            for number in values.values():
                try:
                    valid = type(number) in (int, float) and math.isfinite(number)
                except OverflowError:
                    valid = False
                if not valid:
                    raise ToolError(json.dumps({"code": "invalid_argument",
                        "message": "spawn_transform components must be finite Unreal double values"}))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_load_asset(asset_path: str, timeout_seconds: float | None = None,
                       target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Synchronously load an exact top-level Unreal asset on Tick; return its actual descriptor or missing result."""
        return blueprint_request("load_asset", {"asset_path": asset_path}, timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_load_class(asset_path: str, timeout_seconds: float | None = None,
                       target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load an exact generated-class asset through validated SoftClass binding; this does not activate a mod."""
        return blueprint_request("load_class", {"asset_path": asset_path}, timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_blueprint_mod_enable(mod_id: str, entry_class_path: str, package_revision: str,
                                 source_revision: str | None = None,
                                 spawn_transform: dict[str, Any] | None = None,
                                 timeout_seconds: float | None = None,
                                 target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Track/load/spawn a Blueprint entry actor through its natural lifecycle; labels do not prove effects or package bytes."""
        validate_spawn_transform(spawn_transform)
        params = {"mod_id": mod_id, "entry_class_path": entry_class_path, "package_revision": package_revision}
        if source_revision is not None:
            params["source_revision"] = source_revision
        if spawn_transform is not None:
            params["spawn_transform"] = spawn_transform
        return blueprint_request("blueprint_mod_enable", params, timeout_seconds, target_process)

    @server.tool(annotations=read_only)
    def sn2_blueprint_mod_status(mod_id: str | None = None, timeout_seconds: float | None = None,
                                 target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inspect retained Blueprint actor/world identities and observed lifecycle; authored effects require independent checks."""
        return blueprint_request("blueprint_mod_status", {} if mod_id is None else {"mod_id": mod_id},
                                 timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_blueprint_mod_disable(mod_id: str, timeout_seconds: float | None = None,
                                  target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Disable the recorded actor through validated destruction; package/class unload and authored cleanup are separate facts."""
        return blueprint_request("blueprint_mod_disable", {"mod_id": mod_id}, timeout_seconds, target_process)

    def service_mod_call(operation: str, *args, **kwargs) -> dict[str, Any]:
        try:
            return getattr(ServiceMod(blueprint_request), operation)(*args, **kwargs)
        except ServiceModError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                                        "details": error.details}, ensure_ascii=False)) from None

    @server.tool(annotations=read_only)
    def sn2_service_status(timeout_seconds: float | None = None,
                           target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Inspect the live cooked development-service actor and all reflected own functions; does not build or launch Editor."""
        return service_mod_call("status", timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_service_enable(package_revision: str, source_revision: str | None = None,
                           timeout_seconds: float | None = None,
                           target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Load and activate the cooked in-game development service; revision labels do not verify package bytes or install files."""
        return service_mod_call("enable", package_revision, source_revision, timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_service_disable(timeout_seconds: float | None = None,
                            target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Destroy the retained development-service actor through its natural lifecycle; loaded packages remain resident."""
        return service_mod_call("disable", timeout_seconds, target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_service_call(function: str, arguments: dict[str, Any] | None = None,
                         timeout_seconds: float | None = None,
                         target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Call an exact reflected own function of the live in-game service; revalidate its signature and never retry unknown effects."""
        return service_mod_call("call", function, arguments, timeout_seconds, target_process)

    def package_runtime_call(operation: str, target_process: dict[str, Any], package_path: str,
                             expected_manifest_sha256: str | None, timeout_seconds: float,
                             spawn_transform: dict[str, Any] | None = None) -> dict[str, Any]:
        package = checked_mod_package(package_path, expected_manifest_sha256)
        installation = deployment_call("status", package["plugin_id"])
        if operation == "enable":
            receipt = installation.get("receipt") or {}
            if (not installation.get("identified_installation")
                    or receipt.get("package_manifest_sha256") != package["manifest_sha256"]):
                raise ToolError(json.dumps({"code": "package_installation_unconfirmed",
                    "message": "Load requires the identified installed inventory of the selected content package",
                    "installation": installation}))
            entry_class = package["activation"].get("entry_class")
            if not entry_class:
                raise ToolError(json.dumps({"code": "blueprint_entry_absent",
                    "message": "The content package has no Blueprint entry class; inspect its data assets through sn2_load_asset"}))
            observed = sn2_blueprint_mod_enable(package["plugin_id"], entry_class, package["manifest_sha256"],
                package["source_revision"], spawn_transform, timeout_seconds, target_process)
        elif operation == "status":
            observed = sn2_blueprint_mod_status(package["plugin_id"], timeout_seconds, target_process)
        else:
            observed = sn2_blueprint_mod_disable(package["plugin_id"], timeout_seconds, target_process)
        return {"package": package, "installation": installation, "blueprint": observed,
                "package_revision_role": "verified_file_identity_and_runtime_correlation_label",
                "plugin_discovery": "not_checked_by_this_composition", "plugin_mount": "not_checked_by_this_composition",
                "authored_effect_verification": "requires_independent_runtime_observation"}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_mod_load(target_process: dict[str, Any], package_path: str,
                      expected_manifest_sha256: str | None = None,
                      timeout_seconds: float = 30.0,
                      spawn_transform: dict[str, Any] | None = None) -> dict[str, Any]:
        """Verify the installed content package and activate its Blueprint entry; inspect separate lifecycle/effect evidence."""
        return package_runtime_call("enable", target_process, package_path, expected_manifest_sha256,
                                    timeout_seconds, spawn_transform)

    @server.tool(annotations=read_only)
    def sn2_mod_status(target_process: dict[str, Any], package_path: str,
                       expected_manifest_sha256: str | None = None,
                       timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Compare selected package/file inventory with actual Blueprint actor state without claiming mount or authored effects."""
        return package_runtime_call("status", target_process, package_path, expected_manifest_sha256, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_disable(target_process: dict[str, Any], package_path: str,
                        expected_manifest_sha256: str | None = None,
                        timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Disable the selected package's recorded entry actor; installed files and cached assets remain separately reported."""
        return package_runtime_call("disable", target_process, package_path, expected_manifest_sha256, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_mod_reload(target_process: dict[str, Any], package_path: str,
                       bridge_dll_path: str, expected_bridge_sha256: str,
                       expected_manifest_sha256: str | None = None,
                       expected_installed_inventory_sha256: str | None = None,
                       discovery_timeout_seconds: float = 30.0,
                       readiness_timeout_seconds: float = 30.0,
                       timeout_seconds: float = 30.0,
                       spawn_transform: dict[str, Any] | None = None) -> dict[str, Any]:
        """Replace verified content at a confirmed restart boundary, load the fresh bridge and activate the Blueprint entry."""
        for value in (readiness_timeout_seconds, discovery_timeout_seconds, timeout_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ToolError(json.dumps({"code": "invalid_timeout", "message": "All lifecycle timeouts must be positive and finite"}))
        validate_spawn_transform(spawn_transform)
        package = checked_mod_package(package_path, expected_manifest_sha256)
        if not package["activation"].get("entry_class"):
            raise ToolError(json.dumps({"code": "blueprint_entry_absent", "message": "This iteration requires a Blueprint entry class"}))
        try:
            bridge = Path(bridge_dll_path).resolve(strict=True)
            if not bridge.is_relative_to(configured_runtime_root) or hashlib.sha256(bridge.read_bytes()).hexdigest() != expected_bridge_sha256:
                raise ValueError("bridge_changed")
        except (OSError, ValueError):
            raise ToolError(json.dumps({"code": "bridge_changed", "message": "Select an existing workspace bridge DLL with the expected SHA-256"})) from None
        installation = deployment_call("status", package["plugin_id"])
        previous_install = installation.get("receipt")
        if previous_install and previous_install.get("state") not in {"installed", "removed", "failed_without_installation"}:
            raise ToolError(json.dumps({"code": "deployment_unresolved",
                "message": "Inspect the unresolved content deployment before restarting for replacement",
                "outcome_unknown": previous_install.get("outcome_unknown", True), "receipt": previous_install}))
        if not installation.get("installed_files") and (
                (previous_install and previous_install.get("state") == "installed")
                or expected_installed_inventory_sha256 is not None):
            raise ToolError(json.dumps({"code": "installation_changed",
                "message": "The selected installed inventory is missing; inspect it before restarting",
                "installation": installation}))
        if installation.get("installed_files") and (
                not installation.get("identified_installation")
                or installation.get("installed_inventory_sha256") != expected_installed_inventory_sha256):
            raise ToolError(json.dumps({"code": "installation_changed",
                "message": "Select the identified current installed inventory before restarting for replacement",
                "installation": installation}))
        progress = {"previous_process": target_process, "stage": "stop"}
        try:
            progress["stop"] = sn2_development_stop(target_process)
            progress["stage"] = "install"
            progress["installation"] = sn2_mod_install(package_path, package["manifest_sha256"],
                expected_installed_inventory_sha256)
            progress["stage"] = "start"
            start = sn2_development_start(discovery_timeout_seconds)
            process = start["process"]
            progress.update(start=start, process=process, stage="bridge_load")
            bridge_load = sn2_native_load(process, str(bridge), expected_bridge_sha256)
            progress.update(bridge_load=bridge_load, stage="transport")
            deadline = time.monotonic() + readiness_timeout_seconds
            while True:
                try:
                    status = sn2_native_status(process)
                    break
                except ToolError as error:
                    detail = json.loads(str(error))
                    if detail.get("code") != "native_unavailable" or time.monotonic() >= deadline:
                        raise
                    time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            progress.update(native_status=status, stage="enable")
            enabled = sn2_native_enable(process, status["session_id"])
            progress.update(native_status=enabled, stage="readiness")
            while not enabled.get("ready", False) and time.monotonic() < deadline:
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                enabled = sn2_native_status(process)
                progress["native_status"] = enabled
            if not enabled.get("ready", False):
                raise ToolError(json.dumps({"code": "bridge_readiness_unconfirmed", "message": "The fresh bridge did not establish Tick/reflection readiness"}))
            progress["stage"] = "blueprint_activation"
            mod = sn2_mod_load(process, package_path, package["manifest_sha256"], timeout_seconds, spawn_transform)
        except ToolError as error:
            try:
                detail = json.loads(str(error))
            except (TypeError, ValueError):
                detail = {"code": "revision_iteration_unconfirmed", "message": str(error)}
            detail["iteration_receipt"] = progress
            raise ToolError(json.dumps(detail, ensure_ascii=False)) from None
        return {"state": "revision_activation_requested", "iteration": "controlled_process_restart",
                "hot_reload": False, "process": process, "previous_process": target_process,
                "stop": progress["stop"], "installation": progress["installation"], "start": start,
                "bridge_load": bridge_load, "native_status": enabled, "mod": mod}

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_development_start(discovery_timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Launch Subnautica and identify the fresh Shipping process; loading and readiness remain separate."""
        return lifecycle_call("start", discovery_timeout_seconds=discovery_timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_development_stop(target_process: dict[str, Any]) -> dict[str, Any]:
        """Stop the exact selected game instance and confirm process cleanup."""
        return terminating_lifecycle_call("stop", target_process)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True))
    def sn2_development_restart(target_process: dict[str, Any],
                                discovery_timeout_seconds: float = 30.0) -> dict[str, Any]:
        """Confirm the selected instance stopped, then launch and identify a fresh instance."""
        return terminating_lifecycle_call("restart", target_process,
                                          discovery_timeout_seconds=discovery_timeout_seconds)

    @server.tool(annotations=read_only)
    def sn2_status() -> dict[str, Any]:
        """Report local frontend configuration; this does not probe the game."""
        return {"server": {"name": "Subnautica 2 MCP", "version": __version__, "transport": transport, "ready": True},
                "workspace": {"root": str(workspace_root), "deployment_state": "not_checked",
                    "installed_game_deployment_known": False},
                "runtime_root": str(configured_runtime_root), "game_root": str(configured_game_root),
                "authoring_dependencies": {"modkit_root": str(configured_modkit_root),
                    "engine_root": str(configured_engine_root), "checked": False},
                "native": {"endpoint": f"127.0.0.1:{native_port}", "connection_checked": False},
                "target_process": selected_target, "game_ready": False, "game_ready_known": False}

    @server.tool(annotations=read_only)
    def sn2_source_identity(relative_path: str = ".", configured_revision: str | None = None) -> dict[str, Any]:
        """Read workspace Git facts; a revision label does not establish game compatibility."""
        try:
            root = source_workspace()._selected(relative_path)
            return {"source": "workspace_source", **inspect_source_identity(root, configured_revision)}
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_list_source_files(relative_path: str = ".", limit: StrictInt = 100, offset: StrictInt = 0) -> dict[str, Any]:
        """List workspace files, including binary entries; omit repository internals and build outputs."""
        return source_call("list_files", relative_path, limit, offset)

    @server.tool(annotations=read_only)
    def sn2_find_source_files(query: str, relative_path: str = ".", limit: StrictInt = 50, offset: StrictInt = 0) -> dict[str, Any]:
        """Find literal workspace paths without assuming an AngelScript or game source tree."""
        return source_call("search_files", query, relative_path, limit, offset)

    @server.tool(annotations=read_only)
    def sn2_search_source(query: str, relative_path: str = ".", limit: StrictInt = 50,
                          case_sensitive: StrictBool = False, offset: StrictInt = 0) -> dict[str, Any]:
        """Find literal UTF-8 source lines and report files that could not be decoded or read."""
        return source_call("search_text", query, relative_path, limit, case_sensitive, offset)

    @server.tool(annotations=read_only)
    def sn2_read_source(file_path: str, start_line: StrictInt = 1, line_count: StrictInt = 100) -> dict[str, Any]:
        """Read display lines with the complete source byte hash; no declaration parser is implied."""
        return source_call("read_file", file_path, start_line, line_count)

    @server.tool(annotations=read_only)
    def sn2_read_source_exact(file_path: str) -> dict[str, Any]:
        """Read complete UTF-8 source preserving BOM, line endings and EOF for exact revision."""
        return source_call("read_file_exact", file_path)

    @server.tool(annotations=source_write)
    def sn2_write_source(file_path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
        """Create source or replace bytes matching their SHA-256; no compilation or game write occurs."""
        return source_call("write_file", file_path, content, expected_sha256)

    @server.tool(annotations=source_write)
    def sn2_create_source_directory(directory_path: str) -> dict[str, Any]:
        """Create a selected workspace directory without inventing a game mod descriptor or entry point."""
        return source_call("create_directory", directory_path)

    @server.tool(annotations=source_write)
    def sn2_export_sources(source_files: list[str], destination: str) -> dict[str, Any]:
        """Export exact selected source bytes to a ZIP and hash manifest; this is not an installable mod."""
        try:
            return export_sources(source_workspace(), source_files, destination)
        except (WorkspaceError, ExportError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_verify_source_export(relative_path: str) -> dict[str, Any]:
        """Verify a workspace source ZIP and inventory without compiling, installing or loading it."""
        try:
            from .workspace import _parts, _checked
            parts = _parts(relative_path)
            if len(parts) < 2 or parts[0] != "artifacts":
                raise WorkspaceError("unsafe_path", "Select an export under workspace artifacts")
            root = source_workspace()._unprotected(_checked(workspace_root.joinpath(*parts), workspace_root, directory=True))
            return verify_source_export(root)
        except (WorkspaceError, ExportError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def sn2_development_recipe(source_files: list[str], targets: list[dict[str, Any]] | None = None,
                               timeout_seconds: float | None = None) -> dict[str, Any]:
        """Relate exact workspace revisions to current reflected interfaces and runtime diagnostics.

        Each optional target has a current handle and explicit property/function
        names. This records evidence; it never builds, loads or invokes a mod.
        """
        if not source_files or len(set(source_files)) != len(source_files):
            raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select nonempty unique source paths"}))
        selected = []
        for path in source_files:
            value = source_call("read_file_exact", path)
            selected.append({key: value[key] for key in ("path", "sha256", "bytes")})
        interfaces = []
        initial_runtime_state = sn2_native_status() if targets is not None else None
        if targets is not None:
            if not targets:
                raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select nonempty runtime targets"}))
            for target in targets:
                if (not isinstance(target, dict) or set(target) - {"handle", "properties", "functions"}
                        or not isinstance(target.get("handle"), str) or not target["handle"]
                        or not (target.get("properties") or target.get("functions"))):
                    raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Select a handle and explicit property or function names"}))
                for key in ("properties", "functions"):
                    names = target.get(key, [])
                    if (not isinstance(names, list) or any(not isinstance(name, str) or not name for name in names)
                            or len(set(names)) != len(names)):
                        raise ToolError(json.dumps({"code": "invalid_arguments", "message": "Interface names must be unique nonempty strings"}))
            for target in targets:
                captured = {"handle": target["handle"], "functions": []}
                if target.get("properties"):
                    captured["properties"] = native_request("properties", {
                        "handle": target["handle"], "property_names": target["properties"],
                        "limit": len(target["properties"])}, timeout_seconds)
                for name in target.get("functions", []):
                    page = 0
                    found = []
                    while True:
                        params = {"handle": target["handle"], "function_query": name, "limit": 100}
                        if page:
                            params["offset"] = page
                        value = native_request("functions", params, timeout_seconds)
                        found.extend(item for item in value.get("items", []) if item.get("name") == name)
                        continuation = value.get("next_offset")
                        if continuation is None:
                            break
                        if type(continuation) is not int or continuation <= page:
                            raise ToolError(json.dumps({"code": "invalid_continuation", "message": "The function page has no forward continuation"}))
                        page = continuation
                    captured["functions"].append({"requested_name": name, "matches": found,
                        "status": "found" if found else "missing"})
                interfaces.append(captured)
        after = [source_call("read_file_exact", item["path"])["sha256"] for item in selected]
        source_stable = after == [item["sha256"] for item in selected]
        identity = sn2_source_identity()
        runtime_state = sn2_native_status() if targets is not None else None
        runtime_stable = None
        if targets is not None:
            identity_keys = ("session_id", "process_id", "process_creation_filetime")
            initial_world = initial_runtime_state.get("world")
            final_world = runtime_state.get("world")
            identity_known = all(state.get(key) is not None
                for state in (initial_runtime_state, runtime_state) for key in identity_keys)
            world_known = all(isinstance(world, dict) and world.get("state") == "observed"
                and type(world.get("generation")) is int for world in (initial_world, final_world))
            if identity_known and world_known:
                runtime_stable = (all(initial_runtime_state.get(key) == runtime_state.get(key)
                    for key in (*identity_keys, "reflection_generation")) and initial_world == final_world)
        inventory = json.dumps(selected, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return {"kind": "mod_development_evidence", "source_files": selected,
            "source_inventory_sha256": hashlib.sha256(inventory).hexdigest(), "source_stable": source_stable,
            "source_identity": identity, "interfaces": interfaces, "native_status": runtime_state,
            "initial_native_status": initial_runtime_state, "runtime_identity_stable": runtime_stable,
            "snapshot_kind": "sequential_source_and_runtime_reads", "compiled": False,
            "mod_loaded": False, "mod_execution_checked": False,
            "next_steps": ["Inspect captured signatures and unsupported types", "Prototype explicitly with sn2_invoke",
                           "Compare sn2_snapshot results and inspect sn2_watch events", "Revise source with its exact SHA-256"],
            "limits": ["Workspace text is not shipped game source", "Reflection is not a C++ header or compiler",
                       "This evidence is not an installable mod or a source-level debugger"]}

    @server.tool(annotations=read_only)
    async def sn2_capabilities(target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return registered contracts and distinguish source support from target verification."""
        observed = None
        if target_process is not None:
            select_target(target_process)
            observed = sn2_native_status()
        tools = build_tool_contracts(await server.list_tools())
        return {"catalog_revision": __version__, "tool_contracts": tools,
                "target_process": selected_target, "native_status": observed,
                "capabilities": {"frontend": {"state": "verified", "source": "registered_mcp_tools"},
                    "target_abi": {"state": "unverified" if observed is None else observed.get("abi_state", "unverified"),
                                   "reason": "per_shape_runtime_receipts_required"}},
                "accepted_types": {} if observed is None else observed.get("accepted_types", {}),
                "automatic_retries": False}

    @server.tool(annotations=read_only)
    def sn2_target_processes(executable: str | None = None) -> dict[str, Any]:
        """Enumerate selected Subnautica processes without launching or choosing an instance."""
        try:
            return runtime_service().process_state(executable)
        except DevelopmentError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
    def sn2_native_load(target_process: dict[str, Any], dll_path: str, expected_dll_sha256: str) -> dict[str, Any]:
        """Load the selected DLL; preserve unknown outcomes and never imply executor readiness."""
        target = select_target(target_process)
        try:
            return runtime_service().load_native(target, dll_path, expected_dll_sha256)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                "outcome_unknown": error.outcome_unknown, "receipt": error.receipt,
                "do_not_retry": error.outcome_unknown}, ensure_ascii=False)) from None

    @server.tool(annotations=read_only)
    def sn2_native_status(target_process: dict[str, Any] | None = None) -> dict[str, Any]:
        """Read native handshake/discovery/Tick state independently from executor readiness."""
        if target_process is not None:
            target = select_target(target_process)
        else:
            with native_lock:
                target = None if selected_target is None else dict(selected_target)
        result = native_request("status", {}, target_process=target)
        if target is not None and (result.get("process_id") != target["pid"]
                or str(result.get("process_creation_filetime")) != target["creation_time"]):
            raise ToolError(json.dumps({"code": "native_process_mismatch", "message": "The native connection belongs to another process"}))
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def sn2_native_enable(target_process: dict[str, Any], expected_native_session_id: str) -> dict[str, Any]:
        """Enable the target Tick instrumentation in the selected loaded native session."""
        target = select_target(target_process)
        state = native_request("status", {})
        if (state.get("process_id") != target["pid"]
                or str(state.get("process_creation_filetime")) != target["creation_time"]
                or state.get("session_id") != expected_native_session_id):
            raise ToolError(json.dumps({"code": "native_identity_mismatch", "message": "The native process or session changed"}))
        return native_request("enable_game", {"expected_process_id": target["pid"], "expected_session_id": expected_native_session_id})

    @server.tool(annotations=read_only)
    def sn2_find_objects(query: str = "", class_query: str = "", limit: int = 50,
                         exact_path: str = "", timeout_seconds: float | None = None,
                         offset: StrictInt = 0) -> dict[str, Any]:
        """Find UObject by partial name/class on game Tick; return opaque handles."""
        params = {"query": query, "class_query": class_query, "limit": limit}
        if offset:
            params["offset"] = offset
        if exact_path:
            params["exact_path"] = exact_path
        return native_request("objects", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_object_properties(handle: str, limit: int = 50,
                              property_names: list[StrictStr] | None = None,
                              timeout_seconds: float | None = None,
                              offset: StrictInt = 0) -> dict[str, Any]:
        """Read handle properties; property_names selects exact unique names in the specified order."""
        params = {"handle": handle, "limit": limit}
        if offset:
            params["offset"] = offset
        if property_names is not None:
            params["property_names"] = property_names
        return native_request("properties", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_array(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                        timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy an array page, including owned reflected struct fields with per-field support/errors; pages do not share an atomic snapshot."""
        return native_request("read_array", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_struct(handle: str, property_name: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy owned reflected fields with per-field support/errors and known nested Vector, Rotator, Transform, LinearColor, and Quat layouts."""
        return native_request("read_struct", {"handle": handle, "property_name": property_name}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_set(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Set<Name> page; offset counts occupied slots, items retain sparse_index, and pages do not share an atomic snapshot."""
        return native_request("read_set", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_read_map(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Map<Name,Object> page with object descriptors or null; offset counts occupied slots and pages do not share an atomic snapshot."""
        return native_request("read_map", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_object_functions(handle: str, limit: int = 50, function_query: str = "",
                              timeout_seconds: float | None = None,
                              offset: StrictInt = 0) -> dict[str, Any]:
        """Get UFunction, parameters, declared classes, and enums with allowed values."""
        params = {"handle": handle, "limit": limit}
        if offset:
            params["offset"] = offset
        if function_query:
            params["function_query"] = function_query
        return native_request("functions", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_snapshot(targets: list[dict[str, Any]], timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy targets={handle, properties:[names]} on one Tick; reads are sequential."""
        return native_request("snapshot", {"targets": targets}, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_snapshot_compare(before: dict[str, Any], after: dict[str, Any],
                              limit: int = 100) -> dict[str, Any]:
        """Compare copies with the selected limit; does not verify the original runtime or provenance of client data."""
        try:
            return compare_snapshots(before, after, limit)
        except SnapshotError as error:
            raise _tool_failure(error) from None


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_start(handle: str, properties: list[str], duration_ms: int,
                         interval_ms: int, capacity: int = 32, ctx: Context = None,
                         timeout_seconds: float | None = None,
                         expected_session_id: str | None = None) -> dict[str, Any]:
        """Observe selected properties on Tick with explicit duration, interval, and buffer capacity."""
        params = {"handle": handle, "properties": properties,
                              "duration_ms": duration_ms, "interval_ms": interval_ms,
                              "capacity": capacity, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_start", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_watch_read(watch_id: str, after_seq: int = 0, limit: int = 100,
                       ctx: Context = None, timeout_seconds: float | None = None,
                       expected_session_id: str | None = None) -> dict[str, Any]:
        """Read events after seq without deleting them; gaps and overflow are explicitly reported."""
        params = {"watch_id": watch_id, "after_seq": after_seq, "limit": limit, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_read", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False))
    def sn2_watch_stop(watch_id: str, ctx: Context = None,
                        timeout_seconds: float | None = None,
                        expected_session_id: str | None = None) -> dict[str, Any]:
        """Stop subsequent reads on the game thread; retain final events."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_stop", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_forget(watch_id: str, ctx: Context = None,
                          timeout_seconds: float | None = None,
                          expected_session_id: str | None = None) -> dict[str, Any]:
        """Release an already completed observation in the current session."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_forget", params, timeout_seconds)


    @server.tool(annotations=read_only)
    def sn2_watch_list(ctx: Context = None, timeout_seconds: float | None = None,
                       expected_session_id: str | None = None) -> dict[str, Any]:
        """List observation metadata for the current native connection without events or values."""
        params = {"client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        return native_request("watch_list", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    def sn2_watch_resume(watch_id: str, expected_session_id: str | None = None,
                        expected_client_owner: str | None = None, ctx: Context = None,
                         timeout_seconds: float | None = None) -> dict[str, Any]:
        """Update optional observation client metadata within the surviving native session."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        if expected_client_owner is not None:
            params["expected_client_owner"] = expected_client_owner
        return native_request("watch_resume", params, timeout_seconds)


    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    def sn2_invoke(handle: str, function: str, arguments: dict[str, Any] | None = None,
                    expected_signature_token: StrictStr | None = None,
                    timeout_seconds: float | None = None) -> dict[str, Any]:
        """Invoke UFunction by signature; Do not retry an unknown effect."""
        params = {"handle": handle, "function": function, "arguments": arguments or {}}
        if expected_signature_token is not None:
            params["expected_signature_token"] = expected_signature_token
        return native_request("invoke", params, timeout_seconds)


    return server


def main(argv: list[str] | None = None) -> int:
    class EnglishArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage: ", "Usage: ", 1).replace("options:", "Options:")

        def error(self, message):
            self.exit(2, "Invalid Subnautica 2 MCP arguments; use --help\n")

    parser = EnglishArgumentParser(description="Subnautica 2 MCP: native runtime server", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, required=True, help="Development workspace root")
    parser.add_argument("--native-port", type=int, default=27223, help="Localhost port of the native bridge")
    parser.add_argument("--game-root", type=Path, help="Selected game root for process identity and native loading")
    parser.add_argument("--runtime-root", type=Path, help="Root of native DLL, loader, and fixed development tools")
    parser.add_argument("--modkit-root", type=Path, help="Content authoring Modkit root; default: D:\\SN2Modding\\Modkit")
    parser.add_argument("--engine-root", type=Path, help="Compatible custom Unreal root; default: D:\\SN2Modding\\Engine")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio",
                        help="MCP transport; default: stdio")
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1",
                        help="HTTP server address; only 127.0.0.1 is allowed")
    parser.add_argument("--port", type=int, default=27224, help="Streamable HTTP port; default: 27224")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("HTTP port must be in the range 1024–65535")
    try:
        server = create_server(args.workspace_root, args.native_port, args.game_root,
                               transport=args.transport, runtime_root=args.runtime_root,
                               modkit_root=args.modkit_root, engine_root=args.engine_root)
    except OSError as error:
        print(f"Could not create Subnautica 2 MCP: operating system access error (errno={error.errno}, winerror={getattr(error, 'winerror', None)})", file=sys.stderr)
        return 2
    except (ValueError, NativeError) as error:
        print(f"Could not create Subnautica 2 MCP: {error}", file=sys.stderr)
        return 2
    logging.getLogger("mcp.server.mcpserver.server").addFilter(_EnglishOperatingSystemLog())
    if args.transport == "streamable-http":
        logging.getLogger("asyncio").addFilter(_EnglishOperatingSystemLog())
        server.run(transport="streamable-http", host=args.host, port=args.port,
                   streamable_http_path="/mcp", stateless_http=False, json_response=True,
                   max_request_body_size=sys.maxsize,
                   transport_security=TransportSecuritySettings(
                       enable_dns_rebinding_protection=True,
                       allowed_hosts=[f"{args.host}:{args.port}"],
                       allowed_origins=[f"http://{args.host}:{args.port}"]))
    else:
        server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
