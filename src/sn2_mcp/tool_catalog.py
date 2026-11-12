"""Registered MCP-tool contracts without reading runtime state."""

from copy import deepcopy
from typing import Any, Iterable


PROTECTABLE_TOOLS = (
    "sn2_create_package", "sn2_create_package_directory", "sn2_write_package_file", "sn2_export_package",
    "sn2_development_start", "sn2_development_stop", "sn2_development_key", "sn2_native_load", "sn2_apply_staged_file",
    "sn2_native_enable", "sn2_movieplayer_finish", "sn2_invoke",
    "sn2_watch_start", "sn2_watch_stop", "sn2_watch_forget", "sn2_watch_resume",
    "sn2_debug_attach", "sn2_debug_detach", "sn2_debug_breakpoints", "sn2_debug_control", "sn2_debug_evaluate",
    "sn2_save_list", "sn2_save_load", "sn2_modlist_dialog",
)


# The fields below are an incomplete list of known successful-response fields,
# not a new required schema. Schemas and arguments come only from MCP registration.
_SOURCE_PAGE = ("items", "truncated", "total_matches", "source", "validation", "offset", "next_offset")
_WORKSPACE_FILE = ("source", "package_path", "file_path", "sha256", "text", "compiled", "runtime_validation")
_NATIVE_STATUS = ("version", "ready", "hook_installed", "tick_count", "requests", "pump")
_WATCH = ("watch_id", "session_id", "client_owner", "started_ms", "state", "reason", "cleanup_complete",
          "next_seq", "last_seq", "oldest_seq", "dropped_events", "capacity", "buffer_bytes", "buffer_limit_bytes")


def _entry(target: str, requires: tuple[str, ...], implementation: str,
           fields: tuple[str, ...], *, dynamic: bool = False,
           state_tools: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"target": target, "requires": requires, "implementation": implementation,
            "fields": fields, "dynamic": dynamic, "state_tools": state_tools}


