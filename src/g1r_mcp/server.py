"""MCP server for static tools with an explicit game-channel boundary."""

import argparse
import asyncio
from functools import wraps
import inspect
import json
import logging
from pathlib import Path
import re
import sys
import threading
import traceback
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer, Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from . import __version__
from .packages import PackageError, validate_package
from .sources import SourceCatalog, SourceError
from .native_client import NativeClient, NativeError
from .debug_database import DebugDatabaseCatalog
from .debug_session import DebugSession
from .save_loading import SaveLoading, SaveLoadingError
from . import modlist_dialog
from .tool_catalog import PROTECTABLE_TOOLS, build_tool_contracts
from pydantic import StrictBool, StrictInt, StrictStr
from .workspace import PackageWorkspace, WorkspaceError, _parts, _checked
from .package_export import ExportError, export_package, verify_export
from .snapshots import SnapshotError, compare_snapshots
from .development import DevelopmentError, DevelopmentService
from .patches import PatchError, ScriptPatchBuilder
from .session_ownership import SessionOwnership
from .compile_diagnostics import read_report, read_runtime_report
from .game_environment import InspectionError, verify_source_game


LOCAL_TOOLS = (
    ("g1r_status", "Server status and game-channel boundary"),
    ("g1r_capabilities", "Catalog of implemented and unavailable groups"),
    ("g1r_debug_database", "AngelScript debug database from the game's listener or the shared cache"),
    ("g1r_debug_attach", "Attach the shared AngelScript debugger session"),
    ("g1r_debug_detach", "Detach the shared AngelScript debugger session"),
    ("g1r_debug_status", "Shared debugger connection and stopped state"),
    ("g1r_debug_breakpoints", "Replace breakpoints for one script/module"),
    ("g1r_debug_control", "Pause, continue or step AngelScript execution"),
    ("g1r_debug_stack", "Current stopped AngelScript call stack"),
    ("g1r_debug_variables", "Variables for an explicit debugger expression"),
    ("g1r_debug_evaluate", "Evaluate an expression in an explicit stopped frame"),
    ("g1r_save_list", "Select a profile and list its saved games"),
    ("g1r_save_load", "Load a saved game and handle a mod-list mismatch"),
    ("g1r_save_status", "Read the last shared save-loading result"),
    ("g1r_modlist_dialog", "Read or continue the native mod-list mismatch dialog"),
    ("g1r_source_identity", "Current Modkit Git HEAD and comparison with the revision label"),
    ("g1r_verify_source_game", "Compare Modkit script bytes with the installed game"),
    ("g1r_find_source_files", "Find Modkit or selected workspace-package source files"),
    ("g1r_search_source", "Literal search of Modkit or selected workspace-package sources"),
    ("g1r_read_source", "Bounded reading of Modkit source"),
    ("g1r_read_source_exact", "Full source text preserving BOM, line endings, and SHA-256"),
    ("g1r_list_source_types", "Lexical search of type and base-class declarations with source SHA-256"),
    ("g1r_list_source_functions", "Lexical source-function index with prototypes and SHA-256"),
    ("g1r_validate_package", "Structural validation of an official package"),
    ("g1r_build_script_patch", "Unified diff from verified Modkit bytes without changing files"),
    ("g1r_compare_snapshots", "Compare snapshot copies without game calls"),
)
WORKSPACE_TOOLS = (
    ("g1r_create_package", "Create an official package in the selected workspace", "local_write"),
    ("g1r_create_package_directory", "Create Script, ScriptPatches, Config, Content, or Story directories inside a workspace package", "local_write"),
    ("g1r_list_package_files", "List the descriptor and files from all five official package components", "read_only"),
    ("g1r_read_package_file", "Read a UTF-8 descriptor or component file with SHA-256 for change verification", "read_only"),
    ("g1r_read_package_file_exact", "Exact UTF-8 text from a descriptor or official package component without normalization", "read_only"),
    ("g1r_write_package_file", "Write UTF-8 descriptor or official component files with conflict detection", "local_write"),
    ("g1r_export_package", "Export a complete workspace package to a new directory containing a ZIP and SHA-256 manifest", "local_write"),
    ("g1r_verify_package_export", "Verify the package ZIP and manifest in a workspace export directory", "read_only"),
)
NATIVE_TOOLS = (
    ("g1r_native_status", "Live status of the experimental native channel"),
    ("g1r_native_enable", "Enable the game Tick queue in an already loaded DLL"),
    ("g1r_find_objects", "Find objects in the current game process"),
    ("g1r_object_properties", "Read reflected scalar properties"),
    ("g1r_read_array", "Copy an array page, including structures with owned reflected fields"),
    ("g1r_read_struct", "Copy a structure's owned reflected fields with per-field support and errors"),
    ("g1r_read_set", "Copy an occupied-slot page of a Set<Name> property"),
    ("g1r_read_map", "Copy an occupied-slot page of a Map<Name,Object> property"),
    ("g1r_object_functions", "List an object's reflected functions"),
    ("g1r_invoke", "One explicit reflected-function call"),
    ("g1r_snapshot", "Copy selected object properties on one Tick"),
    ("g1r_watch_start", "Start a bounded observation of selected properties"),
    ("g1r_watch_read", "Read observation events and loss information"),
    ("g1r_watch_stop", "Stop observation and confirm that reads have ceased"),
    ("g1r_watch_forget", "Release a completed observation"),
    ("g1r_watch_list", "Observation metadata for the current native connection"),
    ("g1r_watch_resume", "Update optional observation client metadata"),
    ("g1r_movieplayer_status", "Consistent copy of MoviePlayer wait status without game Tick"),
    ("g1r_movieplayer_finish", "Explicit request to finish a specific MoviePlayer wait"),
)
DEVELOPMENT_TOOLS = (
    "g1r_session_status", "g1r_session_acquire", "g1r_session_release", "g1r_compile_diagnostics",
    "g1r_development_recipe", "g1r_development_processes", "g1r_development_start",
    "g1r_development_stop", "g1r_development_key", "g1r_native_load", "g1r_apply_staged_file",
)


def _tool_failure(error: SourceError | PackageError | WorkspaceError | SnapshotError | PatchError | InspectionError | ExportError) -> ToolError:
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