_CONTRACTS = {
    "sn2_save_list": _entry("game_profile_saved_games", ("live_game_menu",), "src/sn2_mcp/save_loading.py:SaveLoading.list_saves", ("profile_id", "items", "total_count", "profile_selected"), dynamic=True),
    "sn2_save_load": _entry("game_save_loading", ("live_game_menu",), "src/sn2_mcp/save_loading.py:SaveLoading.load", ("state", "profile_id", "slot_name", "world_loaded", "warnings"), dynamic=True),
    "sn2_save_status": _entry("shared_save_loading_result", (), "src/sn2_mcp/save_loading.py:SaveLoading.status", ("state", "load_requested"), dynamic=True),
    "sn2_modlist_dialog": _entry("native_slate_modlist_dialog", ("game_process",), "src/sn2_mcp/modlist_dialog.py", ("status", "active", "sent", "confirmed"), dynamic=True),
    "sn2_debug_attach": _entry("angelscript_debugger_session", ("live_debugger_listener",), "src/sn2_mcp/debug_session.py:DebugSession.attach", ("connected", "paused", "last_event"), dynamic=True),
    "sn2_debug_detach": _entry("angelscript_debugger_session", (), "src/sn2_mcp/debug_session.py:DebugSession.detach", ("connected", "resume_requested"), dynamic=True),
    "sn2_debug_status": _entry("angelscript_debugger_metadata", (), "src/sn2_mcp/debug_session.py:DebugSession.status", ("connected", "paused", "last_event"), dynamic=True),
    "sn2_debug_breakpoints": _entry("angelscript_breakpoints", ("debugger_session",), "src/sn2_mcp/debug_session.py:DebugSession.breakpoints", ("path", "module", "breakpoints"), dynamic=True),
    "sn2_debug_control": _entry("angelscript_debugger_execution", ("debugger_session",), "src/sn2_mcp/debug_session.py:DebugSession.control", ("action", "sent", "confirmed", "status"), dynamic=True),
    "sn2_debug_stack": _entry("angelscript_call_stack", ("debugger_session",), "src/sn2_mcp/debug_session.py:DebugSession.stack", ("frames", "total_count"), dynamic=True),
    "sn2_debug_variables": _entry("angelscript_frame_variables", ("debugger_session",), "src/sn2_mcp/debug_session.py:DebugSession.variables", ("expression", "variables"), dynamic=True),
    "sn2_debug_evaluate": _entry("angelscript_frame_expression", ("debugger_session",), "src/sn2_mcp/debug_session.py:DebugSession.evaluate", ("expression", "frame_id", "name", "value", "type", "has_members"), dynamic=True),
    "sn2_debug_database": _entry("angelscript_debug_database", ("cached_database_or_live_debugger_listener",),
                                    "src/sn2_mcp/debug_database.py:DebugDatabaseCatalog",
                                    ("complete", "items", "total_matches", "offset", "next_offset", "source"),
                                    dynamic=True, state_tools=("sn2_status",)),
    "sn2_status": _entry("server_metadata", (), "src/sn2_mcp/server.py:sn2_status",
                          ("server", "sources", "workspace", "game_session", "game")),
    "sn2_capabilities": _entry("registered_tool_metadata", (), "src/sn2_mcp/server.py:sn2_capabilities",
                                ("catalog_revision", "tool_contracts", "parallel_work", "local_tools", "native_tools", "workspace_tools")),
    "sn2_source_identity": _entry("modkit_git_metadata", ("configured_modkit",),
                                   "src/sn2_mcp/source_identity.py:inspect",
                                   ("inspected_at", "snapshot_kind", "source_only", "git", "game_compatibility_checked")),
    "sn2_verify_source_game": _entry("source_and_installed_game_bytes", ("configured_modkit", "installed_game_files"),
                                      "src/sn2_mcp/game_environment.py:verify_source_game",
                                      ("ok", "manifest", "scripts", "source_identity", "inspection_finished_at")),
    "sn2_find_source_files": _entry("modkit_or_workspace_package_source_files", ("configured_modkit_or_workspace",),
                                     "src/sn2_mcp/sources.py:SourceCatalog.search_files; src/sn2_mcp/workspace.py:PackageWorkspace.search_source_files", _SOURCE_PAGE),
    "sn2_search_source": _entry("modkit_or_workspace_package_source_text", ("configured_modkit_or_workspace",),
                                 "src/sn2_mcp/sources.py:SourceCatalog.search_text; src/sn2_mcp/workspace.py:PackageWorkspace.search_source_text", _SOURCE_PAGE),
    "sn2_read_source": _entry("modkit_source_text", ("configured_modkit",),
                               "src/sn2_mcp/sources.py:SourceCatalog.read_file", ("items", "path", "text", "sha256", "source")),
    "sn2_read_source_exact": _entry("modkit_source_bytes", ("configured_modkit",),
                                     "src/sn2_mcp/sources.py:SourceCatalog.read_file_exact",
                                     ("items", "path", "text", "sha256", "contains_bom", "source")),
    "sn2_list_source_types": _entry("lexical_source_types", ("configured_modkit",),
                                     "src/sn2_mcp/sources.py:SourceCatalog.list_types", _SOURCE_PAGE),
    "sn2_list_source_functions": _entry("lexical_source_functions", ("configured_modkit",),
                                         "src/sn2_mcp/sources.py:SourceCatalog.list_functions", _SOURCE_PAGE + ("parser_kind",)),
    "sn2_validate_package": _entry("workspace_package_structure", ("workspace_package",),
                                    "src/sn2_mcp/packages.py:validate_package",
                                    ("package_path", "valid_json", "valid_structure", "compiled", "runtime_validation", "metadata")),
    "sn2_create_package": _entry("workspace_package_files", ("configured_workspace", "windows_filesystem"),
                                  "src/sn2_mcp/workspace.py:PackageWorkspace.create_package",
                                  ("package_path", "created", "files", "directories", "sha256", "validation")),
    "sn2_create_package_directory": _entry("workspace_package_directories", ("workspace_package",),
                                            "src/sn2_mcp/workspace.py:PackageWorkspace.create_package_directory",
                                            ("package_path", "directory_path", "created", "created_paths", "created_count", "exists")),
    "sn2_list_package_files": _entry("workspace_package_files", ("workspace_package",),
                                      "src/sn2_mcp/workspace.py:PackageWorkspace.list_package_files",
                                      ("package_path", "items", "total", "offset", "next_offset", "has_more")),
    "sn2_read_package_file": _entry("workspace_package_text", ("workspace_package",),
                                     "src/sn2_mcp/workspace.py:PackageWorkspace.read_package_file", _WORKSPACE_FILE),
    "sn2_read_package_file_exact": _entry("workspace_package_bytes", ("workspace_package",),
                                           "src/sn2_mcp/workspace.py:PackageWorkspace.read_package_file_exact", _WORKSPACE_FILE),
    "sn2_write_package_file": _entry("workspace_package_files", ("workspace_package", "expected_file_hash_or_creation"),
                                      "src/sn2_mcp/workspace.py:PackageWorkspace.write_package_file",
                                      ("package_path", "file_path", "written", "created", "sha256", "bytes", "validation")),
    "sn2_export_package": _entry("official_package_export", ("workspace_package", "workspace_export_destination"),
                                   "src/sn2_mcp/package_export.py:export_package",
                                   ("destination", "archive", "archive_sha256", "manifest_sha256", "file_count",
                                    "total_source_bytes", "compiled", "installed", "runtime_validation")),
    "sn2_verify_package_export": _entry("official_package_export_archive", ("local_export_directory",),
                                          "src/sn2_mcp/package_export.py:verify_export",
                                          ("package_name", "file_count", "archive_sha256", "verified_files",
                                           "compiled", "installed", "runtime_validation")),
    "sn2_build_script_patch": _entry("returned_patch_text", ("configured_modkit",),
                                      "src/sn2_mcp/patches.py:ScriptPatchBuilder.build_script_patch",
                                      ("source", "path", "source_sha256", "replacement_sha256", "patch_text", "compiled")),
    "sn2_snapshot_compare": _entry("client_supplied_snapshot_copies", ("caller_supplied_snapshot_copies",),
                                     "src/sn2_mcp/snapshots.py:compare_snapshots",
                                     ("source", "validation", "provenance", "comparable", "items", "coverage", "truncated")),
    "sn2_session_status": _entry("optional_development_session_metadata", (), "src/sn2_mcp/session_ownership.py:SessionOwnership.status",
                                  ("claimed", "claim", "protection", "protected_tools", "caller_owns_protection", "scope", "control_required",
                                   "ownership_enforced", "disconnect_releases_claim", "automatic_game_stop")),
    "sn2_session_acquire": _entry("optional_development_session_metadata", (),
                                    "src/sn2_mcp/session_ownership.py:SessionOwnership.acquire",
                                    ("claimed", "claim", "protection", "protected_tools", "caller_owns_protection")),
    "sn2_session_release": _entry("optional_development_session_metadata", (),
                                   "src/sn2_mcp/session_ownership.py:SessionOwnership.release",
                                   ("released", "previous_claim", "previous_protection", "game_stopped")),
    "sn2_compile_diagnostics": _entry("compiler_diagnostic_text", ("workspace_package",
                                                                "supplied_report_file_or_native_channel_and_angelscript_manager"),
                                       "src/sn2_mcp/compile_diagnostics.py:read_runtime_report/read_report",
                                       ("package_name", "manifest_sha256", "diagnostics", "compiled"),
                                       state_tools=("sn2_native_status",)),
    "sn2_development_recipe": _entry("package_and_process_metadata", ("workspace_package", "configured_game", "windows"),
                                       "src/sn2_mcp/development.py:DevelopmentService.recipe",
                                       ("manifest", "process_state", "native_artifact", "launch", "compiled")),
    "sn2_development_processes": _entry("windows_process_snapshot", ("configured_game", "windows"),
                                          "src/sn2_mcp/development.py:DevelopmentService.process_state",
                                          ("processes", "running", "source", "launch_observation")),
    "sn2_development_start": _entry("game_process_and_development_staging", ("workspace_package", "expected_manifest_hash",
                                                                          "windows", "exact_process_if_restart"),
                                      "src/sn2_mcp/development.py:DevelopmentService.start",
                                      ("state", "manifest_sha256", "package_name", "tool_result", "compiled", "outcome_unknown"),
                                      state_tools=("sn2_development_processes",)),
    "sn2_apply_staged_file": _entry("active_staging_file", ("workspace_package", "windows", "exact_expected_process"),
                                     "src/sn2_mcp/development.py:DevelopmentService.apply_staged_file",
                                     ("package_path", "file_path", "written", "created", "bytes", "sha256", "validation",
                                      "source_package_path", "staged_package_path", "source_sha256", "staged_before_sha256",
                                      "process", "launch_id", "source_modified", "hot_reload", "compiled", "runtime_validation"),
                                     state_tools=("sn2_development_processes",)),
    "sn2_development_stop": _entry("game_process", ("windows", "exact_expected_process"),
                                     "src/sn2_mcp/development.py:DevelopmentService.stop",
                                     ("state", "process", "tool_result", "outcome_unknown", "stop_reason"),
                                     state_tools=("sn2_development_processes",)),
    "sn2_development_key": _entry("game_window_message_queue", ("windows", "exact_expected_process",
                                                               "unique_or_exact_expected_window"),
                                    "src/sn2_mcp/window_input.py:queue_key",
                                    ("status", "process", "selected_hwnd", "window_thread_id", "virtual_key", "messages",
                                     "handling_confirmed", "outcome_unknown", "do_not_retry"),
                                    state_tools=("sn2_development_processes",)),
    "sn2_native_load": _entry("game_process_modules", ("windows", "exact_expected_process", "expected_dll_hash"),
                               "src/sn2_mcp/development.py:DevelopmentService.load_native",
                               ("state", "process", "dll_sha256", "game_ready", "outcome_unknown"),
                               state_tools=("sn2_development_processes",)),
    "sn2_native_status": _entry("native_process_metadata", ("native_tcp_channel",),
                                 "native/src/game_access.cpp:GameAccess::status", _NATIVE_STATUS),
    "sn2_native_enable": _entry("native_hooks_and_tick_queue", ("native_tcp_channel", "compatible_loaded_dll"),
                                 "native/src/game_access.cpp:GameAccess::start", _NATIVE_STATUS,
                                 state_tools=("sn2_native_status",)),
    "sn2_movieplayer_status": _entry(
        "movieplayer_wait_cache", ("native_tcp_channel",),
        "native/src/game_access.cpp:GameAccess::movieplayer_status",
        ("source", "active_wait", "wait_id", "wait_returned", "thread_id", "caller_allow_engine_tick",
         "entered_ms", "last_sample_ms", "sample_wait_id", "loading_finished", "movie_playing",
         "finish_call_completed", "last_returned_wait_id", "last_returned_ms", "sample_error",
         "hooks_installed", "wait_hook_installed", "message_pump_hook_installed", "adapter_source",
         "game_calls_on_status", "ordinary_tick_ready", "session_id", "process_id", "protocol")),
    "sn2_movieplayer_finish": _entry(
        "movieplayer_wait_finish", ("native_tcp_channel", "loaded_movieplayer_hooks", "matching_observed_wait_id",
                                  "loading_finished_at_platform_service"),
        "native/src/game_access.cpp:GameAccess::request_movieplayer_finish",
        ("source", "wait_id", "thread_id", "loading_finished", "finish_call_completed",
         "session_id", "process_id", "protocol"),
        state_tools=("sn2_movieplayer_status",)),
}

for _name, _implementation, _fields, _dynamic in (
    ("sn2_find_objects", "native/src/game_access.cpp:GameAccess::execute", ("items", "scanned_slots", "truncated", "thread_id"), False),
    ("sn2_object_properties", "native/src/game_access.cpp:GameAccess::properties", ("items", "truncated", "validation"), True),
    ("sn2_object_functions", "native/src/game_access.cpp:GameAccess::functions", ("items", "truncated", "scan_truncated", "scan_complete"), True),
    ("sn2_read_array", "native/src/game_access_array.cpp:GameAccess::read_array",
     ("source", "session_id", "handle", "items", "total_count", "offset", "next_offset", "has_more", "partial"), True),
    ("sn2_read_struct", "native/src/game_access_struct.cpp:GameAccess::read_struct",
     ("source", "session_id", "handle", "components", "partial", "truncated"), True),
    ("sn2_read_set", "native/src/game_access_containers.cpp:GameAccess::read_set",
     ("source", "session_id", "handle", "container_type", "element", "items", "total_count", "max_index",
      "offset", "next_offset", "has_more", "truncated", "partial", "consistency"), True),
    ("sn2_read_map", "native/src/game_access_containers.cpp:GameAccess::read_map",
     ("source", "session_id", "handle", "container_type", "key", "value", "value_offset", "items", "total_count",
      "max_index", "offset", "next_offset", "has_more", "truncated", "partial", "consistency"), True),
    ("sn2_snapshot", "native/src/game_access.cpp:GameAccess::snapshot",
     ("snapshot_id", "source", "session_id", "objects", "partial", "truncated", "consistency"), True),
    ("sn2_invoke", "native/src/game_access.cpp:GameAccess::invoke",
     ("function", "executed", "return_type", "result", "thread_id"), True),
):
    _requires = ("native_tcp_channel", "current_game_tick")
    if _name != "sn2_find_objects":
        _requires += ("current_native_session_object_handles",)
    if _name == "sn2_invoke":
        _requires += ("reflected_function_signature", "function_specific_game_context")
    _CONTRACTS[_name] = _entry("game_function_effects" if _name == "sn2_invoke" else "runtime_reflection",
                               _requires, _implementation, _fields, dynamic=_dynamic,
                               state_tools=("sn2_native_status",))