def create_server(modkit_root: Path, workspace_root: Path, source_revision: str | None = None,
                  native_port: int = 27123, game_root: Path | None = None, *, transport: str = "stdio",
                  runtime_root: Path | None = None) -> MCPServer:
    """Create a server with a Modkit catalog and a separate package workspace."""
    if source_revision is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", source_revision):
        raise ValueError("Source revision must be a full Git SHA or omitted")
    if transport not in ("stdio", "streamable-http"):
        raise ValueError("Transport must be stdio or streamable-http")
    workspace_root = workspace_root.absolute()
    if not workspace_root.is_dir():
        raise ValueError("Workspace directory does not exist")
    for component in (workspace_root, *workspace_root.parents):
        if component.is_symlink() or (hasattr(component, "is_junction") and component.is_junction()):
            raise ValueError("Workspace directory contains a symlink or junction")
    workspace_root = workspace_root.resolve(strict=True)
    sources = SourceCatalog(modkit_root, revision=source_revision)
    debug_database = DebugDatabaseCatalog()
    debug_session = DebugSession()
    patch_builder = ScriptPatchBuilder(modkit_root, revision=source_revision)
    workspace = PackageWorkspace(workspace_root, protected_roots=[modkit_root])
    native = NativeClient(port=native_port)
    native_lock = threading.Lock()
    development_lock = threading.Lock()
    configured_game_root = game_root or Path(r"E:\SteamLibrary\steamapps\common\Gothic 1 Remake")
    configured_runtime_root = runtime_root.resolve() if runtime_root is not None else Path(__file__).resolve().parents[2]
    development = None
    ownership = SessionOwnership()
    local_owner = object()

    def request_owner(ctx: Context | None) -> Any:
        if ctx is not None:
            try:
                request = ctx.request_context.request
                session_id = request.headers.get("mcp-session-id") if request is not None else None
                if session_id:
                    return "http:" + session_id
                return ctx.request_context.session
            except ValueError:
                pass
        return local_owner

    def watch_owner(ctx: Context | None) -> str:
        return "shared_native_connection"

    def development_service() -> DevelopmentService:
        nonlocal development
        with development_lock:
            if development is None:
                development = DevelopmentService(repository_root=configured_runtime_root,
                                                 workspace_root=workspace_root, modkit_root=modkit_root,
                                                 game_root=configured_game_root)
            return development

    @asynccontextmanager
    async def server_lifespan(_server: MCPServer):
        """Close shared resources when the server stops, rather than when an HTTP session ends."""
        try:
            yield
        finally:
            with native_lock:
                current_native = native
            with development_lock:
                current_development = development
            try:
                current_native.close()
            finally:
                debug_session.detach()
                if current_development is not None:
                    current_development.close()
    server = MCPServer(
        "G1R MCP", version=__version__, log_level="WARNING",
        lifespan=server_lifespan,
        instructions=(
            "Development bridge for official GothicMod.json AngelScript packages; it is not UE4SS/Lua. "
            "Start with g1r_status and g1r_capabilities: they do not check the live game. "
            "For a new development session: g1r_development_recipe → g1r_development_processes → "
            "explicitly authorized g1r_development_start with manifest SHA-256 → g1r_development_processes → "
            "g1r_native_load with exact PID/creation_time/executable and DLL SHA-256 → g1r_native_status. "
            "If Tick is not installed, explicitly call g1r_native_enable and check ready through g1r_native_status; "
            "a Steam request and DLL loading alone do not confirm compilation or Tick readiness. "
            "For an already running bridge, start with native_status, then g1r_find_objects and "
            "g1r_object_functions before an explicit g1r_invoke; end your session with g1r_development_stop. "
            "One TCP client owns handles and observations; find objects again after reconnecting. "
            "Load an existing save from the main menu with g1r_save_list(profile_id), then "
            "g1r_save_load(slot_name, profile_id); inspect g1r_save_status after a timeout. "
            "The load tool handles the native mod-list mismatch dialog and waits for a gameplay-ready player. "
            "Use g1r_modlist_dialog outside Tick for a dialog created by a separate load call. "
            "Edit complete bytes through g1r_read_source_exact/g1r_read_package_file_exact and SHA-256; "
            "line-based reads normalize text. Search and valid_structure do not constitute compilation. "
            "ScriptPatches are created as diffs without applying them; official-runtime application to base Script sources was confirmed by an isolated probe. Each package still requires runtime verification. "
            "Starting, stopping, DLL loading, and invoke change external state: do not automatically retry an operation "
            "after outcome_unknown, may_have_executed, or do_not_retry. "
            "Do not infer that a game API works merely because its source exists."
        ),
    )
    read_only = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
    debug_action = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False)

    def protection_failure(error: DevelopmentError) -> ToolError:
        payload = {"code": error.code, "message": str(error), "outcome_unknown": False}
        if error.receipt is not None:
            payload["receipt"] = error.receipt
        return ToolError(json.dumps(payload, ensure_ascii=False))

    def protected_tool(function):
        """Check explicit tool protection at admission, preserving its MCP signature."""
        signature = inspect.signature(function)
        accepts_context = "ctx" in signature.parameters
        guarded_signature = signature if accepts_context else signature.replace(parameters=[
            *signature.parameters.values(),
            inspect.Parameter("ctx", inspect.Parameter.KEYWORD_ONLY, default=None, annotation=Context),
        ])

        def check(args, kwargs):
            arguments = guarded_signature.bind_partial(*args, **kwargs).arguments
            if function.__name__ == "g1r_modlist_dialog" and arguments.get("action", "status") != "continue":
                return
            try:
                with ownership.lock:
                    ownership.check(request_owner(arguments.get("ctx")), function.__name__)
            except DevelopmentError as error:
                raise protection_failure(error) from None

        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def guarded(*args, **kwargs):
                check(args, kwargs)
                if not accepts_context:
                    kwargs.pop("ctx", None)
                return await function(*args, **kwargs)
        else:
            @wraps(function)
            def guarded(*args, **kwargs):
                check(args, kwargs)
                if not accepts_context:
                    kwargs.pop("ctx", None)
                return function(*args, **kwargs)
        if not accepts_context:
            guarded.__signature__ = guarded_signature
            guarded.__annotations__ = {**function.__annotations__, "ctx": Context}
        return guarded

    @server.tool(annotations=read_only)
    def g1r_status(ctx: Context = None) -> dict[str, Any]:
        """Get the server version and identify results that are not confirmed in the game."""
        return {
            "server": {"name": "G1R MCP", "version": __version__, "transport": transport, "ready": True,
                       "runtime_root": str(configured_runtime_root)},
            "sources": {"kind": "official_modkit_static", "root": str(sources.root), "revision": source_revision,
                        "revision_source": "caller_label", "validation": "source_only",
                        "identity_tool": "g1r_source_identity"},
            "workspace": {"root": str(workspace.root), "format": "GothicMod.json/Script/ScriptPatches/Config/Content/Story",
                          "installed_game_deployment": False},
            "game_session": ownership.status(request_owner(ctx)),
            "game": {
                "backend_implemented": True, "connection_checked": False, "ready": False, "ready_known": False,
                "configured_root": str(configured_game_root.absolute()),
                "reason_code": "use_native_status_for_live_state",
                "reason": "Call g1r_native_status for current DLL status; local tools are independent",
                "last_research": {
                    "date": "2026-10-30", "buildid": "26295476", "live_status": "not_checked_by_this_tool",
                    "source": "https://github.com/AlkimiaInteractive/G1R-Modkit/issues/1#issuecomment-6520702135",
                },
            },
        }

    @server.tool(annotations=read_only)
    async def g1r_capabilities() -> dict[str, Any]:
        """Get the catalog of local tools and unavailable game groups."""
        tool_contracts = build_tool_contracts(await server.list_tools())
        contracts_by_name = {contract["name"]: contract for contract in tool_contracts}
        return {
            "catalog_revision": __version__,
            "catalog_revision_kind": "server_tool_contract",
            "tool_contracts": tool_contracts,
            "parallel_work": {"default": "shared", "opt_in": "g1r_session_acquire.protected_tools",
                              "protectable_tools": list(PROTECTABLE_TOOLS), "reads_always_allowed": True,
                              "conditional_read_paths": {"g1r_modlist_dialog": {"action": "status"}},
                              "check_point": "dispatch_admission", "already_admitted_calls_cancelled": False,
                              "release_tool": "g1r_session_release", "takeover_parameter": "takeover"},
            "local_tools": [
                {"name": name, "description": description, "implemented": True,
                 "available": (None if contracts_by_name[name]["result"]["dynamic_fields"]
                               and contracts_by_name[name]["context"]["requires"] else True),
                 "effects": "read_only" if contracts_by_name[name]["effects"]["read_only"] else "runtime_state_change",
                 "validation": "runtime_not_checked" if contracts_by_name[name]["result"]["dynamic_fields"] else "local_only"}
                for name, description in LOCAL_TOOLS
            ],
            "native_tools": [{"name": name, "description": description, "implemented": True,
                              "read_only_hint": name in {"g1r_native_status", "g1r_find_objects", "g1r_object_properties", "g1r_object_functions", "g1r_read_array", "g1r_read_struct", "g1r_read_set", "g1r_read_map", "g1r_snapshot", "g1r_watch_read", "g1r_watch_list", "g1r_movieplayer_status"},
                              "availability": ("requires_native_tcp_channel" if name == "g1r_movieplayer_status"
                                               else "requires_loaded_movieplayer_hooks_and_matching_wait" if name == "g1r_movieplayer_finish"
                                               else "requires_live_native_status")} for name, description in NATIVE_TOOLS],
            "workspace_tools": [
                {"name": name, "description": description, "implemented": True,
                 "available": True, "effects": effects, "validation": "local_only"}
                for name, description, effects in WORKSPACE_TOOLS
            ],
            "game_groups": [
                {"name": "compile_diagnostics", "implemented": True, "available": None,
                 "availability": "caller_selected_file_or_native_compiler_manager",
                 "reason_code": "current_availability_not_checked"},
            ] + [
                {"name": group, "implemented": False, "available": False,
                 "reason_code": "confirmed_operation_unavailable"}
                for group in ("arbitrary_angelscript",)
            ],
            "development_tools": {"implemented": True, "availability": "requires_windows_and_configured_game",
                                  "operations": list(DEVELOPMENT_TOOLS),
                                  "launch_result": "launch_requested", "compiler_result": "requires_runtime_probe",
                                  "companion_packages": {"supported": True, "max_packages": None,
                                                         "edit_target": "active_staging_packages",
                                                         "sources_modified": False, "retention": "retain_for_review"},
                                  "helpers": {"relative_package": "development/helpers/G1RMCPDevelopmentHelpers",
                                              "class": "/Script/Angelscript.G1RMCPDevelopmentHelpers",
                                              "requires": "explicit_companion_package_and_live_world_instance",
                                              "functions": ["GetHelperVersion", "ReadText", "HasWorld", "HasGameplayPlayer", "GetVisibilityTraceChannel", "TraceImpactPoint"]}},
            "compiler_reports": {"tool": "g1r_compile_diagnostics", "implemented": True,
                                 "source": "native_formatter_or_caller_selected_workspace_file", "compiler_api_available": True,
                                 "absence_of_errors_proves_compile": False},
            "script_runtime_errors": {"tool": "g1r_invoke", "implemented": True,
                                      "source": "observed_logging_entry", "scope": "current_thread_process_event",
                                      "error_code": "angelscript_exception", "details_field": "script_errors",
                                      "historical_errors_available": False, "stack_captured": None,
                                      "stack_capture_supported": True,
                                      "stack_result_field": "script_errors.stack_captured",
                                      "absence_of_logged_errors_proves_script_completion": False},
            "array_values": {"tool": "g1r_read_array", "source": "runtime_reflection",
                             "result_field": "items", "item_fields": ["index", "supported", "value", "error", "components", "partial"],
                             "struct_element_type": "StructProperty", "struct_value_shape": "object",
                             "struct_metadata_field": "element_struct",
                             "struct_metadata_fields": ["struct_path", "struct_name", "properties_size", "min_alignment", "components"],
                             "struct_component_fields": ["name", "type", "element_size", "offset", "array_dim"],
                             "struct_value_component_fields": ["name", "type", "element_size", "offset", "array_dim", "supported", "value", "enum", "error", "components", "items", "partial", "total_count"],
                             "struct_field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                    "UInt64Property", "FloatProperty", "DoubleProperty", "ByteProperty",
                                                    "EnumProperty", "NameProperty", "StrProperty", "ObjectProperty", "WeakObjectProperty", "ClassProperty", "StructProperty", "ArrayProperty"],
                             "own_fields_only": True, "nested_struct_fields_supported": True,
                             "string_struct_fields_supported": True, "nested_arrays_supported": True,
                             "nested_layout": "reflected_own_struct_and_array_fields",
                             "nested_child_errors": "per_child_partial",
                             "buffer_ownership": "borrowed_native_buffers", "buffer_cleanup": "none",
                             "consistency": "single_tick_sequential", "multiple_pages_atomic": False,
                             "availability": "requires_live_native_status_and_current_game_tick",
                             "current_checked": False},
            "weak_values": {"source": "runtime_reflection", "property_type": "WeakObjectProperty",
                            "tools": ["g1r_object_properties", "g1r_read_array", "g1r_read_struct"],
                            "result_shape": "object_descriptor_or_null", "weak_resolution": "registry_index_serial",
                            "declared_type_source": "reflected_property_class",
                            "null_when": ["zero_serial", "negative_index", "missing_slot", "serial_mismatch"],
                            "null_supported": True, "gc_reachability_checked": False, "engine_get_invoked": False},
            "set_values": {"tool": "g1r_read_set", "source": "runtime_reflection",
                           "container_type": "SetProperty", "supported_element_types": ["NameProperty"],
                           "result_field": "items", "item_fields": ["index", "sparse_index", "supported", "value", "error"],
                           "element_metadata_field": "element",
                           "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                           "offset_semantics": "occupied_slot_ordinal", "sparse_index_semantics": "physical_slot_index",
                           "page_fields": ["total_count", "max_index", "offset", "next_offset", "has_more", "truncated", "partial"],
                           "total_count_semantics": "occupied_slots", "max_index_semantics": "sparse_slot_upper_bound",
                           "consistency": "copied_slots_then_metadata_revalidated", "multiple_pages_atomic": False,
                           "availability": "requires_live_native_status_and_current_game_tick", "current_checked": False},
            "map_values": {"tool": "g1r_read_map", "source": "runtime_reflection",
                           "container_type": "MapProperty", "supported_key_types": ["NameProperty"],
                           "supported_value_types": ["ObjectProperty"], "null_object_values_supported": True,
                           "result_field": "items", "item_fields": ["index", "sparse_index", "supported", "key", "value", "error"],
                           "key_metadata_field": "key", "value_metadata_field": "value",
                           "key_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                           "value_metadata_fields": ["type", "element_size", "array_dim", "offset", "declared_type"],
                           "value_offset_field": "value_offset", "value_offset_source": "reflected_embedded_layout",
                           "declared_type_fields": ["name", "path"],
                           "offset_semantics": "occupied_slot_ordinal", "sparse_index_semantics": "physical_slot_index",
                           "page_fields": ["total_count", "max_index", "offset", "next_offset", "has_more", "truncated", "partial"],
                           "total_count_semantics": "occupied_slots", "max_index_semantics": "sparse_slot_upper_bound",
                           "consistency": "copied_slots_then_metadata_revalidated", "multiple_pages_atomic": False,
                           "availability": "requires_live_native_status_and_current_game_tick", "current_checked": False},
            "struct_values": {"tool": "g1r_read_struct", "source": "runtime_reflection",
                              "result_field": "components",
                              "component_fields": ["name", "type", "element_size", "offset", "array_dim", "supported", "value", "enum", "error", "struct_path", "total_count", "element_struct_path", "components", "items", "partial"],
                              "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                              "UInt64Property", "FloatProperty", "DoubleProperty", "ByteProperty",
                                              "EnumProperty", "NameProperty", "StrProperty", "ObjectProperty", "WeakObjectProperty",
                                              "ClassProperty", "StructProperty", "ArrayProperty"],
                              "own_fields_only": True, "layout": "reflected_fields",
                              "properties_size": "reflected", "component_count": "reflected",
                              "known_nested_struct_types": ["/Script/CoreUObject.Vector", "/Script/CoreUObject.Rotator",
                                                            "/Script/CoreUObject.Transform", "/Script/CoreUObject.LinearColor", "/Script/CoreUObject.Quat"],
                              "supported_array_element_structs": ["/Script/GameplayTags.GameplayTag"],
                              "supported_array_element_structs_scope": "known_examples_not_exhaustive",
                              "unknown_nested_structs_supported": True, "string_fields_supported": True,
                              "nested_arrays_supported": True, "nested_layout": "reflected_own_struct_and_array_fields",
                              "nested_child_errors": "per_child_partial",
                              "buffer_ownership": "borrowed_native_buffers", "buffer_cleanup": "none",
                              "consistency": "copied_bytes_then_metadata_revalidated",
                              "availability": "requires_live_native_status_and_current_game_tick",
                              "current_checked": False},
            "source_functions": {"tool": "g1r_list_source_functions", "parser_kind": "lexical",
                                 "result_field": "items", "prototype": "raw_source",
                                 "item_fields": ["name", "owner", "namespace", "kind", "prototype", "path",
                                                 "start_line", "end_line", "file_sha256", "root_kind"],
                                 "runtime_signature_confirmed": False},
            "source_types": {"tool": "g1r_list_source_types", "parser_kind": "lexical",
                             "declaration": "raw_source", "base_type": "unresolved_source",
                             "item_fields": ["kind", "name", "qualified_name", "line", "declaration", "base_type",
                                             "start_line", "end_line", "path", "file_sha256", "root_kind"],
                             "runtime_type_confirmed": False},
            "function_parameter_types": {"tool": "g1r_object_functions", "source": "runtime_reflection",
                                         "object_property": "declared_type", "class_property": "meta_class",
                                         "enum_property": "enum", "descriptor_fields": ["name", "path"],
                                         "enum_fields": ["path", "underlying_type", "entries"],
                                         "enum_entry_fields": ["name", "value"]},
            "source_identity": {"tool": "g1r_source_identity", "snapshot_kind": "sequential_git_reads",
                                "source_only": True, "revision_source": "caller_label",
                                "worktree_scope": "repository", "game_compatibility_checked": False},
            "source_game_verification": {"tool": "g1r_verify_source_game", "snapshot_kind": "sequential_file_reads",
                                         "correlation_scope": "modkit_script_subset_byte_equality",
                                         "paths": "server_configuration", "runtime_compatibility_checked": False,
                                         "hash_executable_default": False},
            "bootstrap": {
                "game_sequence": [
                    {"tool": "g1r_development_recipe", "condition": "Validate the selected workspace package and obtain manifest/DLL SHA-256"},
                    {"tool": "g1r_development_processes", "condition": "Determine the initial PID/creation_time/executable"},
                    {"tool": "g1r_development_start", "condition": "Only an explicitly selected launch; restart requires the initial identity"},
                    {"tool": "g1r_development_processes", "condition": "Get the identity of the launched internal game EXE"},
                    {"tool": "g1r_native_load", "condition": "One load with the expected process and DLL SHA-256"},
                    {"tool": "g1r_native_status", "condition": "Check the installed Tick and ready status"},
                    {"tool": "g1r_native_enable", "condition": "Only if hook_installed=false and enabling is explicitly authorized"},
                    {"tool": "g1r_native_status", "condition": "Wait for ready=true; an unknown result is not a compilation failure"},
                    {"tool": "g1r_find_objects", "condition": "Find a current session handle and verify the exact object"},
                    {"tool": "g1r_object_functions", "condition": "Read the reflected signature, using function_query if needed"},
                    {"tool": "g1r_invoke", "condition": "One explicitly selected call using a confirmed signature"},
                    {"tool": "g1r_development_stop", "condition": "When explicitly ending your development session with the expected identity"},
                ],
                "existing_session_sequence": ["g1r_native_status", "g1r_find_objects", "g1r_object_functions"],
                "edit_tools": ["g1r_read_source_exact", "g1r_read_package_file_exact", "g1r_write_package_file", "g1r_apply_staged_file"],
                "hot_reload_sequence": [
                    {"tool": "g1r_read_package_file_exact", "condition": "Read the canonical source and its active staged counterpart with exact SHA-256"},
                    {"tool": "g1r_write_package_file", "condition": "Edit the canonical source using its expected SHA-256"},
                    {"tool": "g1r_apply_staged_file", "condition": "Apply one canonical file to the owned active staging package using both expected hashes and the exact process identity"},
                    {"tool": "g1r_development_recipe", "condition": "Obtain the current manifest SHA-256 for the returned staged package"},
                    {"tool": "g1r_compile_diagnostics", "condition": "Read compiler diagnostics for that staged package; a successful file write does not confirm compilation"},
                    {"tool": "g1r_object_functions", "condition": "Refresh the reflected signature before an explicit call to observe the hot-reloaded behavior"},
                ],
                "export_tools": ["g1r_export_package", "g1r_verify_package_export"],
                "export_sequence": [
                    {"tool": "g1r_export_package", "condition": "Export the complete package to a new directory under workspace artifacts"},
                    {"tool": "g1r_verify_package_export", "condition": "Verify the ZIP and SHA-256 manifest in the export directory"},
                ],
                "argument_examples": {
                    "g1r_create_package": {"relative_path": "Example",
                                           "metadata": {"name": "Example", "title": "Example", "author": "Author", "modVersion": "1.0.0"}},
                    "g1r_create_package_directory": {"relative_path": "Example", "directory_path": "Script/Components"},
                    "g1r_snapshot": {"targets": [{"handle": "use_handle_from_find_objects", "properties": ["UseNameFromObjectProperties"]}]},
                    "g1r_invoke": {"handle": "use_handle_from_find_objects", "function": "use_name_from_object_functions",
                                   "arguments": {"use_reflected_parameter_name": None}},
                    "g1r_list_package_files": {"relative_path": "Example", "limit": 100, "offset": 0},
                    "g1r_export_package": {"relative_package": "Example", "destination": "artifacts/Example-export"},
                    "g1r_verify_package_export": {"relative_path": "artifacts/Example-export"},
                },
                "ownership": "one_native_tcp_client_session",
                "reconnect": "discard_old_handles_and_watches_then_find_objects_again",
                "uncertain_effect": "never_automatically_retry_outcome_unknown_may_have_executed_do_not_retry",
                "package_format": "GothicMod.json/Script/ScriptPatches/Config/Content/Story",
                "script_patch_engine_apply_confirmed": True,
            },
            "invoke_arguments": {
                "rotator": {"type": "FRotator", "struct_path": "/Script/CoreUObject.Rotator",
                            "modes": ["input", "const_reference_input"], "size": 24,
                            "min_alignment": 8, "array_dim": 1, "units": "degrees",
                            "input_shape": {"rotator": {"Pitch": "finite_number", "Yaw": "finite_number", "Roll": "finite_number"}},
                            "reference_input_supported": True, "const_reference_required": True,
                            "layout_source": "reflected_function_parameter", "requires_signature_validation": True},
                "name": {"type": "FName", "input_shape": {"name": "string"},
                         "source_property_input_shape": {"name_from": {"handle": "session_handle",
                                                                         "property_name": "reflected_property_name"}}},
                "gameplay_tag": {"type": "FGameplayTag", "struct_path": "/Script/GameplayTags.GameplayTag",
                                 "modes": ["input", "const_reference_input"], "array_dim": 1,
                                 "input_shape": {"TagName": {"name": "string"}},
                                 "source_property_input_shape": {"TagName": {"name_from": {
                                     "handle": "session_handle", "property_name": "reflected_property_name"}}},
                                 "own_fields_only": True, "field_types": {"TagName": "NameProperty"},
                                 "reference_input_supported": True, "const_reference_required": True,
                                 "out_supported": False, "inout_supported": False,
                                 "layout_source": "reflected_function_parameter", "requires_signature_validation": True},
                "linear_color": {"type": "FLinearColor", "struct_path": "/Script/CoreUObject.LinearColor",
                                 "element_size": 16, "min_alignment": 4,
                                 "input_shape": {"R": "finite_float32", "G": "finite_float32",
                                                 "B": "finite_float32", "A": "finite_float32"},
                                 "passing": "value", "return_supported": True,
                                 "reference_input_supported": False, "out_supported": False,
                                 "inout_supported": False, "clamp": "none", "gamma_conversion": "none",
                                 "layout_source": "reflected_function_parameter",
                                 "requires_signature_validation": True},
                "object_array": {"type": "TArray<UObject>", "input_shape": "array",
                                 "element_shape": [None, {"handle": "session_handle"}],
                                 "passing": "const_reference", "const_reference_required": True,
                                 "element_type": "ObjectProperty", "element_size": 8,
                                 "max_elements": 2147483647, "max_inputs": None,
                                 "max_allocated_input_bytes": None, "empty_allowed": True,
                                 "order_preserved": True, "duplicates_allowed": True,
                                 "ownership": "engine_owned_pointer_copy", "objects_owned": False,
                                 "by_value_input_supported": False, "out_supported": False,
                                 "inout_supported": False, "return_supported": True,
                                 "return_passing": "value",
                                 "mixed_string_signatures_supported": True,
                                 "requires_signature_validation": True,
                                 "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "object_array_output": {"type": "TArray<UObject>", "argument_marker": {"out": True},
                                        "direction": "output", "size": 16, "array_dim": 1,
                                        "element_type": "ObjectProperty", "element_size": 8,
                                        "pure_output_only": True, "initial_buffer": "empty_array",
                                        "const_supported": False, "reference_supported": False, "inout_supported": False,
                                        "output_field": "out_parameters", "metadata_field": "out_parameter_metadata",
                                        "output_keys": "reflected_parameter_name",
                                        "result_fields": ["items", "total_count", "partial"],
                                        "item_fields": ["index", "supported", "value", "error"],
                                        "element_shape": "object_descriptor_or_null", "order_preserved": True,
                                        "metadata_fields": ["container_type", "element"],
                                        "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "declared_type"],
                                        "declared_type_fields": ["name", "path"],
                                        "layout_source": "saved_reflected_function_parameter", "objects_owned": False,
                                        "cleanup_fields": ["cleanup_confirmed", "array_output_cleanup_confirmed"],
                                        "normal_output_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False,
                                        "requires_signature_validation": True,
                                        "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "string_array": {"type": "TArray<FString>", "input_shape": "array", "element_shape": "string",
                                 "direction": "input", "passing": "value_or_const_reference", "const_reference_required": False,
                                 "size": 16, "array_dim": 1, "element_type": "StrProperty", "element_size": 16,
                                 "element_array_dim": 1, "element_offset": 0, "empty_allowed": True,
                                 "empty_strings_allowed": True, "unicode_supported": True, "embedded_nul_allowed": False,
                                 "order_preserved": True, "duplicates_allowed": True,
                                 "metadata_field": "array", "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                                 "layout_source": "reflected_function_parameter", "ownership": "bridge_owned_inner_strings_and_outer_array",
                                 "normal_cleanup": "free_inner_strings_and_outer_array",
                                 "uncertain_construction_cleanup": "retain_unconfirmed_owned_allocations",
                                 "cleanup_fields": ["cleanup_confirmed", "array_input_cleanup_confirmed", "string_array_cleanup_confirmed"],
                                 "by_value_input_supported": True, "reference_input_supported": True,
                                 "out_supported": True, "inout_supported": False, "return_supported": True,
                                 "nested_dto_string_fields_supported": False, "requires_signature_validation": True,
                                 "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "basic_scalar_array": {"input_shape": "array", "direction": "input",
                                       "passing": "value_or_const_reference", "const_reference_required": False,
                                       "size": 16, "array_dim": 1, "element_array_dim": 1, "element_offset": 0,
                                       "element_types": {"BoolProperty": {"element_size": 1, "value_shape": "boolean",
                                                                           "bool_layout": "reflected_bool_layout"},
                                                         "IntProperty": {"element_size": 4, "value_shape": "integer",
                                                                         "integer_range": [-2147483648, 2147483647]},
                                                         "Int64Property": {"element_size": 8, "value_shape": "integer",
                                                                           "integer_range": [-9223372036854775808, 9223372036854775807]},
                                                         "UInt32Property": {"element_size": 4, "value_shape": "integer",
                                                                            "integer_range": [0, 4294967295]},
                                                         "UInt64Property": {"element_size": 8, "value_shape": "integer",
                                                                            "integer_range": [0, 18446744073709551615]},
                                                         "FloatProperty": {"element_size": 4, "value_shape": "finite_float32"},
                                                         "DoubleProperty": {"element_size": 8, "value_shape": "finite_float64"}},
                                       "element_validation": "existing_scalar_encoder_per_item", "padding": "zero_initialized",
                                       "metadata_field": "array", "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                                       "bool_element_metadata_field": "bool_layout", "layout_source": "reflected_function_parameter",
                                       "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                                       "ownership": "bridge_owned_copy", "normal_cleanup": "free_input_copy",
                                       "uncertain_construction_cleanup": "retain_input_copy",
                                       "by_value_input_supported": True, "reference_input_supported": True,
                                       "out_supported": True, "inout_supported": False, "return_supported": True,
                                       "requires_signature_validation": True,
                                       "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "scalar_array_output": {"argument_marker": {"out": True}, "direction": "output",
                                        "size": 16, "array_dim": 1, "pure_output_only": True,
                                        "initial_buffer": "empty_array", "const_supported": False,
                                        "reference_supported": False, "inout_supported": False,
                                        "element_types": {"BoolProperty": {"element_size": 1, "value_shape": "boolean",
                                                                          "bool_layout": "reflected_bool_layout"},
                                                          "IntProperty": {"element_size": 4, "value_shape": "integer"},
                                                          "Int64Property": {"element_size": 8, "value_shape": "integer"},
                                                          "UInt32Property": {"element_size": 4, "value_shape": "integer"},
                                                          "UInt64Property": {"element_size": 8, "value_shape": "integer"},
                                                          "FloatProperty": {"element_size": 4, "value_shape": "finite_float32"},
                                                          "DoubleProperty": {"element_size": 8, "value_shape": "finite_float64"},
                                                          "StrProperty": {"element_size": 16, "value_shape": "string",
                                                                          "cleanup": "inner_strings_and_outer_array"},
                                                          "NameProperty": {"element_size": 8, "value_shape": "string"},
                                                          "EnumProperty": {"element_size": 1, "underlying_type": "ByteProperty",
                                                                           "value_shape": "integer", "item_enum_metadata": True},
                                                          "StructProperty": {"element_size": "reflected_struct_size",
                                                                             "min_alignment": "reflected_struct_min_alignment",
                                                                             "value_shape": "object", "own_fields_only": True}},
                                        "element_array_dim": 1, "element_offset": 0,
                                        "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                        "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
                                                        "EnumProperty", "ByteProperty", "StructProperty"],
                                        "nested_struct_fields": "own_reflected_fields_recursive",
                                        "field_metadata_fields": ["struct", "enum", "bool_layout"],
                                        "name_field_input_shape": {"name": "string"}, "name_from_fields_supported": False,
                                        "enum_field_underlying_type": "ByteProperty", "enum_field_bytes": 1,
                                        "nested_fields_supported": True, "string_fields_supported": False,
                                        "custom_lifecycle_supported": False, "source_contract_required": True,
                                        "output_field": "out_parameters", "metadata_field": "out_parameter_metadata",
                                        "output_keys": "reflected_parameter_name",
                                        "result_fields": ["items", "total_count", "partial"],
                                        "item_fields": ["index", "supported", "value", "error"],
                                        "enum_item_fields": ["index", "supported", "value", "enum", "error"],
                                        "order_preserved": True, "values_copied": True,
                                        "metadata_fields": ["container_type", "element"],
                                        "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                                        "bool_element_metadata_field": "bool_layout",
                                        "enum_element_metadata_field": "enum", "struct_element_metadata_field": "struct",
                                        "struct_metadata_schema": "existing_scalar_struct_snapshot",
                                        "layout_source": "saved_reflected_function_parameter",
                                        "cleanup_fields": ["cleanup_confirmed", "array_output_cleanup_confirmed"],
                                        "normal_output_cleanup_confirmed": True,
                                        "uncertain_construction_cleanup_confirmed": False,
                                        "requires_signature_validation": True,
                                        "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "enum_array": {"type": "TArray<enum>", "input_shape": "array",
                               "element_shape": "integer", "input_example": [1, 3, 9],
                               "direction": "input", "passing": "value", "size": 16, "array_dim": 1,
                               "element_type": "EnumProperty", "element_size": 1,
                               "underlying_type": "ByteProperty", "metadata_field": "array",
                               "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "enum"],
                               "enum_fields": ["path", "underlying_type", "entries"],
                               "enum_entry_fields": ["name", "value"],
                               "element_validation": "encode_integer_per_item",
                               "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                               "ownership": "bridge_owned_copy", "normal_cleanup": "free_input_copy",
                               "uncertain_construction_cleanup": "retain_input_copy",
                               "by_value_input_supported": True, "out_supported": True,
                               "inout_supported": False, "return_supported": True,
                               "requires_signature_validation": True,
                               "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "int_array": {"type": "TArray<int32>", "input_shape": "array", "element_shape": "integer",
                              "input_example": [2147483647, -2147483648],
                              "direction": "input", "passing": "value_or_const_reference", "size": 16, "array_dim": 1,
                              "runtime_passing": "normalized_const_reference",
                              "runtime_passing_source": "current_unreal_angelscript_function_builder",
                              "const_reference_required": False,
                              "element_type": "IntProperty", "element_size": 4, "metadata_field": "array",
                              "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                              "element_validation": "signed_int32_per_item", "integer_range": [-2147483648, 2147483647],
                              "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                              "ownership": "bridge_owned_copy", "normal_cleanup": "free_input_copy",
                              "uncertain_construction_cleanup": "retain_input_copy",
                              "by_value_input_supported": True, "reference_input_supported": True,
                              "out_supported": True, "inout_supported": False, "return_supported": True,
                              "requires_signature_validation": True,
                              "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "name_array": {"type": "TArray<FName>", "input_shape": "array",
                               "element_shape": {"name": "string"},
                               "source_property_element_shape": {"name_from": {
                                   "handle": "session_handle", "property_name": "reflected_property_name"}},
                               "direction": "input", "passing": "value_or_const_reference", "size": 16, "array_dim": 1,
                               "const_reference_required": False,
                               "element_type": "NameProperty", "element_size": 8, "element_array_dim": 1,
                               "element_offset": 0, "metadata_field": "array",
                               "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                               "element_validation": "exact_name_or_name_from_per_item",
                               "source_revalidation": "after_last_allocation_before_invocation",
                               "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                               "ownership": "bridge_owned_copy", "normal_cleanup": "free_input_copy",
                               "uncertain_construction_cleanup": "retain_input_copy",
                               "by_value_input_supported": True, "reference_input_supported": True,
                               "out_supported": True, "inout_supported": False, "return_supported": True,
                               "requires_signature_validation": True,
                               "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "scalar_struct_array": {"type": "TArray<own scalar DTO>", "input_shape": "array", "element_shape": "object",
                                        "direction": "input", "passing": "value_or_const_reference",
                                        "const_reference_required": False, "size": 16, "array_dim": 1,
                                        "element_type": "StructProperty", "element_size": "reflected_struct_size",
                                        "element_min_alignment": "reflected_struct_min_alignment",
                                        "element_array_dim": 1, "element_offset": 0, "own_fields_only": True,
                                        "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                        "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
                                                        "EnumProperty", "ByteProperty", "StructProperty"],
                                        "nested_struct_fields": "own_reflected_fields_recursive",
                                        "field_metadata_fields": ["struct", "enum", "bool_layout"],
                                        "name_field_input_shape": {"name": "string"}, "name_from_fields_supported": False,
                                        "enum_field_underlying_type": "ByteProperty", "enum_field_bytes": 1,
                                        "field_bytes": {"IntProperty": 4, "Int64Property": 8, "UInt32Property": 4,
                                                        "UInt64Property": 8, "FloatProperty": 4, "DoubleProperty": 8,
                                                        "NameProperty": 8, "EnumProperty": 1, "ByteProperty": 1},
                                        "field_names": "exact_reflected_component_names", "bool_layout": "reflected_bool_layout",
                                        "padding": "zero_initialized", "metadata_field": "array",
                                        "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "struct"],
                                        "struct_metadata_schema": "existing_scalar_struct_snapshot",
                                        "layout_source": "reflected_function_parameter", "lifecycle": "plain_default_copy",
                                        "custom_lifecycle_supported": False, "source_contract_required": True,
                                        "nested_fields_supported": True, "string_fields_supported": False,
                                        "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                                        "ownership": "bridge_owned_copy", "normal_cleanup": "free_input_copy",
                                        "uncertain_construction_cleanup": "retain_input_copy",
                                        "by_value_input_supported": True, "reference_input_supported": True,
                                        "out_supported": True, "inout_supported": False, "return_supported": True,
                                        "requires_signature_validation": True,
                                        "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "scalar_struct": {"type": "StructProperty", "input_shape": "object", "direction": "input",
                                  "passing": "value", "array_dim": 1, "own_fields_only": True,
                                  "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                  "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
                                                  "EnumProperty", "ByteProperty", "StructProperty"],
                                  "nested_struct_fields": "own_reflected_fields_recursive",
                                  "field_metadata_fields": ["struct", "enum", "bool_layout"],
                                  "name_field_input_shape": {"name": "string"}, "name_from_fields_supported": False,
                                  "enum_field_underlying_type": "ByteProperty", "enum_field_bytes": 1,
                                  "field_bytes": {"IntProperty": 4, "Int64Property": 8, "UInt32Property": 4,
                                                  "UInt64Property": 8, "FloatProperty": 4, "DoubleProperty": 8,
                                                  "NameProperty": 8, "EnumProperty": 1, "ByteProperty": 1},
                                  "field_names": "exact_reflected_component_names", "bool_layout": "reflected_bool_layout",
                                  "padding": "zero_initialized", "metadata_field": "struct",
                                  "layout_source": "reflected_function_parameter", "lifecycle": "plain_default_copy",
                                  "custom_lifecycle_supported": False, "source_contract_required": True,
                                  "reference_input_supported": False, "out_supported": True, "inout_supported": False,
                                  "output_argument_marker": {"out": True}, "pure_output_only": True,
                                  "output_field": "out_parameters", "output_metadata_field": "out_parameter_metadata",
                                  "output_keys": "reflected_parameter_name",
                                  "output_metadata_schema": "existing_scalar_struct_snapshot",
                                  "nested_fields_supported": True, "string_fields_supported": False,
                                  "return_supported": True, "requires_signature_validation": True},
                "string": {"type": "FString", "input_shape": "string",
                           "passing": "value_or_const_reference", "const_reference_required": False,
                           "runtime_passing": "normalized_const_reference",
                           "runtime_passing_source": "current_unreal_angelscript_function_builder",
                           "max_utf16_units": 2147483646, "max_inputs": None,
                           "max_allocated_input_bytes": None, "embedded_nul_allowed": False,
                           "ownership": "engine_owned_copy", "by_value_input_supported": True,
                           "out_supported": True, "inout_supported": False,
                           "requires_signature_validation": True,
                           "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "string_output": {"type": "FString", "property_type": "StrProperty", "argument_marker": {"out": True},
                                  "direction": "output", "size": 16, "array_dim": 1, "min_alignment": 8,
                                  "pure_output_only": True, "initial_buffer": "empty_string",
                                  "const_supported": False, "reference_supported": False, "inout_supported": False,
                                  "output_field": "out_parameters", "metadata_field": "out_parameter_metadata",
                                  "output_keys": "reflected_parameter_name", "result_shape": "string",
                                  "metadata_schema": {"type": "StrProperty", "element_size": 16, "array_dim": 1},
                                  "layout_source": "saved_reflected_function_parameter", "values_copied": True,
                                  "cleanup_fields": ["cleanup_confirmed", "string_output_cleanup_confirmed"],
                                  "normal_output_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False,
                                  "requires_signature_validation": True,
                                  "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "transform": {"type": "FTransform", "struct_path": "/Script/CoreUObject.Transform",
                              "modes": ["input", "const_reference_input"], "size": 96,
                              "reference_input_supported": True, "const_reference_required": True,
                              "input_shape": {"Rotation": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number", "W": "finite_number"},
                                              "Translation": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number"},
                                              "Scale3D": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number"}},
                              "argument_constraints": {"normalization": "none"},
                              "layout_source": "reflected_function_parameter", "requires_signature_validation": True},
                "scalar_outputs": {"types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                               "UInt64Property", "FloatProperty", "DoubleProperty"],
                                   "argument_marker": {"out": True}, "output_field": "out_parameters",
                                   "metadata_field": "out_parameter_metadata", "pure_output_only": True,
                                   "layout_source": "reflected_function_parameter", "requires_signature_validation": True,
                                   "reference_supported": False, "inout_supported": False},
                "vector": {"type": "FVector", "struct_path": "/Script/CoreUObject.Vector",
                           "modes": ["input", "const_reference_input", "output", "return"],
                           "input_shape": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number"},
                           "output_argument": {"out": True}, "output_field": "out_parameters",
                           "layout_source": "reflected_function_parameter", "requires_signature_validation": True,
                           "reference_input_supported": True, "const_reference_required": True, "inout_supported": False},
            },
            "invoke_returns": {
                "string_array": {"type": "TArray<FString>", "return_type": "ArrayProperty", "passing": "value",
                                 "size": 16, "array_dim": 1, "element_type": "StrProperty", "element_size": 16,
                                 "element_array_dim": 1, "element_offset": 0,
                                 "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                                 "item_fields": ["index", "supported", "value", "error"],
                                 "index_semantics": "array_index", "element_shape": "string", "values_copied": True,
                                 "empty_allowed": True, "empty_strings_allowed": True, "unicode_supported": True,
                                 "order_preserved": True, "duplicates_allowed": True,
                                 "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                                 "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                                 "layout_source": "reflected_function_parameter",
                                 "ownership": "engine_owned_inner_strings_and_outer_array",
                                 "normal_cleanup": "free_inner_strings_and_outer_array",
                                 "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                                 "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False,
                                 "reference_return_supported": False, "const_return_supported": False, "input_supported": True,
                                 "nested_dto_string_fields_supported": False, "requires_signature_validation": True,
                                 "availability": "requires_live_native_status_and_per_invoke_allocator_validation"},
                "basic_scalar_array": {"return_type": "ArrayProperty", "passing": "value", "size": 16, "array_dim": 1,
                                       "element_types": {"BoolProperty": {"element_size": 1, "value_shape": "boolean",
                                                                           "bool_layout": "reflected_bool_layout"},
                                                         "IntProperty": {"element_size": 4, "value_shape": "integer"},
                                                         "Int64Property": {"element_size": 8, "value_shape": "integer"},
                                                         "UInt32Property": {"element_size": 4, "value_shape": "integer"},
                                                         "UInt64Property": {"element_size": 8, "value_shape": "integer"},
                                                         "FloatProperty": {"element_size": 4, "value_shape": "finite_float32"},
                                                         "DoubleProperty": {"element_size": 8, "value_shape": "finite_float64"}},
                                       "element_array_dim": 1, "element_offset": 0,
                                       "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                                       "item_fields": ["index", "supported", "value", "error"], "index_semantics": "array_index",
                                       "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                                       "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                                       "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                                       "bool_element_metadata_field": "bool_layout",
                                       "layout_source": "reflected_function_parameter", "values_copied": True,
                                       "requires_signature_validation": True, "reference_return_supported": False,
                                       "const_return_supported": False, "input_supported": True,
                                       "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                                       "normal_return_cleanup_confirmed": True,
                                       "uncertain_construction_cleanup_confirmed": False},
                "scalar_struct_array": {"type": "TArray<own scalar DTO>", "return_type": "ArrayProperty",
                                        "passing": "value", "size": 16, "array_dim": 1,
                                        "element_type": "StructProperty", "element_size": "reflected_struct_size",
                                        "element_min_alignment": "reflected_struct_min_alignment",
                                        "element_array_dim": 1, "element_offset": 0,
                                        "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                                        "item_fields": ["index", "supported", "value", "error"],
                                        "index_semantics": "array_index", "element_shape": "object", "own_fields_only": True,
                                        "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                        "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
                                                        "EnumProperty", "ByteProperty", "StructProperty"],
                                        "nested_struct_fields": "own_reflected_fields_recursive",
                                        "field_metadata_fields": ["struct", "enum", "bool_layout"],
                                        "name_field_input_shape": {"name": "string"}, "name_from_fields_supported": False,
                                        "enum_field_underlying_type": "ByteProperty", "enum_field_bytes": 1,
                                        "field_bytes": {"IntProperty": 4, "Int64Property": 8, "UInt32Property": 4,
                                                        "UInt64Property": 8, "FloatProperty": 4, "DoubleProperty": 8,
                                                        "NameProperty": 8, "EnumProperty": 1, "ByteProperty": 1},
                                        "bool_layout": "reflected_bool_layout", "lifecycle": "plain_default_copy",
                                        "custom_lifecycle_supported": False, "source_contract_required": True,
                                        "nested_fields_supported": True, "string_fields_supported": False,
                                        "input_supported": True,
                                        "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                                        "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                                        "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "struct"],
                                        "struct_metadata_schema": "existing_scalar_struct_snapshot",
                                        "layout_source": "reflected_function_parameter", "values_copied": True,
                                        "requires_signature_validation": True, "reference_return_supported": False,
                                        "const_return_supported": False,
                                        "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                                        "normal_return_cleanup_confirmed": True,
                                        "uncertain_construction_cleanup_confirmed": False},
                "name_array": {"type": "TArray<FName>", "return_type": "ArrayProperty", "passing": "value",
                               "size": 16, "array_dim": 1, "element_type": "NameProperty", "element_size": 8,
                               "element_array_dim": 1, "element_offset": 0,
                               "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                               "item_fields": ["index", "supported", "value", "error"],
                               "index_semantics": "array_index", "element_shape": "string",
                               "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                               "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                               "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                               "layout_source": "reflected_function_parameter", "values_copied": True,
                               "requires_signature_validation": True, "reference_return_supported": False,
                               "const_return_supported": False,
                               "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                               "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False},
                "enum_array": {"type": "TArray<enum>", "return_type": "ArrayProperty", "passing": "value",
                               "size": 16, "array_dim": 1, "element_type": "EnumProperty", "element_size": 1,
                               "element_array_dim": 1, "element_offset": 0, "underlying_type": "ByteProperty",
                               "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                               "item_fields": ["index", "supported", "value", "enum", "error"],
                               "index_semantics": "array_index", "element_shape": "integer", "integer_range": [0, 255],
                               "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                               "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                               "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "enum"],
                               "enum_fields": ["path", "underlying_type", "entries"],
                               "enum_entry_fields": ["name", "value"],
                               "item_enum_fields": ["path", "underlying_type", "name_status", "value_name"],
                               "layout_source": "reflected_function_parameter", "values_copied": True,
                               "requires_signature_validation": True, "reference_return_supported": False,
                               "const_return_supported": False,
                               "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                               "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False},
                "int_array": {"type": "TArray<int32>", "return_type": "ArrayProperty", "passing": "value",
                              "size": 16, "array_dim": 1, "element_type": "IntProperty", "element_size": 4,
                              "element_array_dim": 1, "element_offset": 0,
                              "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                              "item_fields": ["index", "supported", "value", "error"],
                              "index_semantics": "array_index", "element_shape": "integer",
                              "integer_range": [-2147483648, 2147483647],
                              "empty_allowed": True, "order_preserved": True, "duplicates_allowed": True,
                              "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                              "element_metadata_fields": ["type", "element_size", "array_dim", "offset"],
                              "layout_source": "reflected_function_parameter", "values_copied": True,
                              "requires_signature_validation": True, "reference_return_supported": False,
                              "const_return_supported": False,
                              "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                              "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False},
                "object_array": {"type": "TArray<UObject>", "return_type": "ArrayProperty", "passing": "value",
                                 "size": 16, "array_dim": 1, "element_type": "ObjectProperty", "element_size": 8,
                                 "result_shape": "object", "result_fields": ["items", "total_count", "partial"],
                                 "item_fields": ["index", "supported", "value", "error"],
                                 "index_semantics": "array_index", "element_shape": "object_descriptor_or_null",
                                 "null_object_values_supported": True, "order_preserved": True,
                                 "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                                 "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "declared_type"],
                                 "declared_type_fields": ["name", "path"], "layout_source": "reflected_function_parameter",
                                 "requires_signature_validation": True, "reference_return_supported": False,
                                 "objects_owned": False, "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                                 "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False},
                "object_set": {"type": "TSet<UObject>", "return_type": "SetProperty", "passing": "value",
                               "array_dim": 1, "element_type": "ObjectProperty", "element_size": 8,
                               "result_shape": "object", "result_fields": ["items", "total_count", "max_index", "partial"],
                               "item_fields": ["index", "sparse_index", "supported", "value", "error"],
                               "index_semantics": "occupied_slot_ordinal", "sparse_index_semantics": "physical_slot_index",
                               "element_shape": "object_descriptor_or_null", "null_object_values_supported": True,
                               "metadata_field": "result_metadata", "metadata_fields": ["container_type", "element"],
                               "element_metadata_fields": ["type", "element_size", "array_dim", "offset", "declared_type"],
                               "declared_type_fields": ["name", "path"], "layout_source": "reflected_function_parameter",
                               "requires_signature_validation": True, "reference_return_supported": False,
                               "objects_owned": False, "cleanup_fields": ["cleanup_confirmed", "return_cleanup_confirmed"],
                               "normal_return_cleanup_confirmed": True, "uncertain_construction_cleanup_confirmed": False},
                "rotator": {"type": "FRotator", "return_type": "StructProperty",
                            "struct_path": "/Script/CoreUObject.Rotator", "passing": "value",
                            "size": 24, "min_alignment": 8, "array_dim": 1, "units": "degrees",
                            "result_shape": "object", "output_shape": {"Pitch": "finite_number", "Yaw": "finite_number", "Roll": "finite_number"},
                            "metadata_field": "result_metadata", "layout_source": "reflected_function_parameter",
                            "requires_signature_validation": True, "reference_return_supported": False},
                "transform": {"type": "FTransform", "return_type": "StructProperty",
                              "struct_path": "/Script/CoreUObject.Transform", "passing": "value",
                              "size": 96, "array_dim": 1, "result_shape": "object",
                              "output_shape": {"Rotation": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number", "W": "finite_number"},
                                               "Translation": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number"},
                                               "Scale3D": {"X": "finite_number", "Y": "finite_number", "Z": "finite_number"}},
                              "metadata_field": "result_metadata", "layout_source": "reflected_function_parameter",
                              "requires_signature_validation": True, "reference_return_supported": False},
                "scalar_struct": {"return_type": "StructProperty", "result_shape": "object",
                                  "field_types": ["BoolProperty", "IntProperty", "Int64Property", "UInt32Property",
                                                  "UInt64Property", "FloatProperty", "DoubleProperty", "NameProperty",
                                                  "EnumProperty", "ByteProperty", "StructProperty"],
                                  "nested_struct_fields": "own_reflected_fields_recursive",
                                  "field_metadata_fields": ["struct", "enum", "bool_layout"],
                                  "name_field_input_shape": {"name": "string"}, "name_from_fields_supported": False,
                                  "enum_field_underlying_type": "ByteProperty", "enum_field_bytes": 1,
                                  "field_bytes": {"IntProperty": 4, "Int64Property": 8, "UInt32Property": 4,
                                                  "UInt64Property": 8, "FloatProperty": 4, "DoubleProperty": 8,
                                                  "NameProperty": 8, "EnumProperty": 1, "ByteProperty": 1},
                                  "bool_layout": "reflected_bool_layout", "array_dim": 1,
                                  "metadata_field": "result_metadata", "layout_source": "reflected_function_parameter",
                                  "passing": "value", "own_fields_only": True,
                                  "lifecycle": "plain_default_copy", "custom_lifecycle_supported": False,
                                  "source_contract_required": True,
                                  "input_supported": True, "reference_return_supported": False,
                                  "nested_fields_supported": True, "string_fields_supported": False},
            },
            "hot_reload": {"mechanism_confirmed": True, "method": "file_watch_development_mode",
                           "scope": "function_bodies", "runtime_result_requires_verification": True,
                           "apply_tool": "g1r_apply_staged_file", "apply_source": "canonical_workspace_package",
                           "apply_target": "owned_active_staging_package", "apply_result_proves_compile": False},
            "observations": {"implemented": True, "availability": "requires_live_native_status",
                             "source": "selected_reflected_properties_on_existing_tick",
                             "ownership": "shared_native_connection",
                             "client_authorization_required": False,
                             "connection_lifecycle": "native_tcp_session",
                             "client_disconnect": "continues_until_explicit_stop_or_existing_duration",
                             "reconnect_reassigns_owner": False,
                             "discovery_tool": "g1r_watch_list",
                             "explicit_owner_transfer_tool": "g1r_watch_resume",
                             "discovery_scope": "current_native_connection_metadata",
                             "owner_transfer_identity": "watch_id_session_id_client_owner",
                             "transfer_restarts_observation": False,
                             "native_disconnect_retains_observations": False,
                             "world_generation_supported": True,
                             "world_generation_detection": "requires_live_native_status",
                             "world_generation_policy": "reflected_world_identity",
                             "world_generation_source": "GameEngine.GameViewport.World",
                             "world_generation_initial": "1_on_first_observed_world",
                             "world_generation_changes_on": ["world_identity_changed", "world_unavailable_after_observed"],
                             "world_generation_handle_scope": "bound_world_only",
                             "world_generation_preserves": ["cdo_handles", "asset_handles"],
                             "world_generation_watch_terminal_reason": "world_changed",
                             "script_reload_generation_supported": True,
                             "script_reload_generation_detection": "requires_live_native_status",
                             "script_reload_generation_source": "observed_native_reload_return",
                             "script_reload_generation_initial": 0,
                             "script_reload_generation_changes_on": "successful_native_reload_return",
                             "script_reload_generation_failed_attempt_advances": False,
                             "script_reload_generation_proves_specific_source_compiled": False,
                             "script_reload_generation_handle_scope": "script_bound_only",
                             "script_reload_generation_invalidates": ["angelscript_class_instances", "script_cdo_handles"],
                             "script_reload_generation_preserves": ["native_cdo_handles", "asset_handles"],
                             "script_reload_generation_watch_terminal_reason": "script_reloaded"},
            "movieplayer": {"implemented": True, "status_tool": "g1r_movieplayer_status",
                            "finish_tool": "g1r_movieplayer_finish", "source": "native_movieplayer_cache",
                            "status_requires_game_tick": False,
                            "finish_execution_context": "game_thread_platform_service_inside_active_movieplayer_wait",
                            "finish_arguments": {"expected_wait_id": "wait-N"},
                            "finish_requires_loading_finished": True,
                            "finish_result": "finish_call_completed",
                            "finish_proves_wait_returned": False, "finish_proves_ordinary_tick_ready": False,
                            "arbitrary_calls_supported": False,
                            "status_availability": "requires_native_tcp_channel",
                            "finish_availability": "requires_loaded_movieplayer_hooks_and_matching_wait",
                            "current_checked": False},
            "snapshot_comparison": {"implemented": True, "effects": "read_only",
                                    "tool": "g1r_compare_snapshots", "validation": "client_supplied_copies"},
            "limitations": [
                "Sources do not cover all native bindings and do not confirm calls in the current game",
                "Type declarations are found lexically; they are not a compiler AST or runtime reflection",
                "Package validation does not load the mod or check the loader's version-comparison algorithm",
            ],
        }

    @server.tool(annotations=debug_action)
    @protected_tool
    async def g1r_debug_attach(timeout: float = 5.0) -> dict[str, Any]:
        """Attach one shared debugger connection to the current game's listener."""
        return await asyncio.to_thread(debug_session.attach, timeout)

    @server.tool(annotations=debug_action)
    @protected_tool
    async def g1r_debug_detach() -> dict[str, Any]:
        """Stop debugging and close the shared debugger connection."""
        return await asyncio.to_thread(debug_session.detach)

    @server.tool(annotations=read_only)
    def g1r_debug_status() -> dict[str, Any]:
        """Read the shared debugger connection, execution state and last event."""
        return debug_session.status()

    @server.tool(annotations=debug_action)
    @protected_tool
    async def g1r_debug_breakpoints(path: str, lines: list[int], module: str = "", timeout: float = 5.0) -> dict[str, Any]:
        """Replace breakpoints for one explicit script path and module."""
        return await asyncio.to_thread(debug_session.breakpoints, path, lines, module, timeout)

    @server.tool(annotations=debug_action)
    @protected_tool
    async def g1r_debug_control(action: str, timeout: float = 5.0) -> dict[str, Any]:
        """Pause, continue or step execution using pause, continue, step_over, step_in or step_out."""
        return await asyncio.to_thread(debug_session.control, action, timeout)

    @server.tool(annotations=read_only)
    async def g1r_debug_stack(timeout: float = 5.0) -> dict[str, Any]:
        """Read the AngelScript stack from the shared debugger session."""
        return await asyncio.to_thread(debug_session.stack, timeout)

    @server.tool(annotations=read_only)
    async def g1r_debug_variables(expression: str, timeout: float = 5.0) -> dict[str, Any]:
        """Read variables for a debugger path such as 0:%local%, 0:%this% or a child expression."""
        return await asyncio.to_thread(debug_session.variables, expression, timeout)

    @server.tool(annotations=debug_action)
    @protected_tool
    async def g1r_debug_evaluate(expression: str, frame_id: int, timeout: float = 5.0) -> dict[str, Any]:
        """Evaluate an AngelScript expression in an explicit debugger frame."""
        return await asyncio.to_thread(debug_session.evaluate, expression, frame_id, timeout)

    @server.tool(annotations=read_only)
    async def g1r_debug_database(query: str = "", limit: int = 100, offset: int = 0,
                                 refresh: StrictBool = False, timeout: float = 30.0) -> dict[str, Any]:
        """Read the cached AngelScript debug database; refresh connects once to the current game's debugger listener."""
        if refresh:
            refreshed = await asyncio.to_thread(debug_database.refresh, timeout)
            if not refreshed.get("complete"):
                return refreshed
        return debug_database.query(query, limit, offset)

    @server.tool(annotations=read_only)
    def g1r_source_identity() -> dict[str, Any]:
        """Read Modkit Git HEAD and status; compatibility with the installed game is not checked."""
        return sources.source_identity()

    @server.tool(annotations=read_only)
    def g1r_verify_source_game(hash_executable: StrictBool = False) -> dict[str, Any]:
        """Compare Modkit scripts with game files; matching bytes do not confirm API behavior."""
        try:
            return verify_source_game(sources.root, configured_game_root, hash_executable=hash_executable,
                                      configured_revision=sources.revision)
        except InspectionError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_find_source_files(query: str, limit: int = 50, offset: int = 0,
                              relative_package: str | None = None) -> dict[str, Any]:
        """Find Modkit .as paths or all Script/ScriptPatches paths in relative_package; paginate through next_offset."""
        try:
            if relative_package is not None:
                return workspace.search_source_files(relative_package, query, limit, offset)
            return sources.search_files(query, limit, offset)
        except (SourceError, WorkspaceError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_search_source(query: str, limit: int = 50, case_sensitive: bool = False, offset: int = 0,
                         relative_package: str | None = None) -> dict[str, Any]:
        """Search literal lines in Modkit .as files or all Script/ScriptPatches files in relative_package."""
        try:
            if relative_package is not None:
                return workspace.search_source_text(relative_package, query, limit, case_sensitive, offset)
            return sources.search_text(query, limit, case_sensitive=case_sensitive, offset=offset)
        except (SourceError, WorkspaceError) as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_read_source(relative_path: str, start_line: int = 1, line_count: int = 100) -> dict[str, Any]:
        """Read a source-line range using a relative path within an allowed directory."""
        try:
            return sources.read_file(relative_path, start_line, line_count)
        except SourceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_list_source_types(query: str = "", limit: int = 50, offset: int = 0,
                              relative_path: str | None = None) -> dict[str, Any]:
        """Find class/struct/enum declarations globally or in a selected .as file or directory; results are lexical."""
        try:
            return sources.list_types(query, limit, offset, relative_path=relative_path)
        except SourceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_list_source_functions(query: str = "", relative_path: str | None = None,
                                  limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """Find source-function prototypes lexically; lines and SHA-256 do not confirm a runtime signature."""
        try:
            return sources.list_functions(query, relative_path, limit, offset)
        except SourceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_validate_package(relative_path: str) -> dict[str, Any]:
        """Validate GothicMod.json and workspace-package structure without loading it into the game."""
        try:
            return validate_package(workspace_root, relative_path)
        except PackageError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_create_package(relative_path: str, metadata: dict[str, Any]) -> dict[str, Any]:
        """Create GothicMod.json/Script: metadata requires name, title, author, and modVersion; does not load the mod."""
        try:
            return workspace.create_package(relative_path, metadata)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False))
    @protected_tool
    def g1r_create_package_directory(relative_path: str, directory_path: str) -> dict[str, Any]:
        """Create allowed Script, ScriptPatches, Config, Content, or Story directories inside a package; preserve an existing directory."""
        try:
            return workspace.create_package_directory(relative_path, directory_path)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_list_package_files(relative_path: str, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        """List descriptor and Script, ScriptPatches, Config, Content, or Story files regardless of encoding; continue through next_offset."""
        try:
            return workspace.list_package_files(relative_path, limit, offset)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_read_package_file(relative_path: str, file_path: str, start_line: int = 1,
                              line_count: int = 100) -> dict[str, Any]:
        """Read a UTF-8 descriptor or Script, ScriptPatches, Config, Content, or Story file and its complete-byte SHA-256."""
        try:
            return workspace.read_package_file(relative_path, file_path, start_line, line_count)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_write_package_file(relative_path: str, file_path: str, content: str,
                               expected_sha256: str | None = None) -> dict[str, Any]:
        """Write UTF-8 descriptor or component text; replacement requires SHA-256, while omitting the hash creates only a new file."""
        try:
            return workspace.write_package_file(relative_path, file_path, content, expected_sha256)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_export_package(relative_package: str, destination: str) -> dict[str, Any]:
        """Export the complete package to a new workspace artifacts directory with ZIP and manifest; does not install or load it."""
        try:
            target = workspace_root.joinpath(*_parts(destination))
            return export_package(workspace_root, relative_package, target,
                                  modkit_root=modkit_root, game_root=configured_game_root)
        except (ExportError, WorkspaceError, PackageError) as error:
            raise _tool_failure(error) from None
        except OSError:
            raise ToolError(json.dumps({"code": "export_io_error", "message": "Operating system access failed while exporting the package"})) from None

    @server.tool(annotations=read_only)
    def g1r_verify_package_export(relative_path: str) -> dict[str, Any]:
        """Verify the ZIP and SHA-256 manifest in a workspace export directory without accessing the game."""
        try:
            target = _checked(workspace_root.joinpath(*_parts(relative_path)), workspace_root, directory=True)
            return verify_export(target)
        except (ExportError, WorkspaceError, PackageError) as error:
            raise _tool_failure(error) from None
        except OSError:
            raise ToolError(json.dumps({"code": "export_io_error", "message": "Operating system access failed while verifying the package export"})) from None

    @server.tool(annotations=read_only)
    def g1r_build_script_patch(source_path: str, replacement_content: str,
                                expected_source_sha256: str, context_lines: int = 3) -> dict[str, Any]:
        """Create a unified diff from verified Modkit source bytes without changing files."""
        try:
            return patch_builder.build_script_patch(source_path, replacement_content,
                                                    expected_source_sha256, context_lines)
        except PatchError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_read_source_exact(relative_path: str) -> dict[str, Any]:
        """Read complete UTF-8 source for editing without an artificial limit; preserve BOM, CRLF, and EOF."""
        try:
            return sources.read_file_exact(relative_path)
        except SourceError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_read_package_file_exact(relative_path: str, file_path: str) -> dict[str, Any]:
        """Read complete UTF-8 descriptor or official component text without changing BOM, line endings, or EOF."""
        try:
            return workspace.read_package_file_exact(relative_path, file_path)
        except WorkspaceError as error:
            raise _tool_failure(error) from None

    def native_request(operation: str, params: dict[str, Any],
                       timeout_seconds: float | None = None) -> dict[str, Any]:
        try:
            with native_lock:
                client = native
            return client.request(operation, params) if timeout_seconds is None else client.request(
                operation, params, timeout=timeout_seconds)
        except NativeError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                                       "may_have_executed": error.may_have_executed,
                                       "request_id": error.request_id, "effect": error.details}, ensure_ascii=False)) from None

    def development_request(operation: str, *, ctx: Context | None = None, session_reason: str = "Workspace package verification", **params: Any) -> dict[str, Any]:
        nonlocal native
        try:
            result = getattr(development_service(), operation)(**params)
            if transport == "streamable-http":
                owner = request_owner(ctx)
                if operation == "load_native":
                    ownership.bind(owner, result["process"], "native_loaded")
                elif operation == "start" and result.get("state") == "launch_requested" and result.get("outcome_unknown") is False:
                    ownership.acquire(owner, session_reason, package=params.get("relative_package"))
                    ownership.launched(owner, params["relative_package"])
                elif operation == "stop" and result.get("outcome_unknown") is False and result.get("tool_result", {}).get("state") == "stopped":
                    result["session_released"] = ownership.clear_information()
            stopped = result.get("tool_result")
            if ((operation == "start" and result.get("state") == "launch_requested"
                 and result.get("outcome_unknown") is False)
                    or (operation == "stop" and result.get("state") == "stop_result"
                    and result.get("outcome_unknown") is False and isinstance(stopped, dict)
                    and stopped.get("state") == "stopped"
                    and isinstance(result.get("process"), dict)
                    and stopped.get("process") == result["process"]
                    and isinstance(params.get("expected_process"), dict)
                    and result["process"] == {key: params["expected_process"].get(key)
                                              for key in ("pid", "creation_time", "executable")})):
                with native_lock:
                    previous_native = native
                    native = NativeClient(port=native_port)
                previous_native.close()
            return result
        except DevelopmentError as error:
            payload = {"code": error.code, "message": str(error),
                       "outcome_unknown": error.outcome_unknown,
                       "retry": "only_explicit_new_request"}
            if error.receipt is not None:
                payload["receipt"] = error.receipt
                payload["do_not_retry"] = error.receipt["do_not_retry"]
            raise ToolError(json.dumps(payload, ensure_ascii=False)) from None

    @server.tool(annotations=read_only)
    def g1r_session_status(ctx: Context = None) -> dict[str, Any]:
        """Read shared session information and any explicitly selected tool protection."""
        return ownership.status(request_owner(ctx))

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    def g1r_session_acquire(reason: str, expected_process: dict[str, Any] | None = None,
                            expected_claim_id: str | None = None, protected_tools: list[str] | None = None,
                            takeover: bool = False, ctx: Context = None) -> dict[str, Any]:
        """Record session information, or explicitly protect selected mutating tools; takeover replaces another client's protection."""
        try:
            if protected_tools is not None:
                if not isinstance(protected_tools, list) or not protected_tools or any(
                    not isinstance(name, str) or name not in PROTECTABLE_TOOLS for name in protected_tools
                ):
                    raise DevelopmentError("invalid_protected_tools",
                                           "Select a non-empty list from g1r_capabilities.parallel_work.protectable_tools; use g1r_session_release to clear protection")
            owner = request_owner(ctx)
            return ownership.acquire(owner, reason, process=expected_process,
                                     expected_claim_id=expected_claim_id,
                                     protected_tools=protected_tools, takeover=takeover)
        except DevelopmentError as error:
            raise protection_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    def g1r_session_release(expected_claim_id: str | None = None, ctx: Context = None) -> dict[str, Any]:
        """Explicitly clear session information and protection from any client without stopping the game."""
        try:
            return ownership.release(request_owner(ctx), expected_claim_id=expected_claim_id)
        except DevelopmentError as error:
            raise protection_failure(error) from None

    @server.tool(annotations=read_only)
    def g1r_compile_diagnostics(relative_package: str, expected_manifest_sha256: str | None = None, report_path: str | None = None,
                                enable_ingame_tools: StrictBool = False,
                                show_script_errors: StrictBool = False) -> dict[str, Any]:
        """Get native-compiler diagnostics; report_path selects a supplied UTF-8 report."""
        try:
            if report_path is None:
                return read_runtime_report(development_service(), relative_package, expected_manifest_sha256,
                                           lambda: native_request("compile_diagnostics", {}),
                                           enable_ingame_tools=enable_ingame_tools,
                                           show_script_errors=show_script_errors)
            return read_report(development_service(), relative_package, expected_manifest_sha256, report_path,
                               enable_ingame_tools, show_script_errors=show_script_errors)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error), "outcome_unknown": False}, ensure_ascii=False)) from None

    @server.tool(annotations=read_only)
    def g1r_development_recipe(relative_package: str, enable_ingame_tools: StrictBool = False,
                               companion_packages: list[str] | None = None,
                               show_script_errors: StrictBool = False) -> dict[str, Any]:
        """Check the complete workspace-package manifest, processes, and DLL SHA-256 without starting the game."""
        return development_request("recipe", relative_package=relative_package,
                                   enable_ingame_tools=enable_ingame_tools, companion_packages=companion_packages,
                                   show_script_errors=show_script_errors)

    @server.tool(annotations=read_only)
    def g1r_development_processes() -> dict[str, Any]:
        """Read exact PIDs, creation times, and process paths for the selected game."""
        return development_request("process_state")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_apply_staged_file(relative_package: str, file_path: str,
                               expected_source_sha256: str, expected_staged_sha256: str,
                               expected_process: dict[str, Any], ctx: Context = None) -> dict[str, Any]:
        """Apply one canonical package file to its owned active staging copy using both SHA-256 hashes; verify compilation and runtime behavior separately."""
        return development_request("apply_staged_file", relative_package=relative_package,
                                   file_path=file_path, expected_source_sha256=expected_source_sha256,
                                   expected_staged_sha256=expected_staged_sha256,
                                   expected_process=expected_process, ctx=ctx)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False,
        destructiveHint=False, idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_development_key(expected_process: dict[str, Any], virtual_key: StrictInt,
                           expected_window: StrictInt | None = None,
                           ctx: Context = None) -> dict[str, Any]:
        """Queue one key for an exact game window without Tick; game handling of the key must be verified separately."""
        return development_request("queue_key", expected_process=expected_process,
                                   virtual_key=virtual_key, expected_window=expected_window,
                                   ctx=ctx)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=False, openWorldHint=True))
    @protected_tool
    def g1r_development_start(relative_package: str, expected_manifest_sha256: str,
                               restart: bool = False,
                             expected_process: dict[str, Any] | None = None,
                             enable_ingame_tools: StrictBool = False, session_reason: str = "Workspace package verification",
                             companion_packages: list[str] | None = None, ctx: Context = None,
                             show_script_errors: StrictBool = False) -> dict[str, Any]:
        """Request a development launch of a verified package; restart requires the exact process identity."""
        return development_request("start", relative_package=relative_package,
                                   expected_manifest_sha256=expected_manifest_sha256,
                                   restart=restart, expected_process=expected_process,
                                   enable_ingame_tools=enable_ingame_tools, companion_packages=companion_packages,
                                   show_script_errors=show_script_errors,
                                   ctx=ctx, session_reason=session_reason)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True,
                                            idempotentHint=False, openWorldHint=True))
    @protected_tool
    def g1r_development_stop(expected_process: dict[str, Any], reason: str = "Verification complete", ctx: Context = None) -> dict[str, Any]:
        """Close only the confirmed game instance using retained HANDLEs."""
        if not reason or any(ord(c) < 32 for c in reason):
            raise ToolError(json.dumps({"code":"invalid_stop_reason","message":"A nonempty stop reason without control characters is required","outcome_unknown":False}, ensure_ascii=False))
        result = development_request("stop", expected_process=expected_process, ctx=ctx)
        result["stop_reason"] = reason
        return result

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=True))
    @protected_tool
    def g1r_native_load(expected_process: dict[str, Any], expected_dll_sha256: str, ctx: Context = None) -> dict[str, Any]:
        """Load a DLL into a verified process with hash validation; do not retry an unknown outcome."""
        return development_request("load_native", expected_process=expected_process,
                                   expected_dll_sha256=expected_dll_sha256, ctx=ctx)

    @server.tool(annotations=read_only)
    def g1r_native_status() -> dict[str, Any]:
        """Check the DLL in the current game; does not start the game or loader."""
        return native_request("status", {})

    @server.tool(annotations=read_only)
    def g1r_movieplayer_status() -> dict[str, Any]:
        """Read a consistent copy of MoviePlayer status; ordinary game Tick is not required."""
        return native_request("movieplayer_status", {})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_movieplayer_finish(expected_wait_id: StrictStr, ctx: Context = None,
                                timeout_seconds: float | None = None) -> dict[str, Any]:
        """Explicitly request completion of an exact wait; the finish call, Wait return, and game Tick are distinct."""
        try:
            return native_request("movieplayer_finish", {"expected_wait_id": expected_wait_id}, timeout_seconds)
        except DevelopmentError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error),
                                       "outcome_unknown": error.outcome_unknown,
                                       "retry": "only_explicit_new_request"}, ensure_ascii=False)) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False))
    @protected_tool
    def g1r_native_enable() -> dict[str, Any]:
        """Install a Tick hook in an already loaded DLL; run after the main menu appears."""
        return native_request("enable_game", {})

    @server.tool(annotations=read_only)
    def g1r_find_objects(query: str = "", class_query: str = "", limit: int = 50,
                         exact_path: str = "", timeout_seconds: float | None = None) -> dict[str, Any]:
        """Find UObject by partial name/class on game Tick; return opaque handles."""
        params = {"query": query, "class_query": class_query, "limit": limit}
        if exact_path:
            params["exact_path"] = exact_path
        return native_request("objects", params, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_object_properties(handle: str, limit: int = 50,
                              property_names: list[StrictStr] | None = None,
                              timeout_seconds: float | None = None) -> dict[str, Any]:
        """Read handle properties; property_names selects exact unique names in the specified order."""
        params = {"handle": handle, "limit": limit}
        if property_names is not None:
            params["property_names"] = property_names
        return native_request("properties", params, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_read_array(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                        timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy an array page, including owned reflected struct fields with per-field support/errors; pages do not share an atomic snapshot."""
        return native_request("read_array", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_read_struct(handle: str, property_name: str, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy owned reflected fields with per-field support/errors and known nested Vector, Rotator, Transform, LinearColor, and Quat layouts."""
        return native_request("read_struct", {"handle": handle, "property_name": property_name}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_read_set(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Set<Name> page; offset counts occupied slots, items retain sparse_index, and pages do not share an atomic snapshot."""
        return native_request("read_set", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_read_map(handle: str, property_name: str, offset: int = 0, limit: int = 64,
                     timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy a Map<Name,Object> page with object descriptors or null; offset counts occupied slots and pages do not share an atomic snapshot."""
        return native_request("read_map", {"handle": handle, "property_name": property_name, "offset": offset, "limit": limit}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_object_functions(handle: str, limit: int = 50, function_query: str = "",
                              timeout_seconds: float | None = None) -> dict[str, Any]:
        """Get UFunction, parameters, declared classes, and enums with allowed values."""
        params = {"handle": handle, "limit": limit}
        if function_query:
            params["function_query"] = function_query
        return native_request("functions", params, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_snapshot(targets: list[dict[str, Any]], timeout_seconds: float | None = None) -> dict[str, Any]:
        """Copy targets={handle, properties:[names]} on one Tick; reads are sequential."""
        return native_request("snapshot", {"targets": targets}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_compare_snapshots(before: dict[str, Any], after: dict[str, Any],
                              limit: int = 100) -> dict[str, Any]:
        """Compare copies with the selected limit; does not verify the original runtime or provenance of client data."""
        try:
            return compare_snapshots(before, after, limit)
        except SnapshotError as error:
            raise _tool_failure(error) from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_watch_start(handle: str, properties: list[str], duration_ms: int,
                         interval_ms: int, capacity: int = 32, ctx: Context = None,
                         timeout_seconds: float | None = None) -> dict[str, Any]:
        """Observe selected properties on Tick with explicit duration, interval, and buffer capacity."""
        return native_request("watch_start", {"handle": handle, "properties": properties,
                              "duration_ms": duration_ms, "interval_ms": interval_ms,
                              "capacity": capacity, "client_owner": watch_owner(ctx)}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_watch_read(watch_id: str, after_seq: int = 0, limit: int = 100,
                       ctx: Context = None, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Read events after seq without deleting them; gaps and overflow are explicitly reported."""
        return native_request("watch_read", {"watch_id": watch_id, "after_seq": after_seq,
                              "limit": limit, "client_owner": watch_owner(ctx)}, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=True, openWorldHint=False))
    @protected_tool
    def g1r_watch_stop(watch_id: str, ctx: Context = None,
                        timeout_seconds: float | None = None) -> dict[str, Any]:
        """Stop subsequent reads on the game thread; retain final events."""
        return native_request("watch_stop", {"watch_id": watch_id, "client_owner": watch_owner(ctx)}, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_watch_forget(watch_id: str, ctx: Context = None,
                          timeout_seconds: float | None = None) -> dict[str, Any]:
        """Release an already completed observation in the current session."""
        return native_request("watch_forget", {"watch_id": watch_id, "client_owner": watch_owner(ctx)}, timeout_seconds)

    @server.tool(annotations=read_only)
    def g1r_watch_list(ctx: Context = None, timeout_seconds: float | None = None) -> dict[str, Any]:
        """List observation metadata for the current native connection without events or values."""
        return native_request("watch_list", {"client_owner": watch_owner(ctx)}, timeout_seconds)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                                            idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_watch_resume(watch_id: str, expected_session_id: str | None = None,
                        expected_client_owner: str | None = None, ctx: Context = None,
                         timeout_seconds: float | None = None) -> dict[str, Any]:
        """Update optional observation client metadata while preserving its state and events."""
        params = {"watch_id": watch_id, "client_owner": watch_owner(ctx)}
        if expected_session_id is not None:
            params["expected_session_id"] = expected_session_id
        if expected_client_owner is not None:
            params["expected_client_owner"] = expected_client_owner
        return native_request("watch_resume", params, timeout_seconds)

    save_loading = SaveLoading(native_request)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    @protected_tool
    async def g1r_save_list(profile_id: int = 2) -> dict[str, Any]:
        """Select a profile in the existing game menu and list saves without writing save files."""
        try:
            return await asyncio.to_thread(save_loading.list_saves, profile_id)
        except SaveLoadingError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error), "details": error.details})) from None
        except OSError as error:
            raise ToolError(f"Save listing file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})") from None

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    @protected_tool
    async def g1r_save_load(slot_name: str, profile_id: int = 2,
                            accept_mod_mismatch: bool = True, timeout: float = 120.0) -> dict[str, Any]:
        """Load a save from g1r_save_list; continue a mod mismatch and confirm a live player."""
        try:
            return await save_loading.load(profile_id, slot_name, accept_mod_mismatch, timeout)
        except SaveLoadingError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error), "details": error.details})) from None
        except OSError as error:
            raise ToolError(f"Save loading file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})") from None

    @server.tool(annotations=read_only)
    async def g1r_save_status() -> dict[str, Any]:
        """Read the last load; after it returns, refresh current world readiness without loading again."""
        return await asyncio.to_thread(save_loading.status)

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False))
    @protected_tool
    async def g1r_modlist_dialog(action: str = "status", process_id: int | None = None) -> dict[str, Any]:
        """Read the native mod-list dialog outside Tick; action=continue publishes Continue anyway."""
        if process_id is None:
            state = await asyncio.to_thread(development_request, "process_state")
            processes = [item for item in state["processes"]
                         if "\\binaries\\win64\\" in item["executable"].replace("/", "\\").lower()]
            if len(processes) != 1:
                raise ToolError("One running game process is required to read the mod-list dialog")
            process_id = processes[0]["pid"]
        try:
            if action == "status":
                return await asyncio.to_thread(modlist_dialog.status, process_id)
            if action == "continue":
                return await asyncio.to_thread(modlist_dialog.accept_continue, process_id)
        except modlist_dialog.ModlistDialogError as error:
            raise ToolError(json.dumps({"code": error.code, "message": str(error), "details": error.details})) from None
        except OSError as error:
            raise ToolError(f"Mod-list dialog file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})") from None
        raise ToolError("Unknown mod-list dialog action")

    @server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False))
    @protected_tool
    def g1r_invoke(handle: str, function: str, arguments: dict[str, Any] | None = None,
                    expected_signature_token: StrictStr | None = None,
                    timeout_seconds: float | None = None) -> dict[str, Any]:
        """Invoke UFunction by signature; a recorded AS runtime error is returned as angelscript_exception. Do not retry an unknown effect."""
        params = {"handle": handle, "function": function, "arguments": arguments or {}}
        if expected_signature_token is not None:
            params["expected_signature_token"] = expected_signature_token
        return native_request("invoke", params, timeout_seconds)

    return server


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage: ", "Usage: ", 1).replace("options:", "Options:")

        def error(self, message):
            self.exit(2, "Invalid G1R MCP arguments; use --help\n")

    parser = RussianArgumentParser(description="G1R MCP: local mod-development server", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--modkit-root", type=Path, required=True, help="Official Modkit root")
    parser.add_argument("--workspace-root", type=Path, required=True, help="Root of packages under development")
    parser.add_argument("--source-revision", help="Full Modkit Git SHA; revision is unknown when omitted")
    parser.add_argument("--native-port", type=int, default=27123, help="Localhost port of the experimental DLL")
    parser.add_argument("--game-root", type=Path, help="Selected game root for development-launch commands")
    parser.add_argument("--runtime-root", type=Path, help="Root of native DLL, loader, and fixed development tools")
    parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio",
                        help="MCP transport; default: stdio")
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1",
                        help="HTTP server address; only 127.0.0.1 is allowed")
    parser.add_argument("--port", type=int, default=27124, help="Streamable HTTP port; default: 27124")
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("HTTP port must be in the range 1024–65535")
    try:
        server = create_server(args.modkit_root, args.workspace_root, args.source_revision, args.native_port, args.game_root,
                               transport=args.transport, runtime_root=args.runtime_root)
    except OSError as error:
        print(f"Could not create G1R MCP: operating system access error (errno={error.errno}, winerror={getattr(error, 'winerror', None)})", file=sys.stderr)
        return 2
    except (ValueError, SourceError, NativeError) as error:
        print(f"Could not create G1R MCP: {error}", file=sys.stderr)
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