for _name, _method, _fields in (
    ("sn2_watch_start", "start", _WATCH + ("start_seq", "duration_ms", "interval_ms", "sampling")),
    ("sn2_watch_read", "read", _WATCH + ("events", "gap", "cursor", "truncated")),
    ("sn2_watch_stop", "stop", _WATCH),
    ("sn2_watch_forget", "forget", ("watch_id", "forgotten", "cleanup_complete")),
):
    _requires = ("native_tcp_channel", "current_game_tick")
    _requires += (("current_native_session_object_handles",) if _method == "start" else ("existing_watch_id",))
    _CONTRACTS[_name] = _entry("native_observation" if _method != "read" else "observation_event_copies",
                               _requires, "native/src/observations.cpp:Observations::" + _method,
                               _fields, dynamic=_method == "read", state_tools=("sn2_native_status",))

_CONTRACTS["sn2_watch_list"] = _entry(
    "current_native_connection_observation_metadata", ("native_tcp_channel", "current_game_tick"),
    "native/src/observations.cpp:Observations::list", ("session_id", "current_client_owner", "items"),
    dynamic=True, state_tools=("sn2_native_status",))
_CONTRACTS["sn2_watch_resume"] = _entry(
    "native_observation_client_metadata", ("native_tcp_channel", "current_game_tick", "existing_watch_id",
                                           "matching_native_session_if_supplied"),
    "native/src/observations.cpp:Observations::resume", _WATCH, state_tools=("sn2_native_status", "sn2_watch_list"))


def build_tool_contracts(registered_tools: Iterable[Any]) -> list[dict[str, Any]]:
    """Copy MCP schemas and descriptions without invoking handlers or probes."""
    contracts = []
    for tool in registered_tools:
        details = _CONTRACTS[tool.name]
        annotations = tool.annotations
        contracts.append({
            "name": tool.name,
            "description": tool.description,
            "args": deepcopy(tool.input_schema),
            "result": {"schema": deepcopy(tool.output_schema), "fields": list(details["fields"]),
                       "fields_complete": False, "dynamic_fields": details["dynamic"]},
            "effects": {"read_only": annotations.read_only_hint, "idempotent": annotations.idempotent_hint,
                        "destructive": annotations.destructive_hint, "target": details["target"]},
            "context": {"requires": list(details["requires"]), "state_tools": list(details["state_tools"])},
            "source": {"contract": "registered_mcp_tool", "handler": "src/sn2_mcp/server.py:" + tool.name,
                       "implementation": details["implementation"], "verification_kind": "source"},
            "implemented": True,
            "availability": {"checked": False, "current": None, "reason": "not_checked_by_catalog"},
        })
    return contracts

# Runtime composition uses only registered tools; retained Gothic contracts are provenance.
_CONTRACTS["sn2_status"] = _entry("frontend_configuration", (), "src/sn2_mcp/server.py:sn2_status", ("server", "workspace", "runtime_root", "game_root", "native", "target_process"))
_CONTRACTS["sn2_capabilities"] = _entry("registered_runtime_contracts", (), "src/sn2_mcp/server.py:sn2_capabilities", ("tool_contracts", "capabilities", "accepted_types"))
_CONTRACTS["sn2_target_processes"] = _entry("windows_process_snapshot", ("windows", "configured_game"), "src/sn2_mcp/development.py:RuntimeService.process_state", ("processes", "running", "source"))
_CONTRACTS["sn2_native_load"] = _entry("selected_process_modules", ("windows", "exact_target_process", "expected_dll_hash"), "src/sn2_mcp/development.py:RuntimeService.load_native", ("state", "process", "dll_sha256", "game_ready", "outcome_unknown"), state_tools=("sn2_native_status", "sn2_target_processes"))

for _name, _method in {
    "sn2_list_source_files": "list_files", "sn2_find_source_files": "search_files",
    "sn2_search_source": "search_text", "sn2_read_source": "read_file",
    "sn2_read_source_exact": "read_file_exact", "sn2_write_source": "write_file",
    "sn2_create_source_directory": "create_directory",
}.items():
    _CONTRACTS[_name] = _entry("workspace_source", ("configured_workspace",),
        "src/sn2_mcp/workspace.py:SourceWorkspace." + _method,
        ("source", "path", "items", "sha256", "compiled", "runtime_validation"))
_CONTRACTS["sn2_source_identity"] = _entry("workspace_git_metadata", ("configured_workspace",),
    "src/sn2_mcp/source_identity.py:inspect", ("source", "git", "configured_revision", "game_compatibility_checked"))
_CONTRACTS["sn2_development_recipe"] = _entry("workspace_sources_and_reflected_interfaces", ("configured_workspace",),
    "src/sn2_mcp/server.py:sn2_development_recipe",
    ("source_files", "source_inventory_sha256", "source_stable", "interfaces", "native_status", "limits"),
    state_tools=("sn2_source_identity", "sn2_native_status"))
_CONTRACTS["sn2_export_sources"] = _entry("workspace_source_export", ("configured_workspace",),
    "src/sn2_mcp/package_export.py:export_sources", ("destination", "archive_sha256", "manifest_sha256", "file_count"))
_CONTRACTS["sn2_verify_source_export"] = _entry("workspace_source_export", ("configured_workspace",),
    "src/sn2_mcp/package_export.py:verify_source_export", ("kind", "verified_files", "file_count", "archive_sha256"))

for _name, _method in {"sn2_build_recipe": "recipe", "sn2_build": "build",
        "sn2_build_status": "status", "sn2_compile_diagnostics": "diagnostics"}.items():
    _CONTRACTS[_name] = _entry("captured_native_build", ("configured_runtime_sources", "cmake", "msvc"),
        "src/sn2_mcp/build_workflow.py:BuildWorkflow." + _method,
        ("build_id", "state", "recipe", "stages", "artifacts", "compiler_reports", "exact_source_compiled"),
        state_tools=("sn2_build_status", "sn2_compile_diagnostics"))
for _name, _method in {"sn2_development_start": "start", "sn2_development_stop": "stop",
        "sn2_development_restart": "restart"}.items():
    _CONTRACTS[_name] = _entry("selected_game_process_lifecycle", ("windows", "configured_game"),
        "src/sn2_mcp/runtime_lifecycle.py:RuntimeLifecycle." + _method,
        ("state", "process", "previous_process", "launch_receipt", "stop_receipt", "outcome_unknown"),
        state_tools=("sn2_target_processes", "sn2_native_status"))
for _name, _method in {"sn2_create_mod_project": "create", "sn2_validate_mod_project": "validate",
        "sn2_package_artifact": "package", "sn2_verify_artifact_package": "verify"}.items():
    _CONTRACTS[_name] = _entry("standalone_native_mod_project", ("configured_workspace", "native_sdk"),
        "src/sn2_mcp/mod_projects.py:ModProjects." + _method,
        ("kind", "project_path", "valid", "manifest_sha256", "artifact", "source_revision", "files"),
        state_tools=("sn2_validate_mod_project", "sn2_verify_artifact_package"))
for _name, _method in {"sn2_mod_load": "load", "sn2_mod_status": "status", "sn2_mod_disable": "disable"}.items():
    _CONTRACTS[_name] = _entry("selected_compiled_mod", ("verified_artifact_package", "exact_target_process"),
        "src/sn2_mcp/mod_runtime.py:ModRuntime." + _method,
        ("process", "dll_sha256", "module_loaded", "mod_receipt", "package", "outcome_unknown"),
        state_tools=("sn2_mod_status", "sn2_target_processes"))
_CONTRACTS["sn2_mod_reload"] = _entry("compiled_revision_restart_iteration",
    ("verified_artifact_package", "exact_target_process", "expected_bridge_hash"),
    "src/sn2_mcp/server.py:sn2_mod_reload",
    ("state", "iteration", "process", "previous_process", "restart", "native_status", "mod"),
    state_tools=("sn2_mod_status", "sn2_native_status", "sn2_target_processes"))
