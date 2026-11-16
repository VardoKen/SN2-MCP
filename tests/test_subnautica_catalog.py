"""Public runtime contracts distinguish local discovery from target acceptance."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import Mock, patch

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from sn2_mcp import __version__
from sn2_mcp.server import create_server, main


RUNTIME_TOOLS = {
    "sn2_status", "sn2_capabilities", "sn2_target_processes",
    "sn2_native_load", "sn2_native_status", "sn2_native_enable",
    "sn2_find_objects", "sn2_object_properties", "sn2_read_array",
    "sn2_read_struct", "sn2_read_set", "sn2_read_map",
    "sn2_object_functions", "sn2_snapshot", "sn2_snapshot_compare",
    "sn2_watch_start", "sn2_watch_read", "sn2_watch_stop",
    "sn2_watch_forget", "sn2_watch_list", "sn2_watch_resume", "sn2_invoke",
    "sn2_load_asset", "sn2_load_class", "sn2_blueprint_mod_enable",
    "sn2_blueprint_mod_status", "sn2_blueprint_mod_disable",
}
SOURCE_TOOLS = {"sn2_source_identity", "sn2_list_source_files", "sn2_find_source_files",
                "sn2_search_source", "sn2_read_source", "sn2_read_source_exact", "sn2_write_source",
                "sn2_create_source_directory", "sn2_development_recipe", "sn2_export_sources", "sn2_verify_source_export"}
BUILD_TOOLS = {"sn2_build_recipe", "sn2_build", "sn2_build_status", "sn2_compile_diagnostics"}
LIFECYCLE_TOOLS = {"sn2_development_start", "sn2_development_stop", "sn2_development_restart"}
MOD_TOOLS = {"sn2_create_mod_project", "sn2_validate_mod_project", "sn2_package_artifact",
             "sn2_verify_artifact_package", "sn2_mod_load", "sn2_mod_status",
             "sn2_mod_disable", "sn2_mod_reload", "sn2_content_environment",
             "sn2_cook_recipe", "sn2_cook_mod", "sn2_cook_status", "sn2_cook_report",
             "sn2_mod_install", "sn2_mod_uninstall", "sn2_mod_deployment_status",
             "sn2_authoring_recipe", "sn2_author_mod", "sn2_authoring_status", "sn2_authoring_report",
             "sn2_runtime_authoring_recipe", "sn2_runtime_compile_blueprint", "sn2_runtime_save_package",
             "sn2_runtime_release_blueprint",
             "sn2_runtime_authoring_status", "sn2_runtime_authoring_report", "sn2_runtime_build_status",
             "sn2_package_runtime_artifact", "sn2_service_status", "sn2_service_enable", "sn2_service_disable", "sn2_service_call"}
PUBLIC_TOOLS = RUNTIME_TOOLS | SOURCE_TOOLS | BUILD_TOOLS | LIFECYCLE_TOOLS | MOD_TOOLS


class SubnauticaCatalogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_runtime_catalog_excludes_gothic_and_client_session_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sn2_mcp.server.NativeClient"), \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory:
                server = create_server(Path(directory))
                tools = await server.list_tools()

                self.assertEqual({tool.name for tool in tools}, PUBLIC_TOOLS)
                forbidden = ("g1r_", "sn2_debug_", "sn2_reload", "sn2_save_", "sn2_session_")
                self.assertFalse(any(tool.name.startswith(forbidden) for tool in tools))
                runtime_factory.assert_not_called()

    async def test_schemas_require_operation_identity_and_expose_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sn2_mcp.server.NativeClient"), \
                 patch("sn2_mcp.server.RuntimeService"):
                server = create_server(Path(directory))
                tools = {tool.name: tool for tool in await server.list_tools()}

                expected_required = {
                    "sn2_native_load": {"target_process", "dll_path", "expected_dll_sha256"},
                    "sn2_native_enable": {"target_process", "expected_native_session_id"},
                    "sn2_invoke": {"handle", "function"},
                    "sn2_watch_start": {"handle", "properties", "duration_ms", "interval_ms"},
                    "sn2_runtime_compile_blueprint": {"project_path", "spec_path"},
                    "sn2_runtime_save_package": {"authoring_id"},
            "sn2_runtime_release_blueprint": {"authoring_id"},
                    "sn2_package_runtime_artifact": {"runtime_build_receipt", "destination"},
                }
                for name, required in expected_required.items():
                    with self.subTest(tool=name):
                        self.assertEqual(set(tools[name].input_schema["required"]), required)
                invoke_schema = tools["sn2_invoke"].input_schema
                self.assertIn("expected_signature_token", invoke_schema["properties"])
                self.assertNotIn("expected_signature_token", invoke_schema["required"])

                # Tuple order: read-only, destructive, idempotent, open-world.
                mutation_effects = {
                    "sn2_build": (False, False, False, False),
                    "sn2_create_mod_project": (False, False, False, False),
                    "sn2_package_artifact": (False, False, False, False),
                    "sn2_cook_mod": (False, False, False, False),
                    "sn2_author_mod": (False, False, False, False),
                    "sn2_runtime_compile_blueprint": (False, False, False, True),
                    "sn2_runtime_save_package": (False, False, False, True),
            "sn2_runtime_release_blueprint": (False, False, True, True),
                    "sn2_package_runtime_artifact": (False, False, False, False),
                    "sn2_service_enable": (False, False, False, True),
                    "sn2_service_disable": (False, True, False, True),
                    "sn2_service_call": (False, True, False, True),
                    "sn2_mod_install": (False, True, False, True),
                    "sn2_mod_uninstall": (False, True, False, True),
                    "sn2_load_asset": (False, False, False, True),
                    "sn2_load_class": (False, False, False, True),
                    "sn2_blueprint_mod_enable": (False, False, False, True),
                    "sn2_blueprint_mod_disable": (False, True, False, True),
                    "sn2_development_start": (False, False, False, True),
                    "sn2_development_stop": (False, True, False, True),
                    "sn2_development_restart": (False, True, False, True),
                    "sn2_mod_load": (False, False, False, True),
                    "sn2_mod_disable": (False, True, False, True),
                    "sn2_mod_reload": (False, True, False, True),
                    "sn2_write_source": (False, False, False, False),
                    "sn2_create_source_directory": (False, False, False, False),
                    "sn2_export_sources": (False, False, False, False),
                    "sn2_native_load": (False, False, False, True),
                    "sn2_native_enable": (False, False, True, False),
                    "sn2_watch_start": (False, False, False, False),
                    "sn2_watch_stop": (False, False, True, False),
                    "sn2_watch_forget": (False, False, False, False),
                    "sn2_watch_resume": (False, False, False, False),
                    "sn2_invoke": (False, True, False, False),
                }
                for name, tool in tools.items():
                    with self.subTest(tool=name):
                        self.assertEqual(tool.input_schema["type"], "object")
                        self.assertEqual(tool.output_schema["type"], "object")
                        self.assertNotIn("ctx", tool.input_schema.get("properties", {}))
                        annotations = tool.annotations
                        actual = (annotations.read_only_hint, annotations.destructive_hint,
                                  annotations.idempotent_hint, annotations.open_world_hint)
                        self.assertEqual(actual, mutation_effects.get(name, (True, False, True, False)))

    async def test_runtime_authoring_tools_forward_native_uncertainty_and_saved_receipts(self):
        from sn2_mcp.native_client import NativeError
        from mcp.server.mcpserver.exceptions import ToolError
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            service = Mock()
            service.recipe.return_value = {"execution_performed": False, "backend": "native_unreal_runtime"}
            service.save.return_value = {"state": "serialized_raw_package", "persisted": True, "containerized": False}
            service.status.return_value = {"state": "outcome_unknown", "compiled": False}
            native_error = NativeError("response_lost", "The compile outcome is unknown", True, "compile-17", {"do_not_retry": True})
            service.compile.side_effect = native_error
            with patch("sn2_mcp.server.NativeClient") as native, patch("sn2_mcp.server.RuntimeAuthoring", return_value=service), \
                 patch("sn2_mcp.server.ContentAuthoring") as editor:
                server = create_server(workspace)
                await server.call_tool("sn2_runtime_authoring_recipe", {"project_path": "projects/UserMod", "spec_path": "spec.json"})
                try:
                    result = await server.call_tool("sn2_runtime_compile_blueprint", {"project_path": "projects/UserMod", "spec_path": "spec.json", "timeout_seconds": 8})
                    self.assertTrue(result.is_error)
                    raw = next(item.text for item in result.content if item.type == "text")
                except ToolError as error:
                    raw = str(error)
                error = json.loads(raw[raw.index("{"):])
                self.assertEqual(error["code"], "response_lost")
                self.assertTrue(error["may_have_executed"])
                self.assertEqual(error["effect"], native_error.details)
                result = await server.call_tool("sn2_runtime_save_package", {"authoring_id": "a" * 32, "timeout_seconds": 12})
                self.assertEqual(result.structured_content["state"], "serialized_raw_package")
                self.assertFalse(result.structured_content["containerized"])
                service.compile.assert_called_once_with("projects/UserMod", "spec.json", None, 8.0)
                service.save.assert_called_once_with("a" * 32, 12.0)
                editor.assert_not_called()
                native.return_value.request.assert_not_called()

    async def test_local_status_does_not_create_target_service_or_probe_native(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            native = Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory:
                server = create_server(workspace, native_port=28223,
                                       game_root=workspace / "synthetic-game")
                async with server.settings.lifespan(server):
                    result = await server.call_tool("sn2_status", {})
                    self.assertFalse(result.is_error)
                    status = result.structured_content
                    self.assertEqual(status["server"]["name"], "Subnautica 2 MCP")
                    self.assertEqual(status["server"]["version"], __version__)
                    self.assertTrue(status["server"]["ready"])
                    self.assertEqual(status["workspace"]["root"], str(workspace))
                    self.assertEqual(status["workspace"]["deployment_state"], "not_checked")
                    self.assertFalse(status["workspace"]["installed_game_deployment_known"])
                    self.assertEqual(status["native"]["endpoint"], "127.0.0.1:28223")
                    self.assertFalse(status["native"]["connection_checked"])
                    self.assertIsNone(status["target_process"])
                    self.assertFalse(status["game_ready"])
                    self.assertFalse(status["game_ready_known"])
                runtime_factory.assert_not_called()
                native.request.assert_not_called()

    async def test_unobserved_capabilities_report_source_contracts_only(self):
        with tempfile.TemporaryDirectory() as directory:
            native = Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory:
                server = create_server(Path(directory))
                async with server.settings.lifespan(server):
                    registrations = {tool.name: tool for tool in await server.list_tools()}
                    result = await server.call_tool("sn2_capabilities", {})
                    self.assertFalse(result.is_error)
                    capabilities = result.structured_content
                    self.assertEqual(capabilities["catalog_revision"], __version__)
                    self.assertIsNone(capabilities["target_process"])
                    self.assertIsNone(capabilities["native_status"])
                    self.assertEqual(capabilities["accepted_types"], {})
                    self.assertIs(capabilities["automatic_retries"], False)
                    self.assertEqual(capabilities["capabilities"]["frontend"], {
                        "state": "verified", "source": "registered_mcp_tools",
                    })
                    self.assertEqual(capabilities["capabilities"]["target_abi"], {
                        "state": "unverified", "reason": "per_shape_runtime_receipts_required",
                    })
                    contracts = capabilities["tool_contracts"]
                    self.assertEqual({contract["name"] for contract in contracts}, PUBLIC_TOOLS)
                    for contract in contracts:
                        name = contract["name"]
                        with self.subTest(tool=name):
                            tool = registrations[name]
                            self.assertEqual(contract["args"], tool.input_schema)
                            self.assertEqual(contract["result"]["schema"], tool.output_schema)
                            self.assertFalse(contract["result"]["fields_complete"])
                            self.assertEqual(contract["source"]["contract"], "registered_mcp_tool")
                            self.assertEqual(contract["source"]["handler"],
                                             "src/sn2_mcp/server.py:" + name)
                            self.assertEqual(contract["source"]["verification_kind"], "source")
                            self.assertTrue(contract["implemented"])
                            self.assertEqual(contract["availability"], {
                                "checked": False, "current": None,
                                "reason": "not_checked_by_catalog",
                            })
                            self.assertEqual(contract["effects"]["read_only"],
                                             tool.annotations.read_only_hint)
                            self.assertEqual(contract["effects"]["idempotent"],
                                             tool.annotations.idempotent_hint)
                            self.assertEqual(contract["effects"]["destructive"],
                                             tool.annotations.destructive_hint)
                runtime_factory.assert_not_called()
                native.request.assert_not_called()

    async def test_optional_offsets_and_watch_sessions_cross_tcp_and_preserve_refusals(self):
        from tests.test_mcp_observations import SessionNativeFixture

        pagination = (
            ("sn2_find_objects", "objects", {"query": "Neutral", "class_query": "Actor", "limit": 7,
                                              "exact_path": "/Script/Neutral.Default__Actor"}),
            ("sn2_object_properties", "properties", {"handle": "neutral-handle", "limit": 7,
                                                       "property_names": ["Enabled", "Count"]}),
            ("sn2_object_functions", "functions", {"handle": "neutral-handle", "limit": 7,
                                                     "function_query": "NeutralFunction"}),
        )
        watches = (
            ("sn2_watch_start", "watch_start", {"handle": "neutral-handle", "properties": ["Enabled"],
                                                "duration_ms": 1000, "interval_ms": 20, "capacity": 3}),
            ("sn2_watch_read", "watch_read", {"watch_id": "neutral-watch", "after_seq": 2, "limit": 7}),
            ("sn2_watch_stop", "watch_stop", {"watch_id": "neutral-watch"}),
            ("sn2_watch_forget", "watch_forget", {"watch_id": "neutral-watch"}),
            ("sn2_watch_list", "watch_list", {}),
            ("sn2_watch_resume", "watch_resume", {"watch_id": "neutral-watch",
                                                    "expected_client_owner": "shared_native_connection"}),
        )
        steps, calls = [], []
        for name, operation, arguments in pagination:
            for offset, code in ((0, None), (37, None), (-1, "invalid_offset")):
                supplied = {**arguments, "offset": offset}
                calls.append((name, supplied, code))
                steps.append({"op": operation, "params": supplied if offset else dict(arguments)})
        for name, operation, arguments in watches:
            for session, code in ((None, None), ("fixture-session-1", None),
                                  ("stale-native-session", "native_session_mismatch")):
                supplied = {**arguments, "expected_session_id": session}
                calls.append((name, supplied, code))
                wire = dict(arguments) if session is None else supplied
                steps.append({"op": operation, "params": {**wire, "client_owner": "shared_native_connection"}})
        for index, ((_, _, code), step) in enumerate(zip(calls, steps)):
            if code is None:
                step["result"] = {"operation": step["op"], "receipt_index": index, "source": "neutral_tcp_fixture"}
            else:
                step["error"] = {"code": code, "message": "The supplied runtime identity or page offset was rejected",
                                 "details": {"executed": False, "may_have_executed": False}}

        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", directory, "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    schemas = {tool.name: tool.input_schema for tool in (await client.list_tools()).tools}
                    for name, _, _ in pagination:
                        self.assertEqual(schemas[name]["properties"]["offset"]["default"], 0)
                        self.assertNotIn("offset", schemas[name].get("required", []))
                    for name, _, _ in watches:
                        self.assertIsNone(schemas[name]["properties"]["expected_session_id"]["default"])
                        self.assertNotIn("expected_session_id", schemas[name].get("required", []))
                    for index, (name, arguments, code) in enumerate(calls):
                        with self.subTest(tool=name, arguments=arguments):
                            response = await client.call_tool(name, arguments)
                            if code is None:
                                self.assertFalse(response.is_error, response.content)
                                self.assertEqual(response.structured_content, steps[index]["result"])
                            else:
                                self.assertTrue(response.is_error, response.content)
                                message = next(item.text for item in response.content if item.type == "text")
                                error = json.loads(message[message.index("{"):])
                                self.assertEqual(error["code"], code)
                                self.assertFalse(error["may_have_executed"])
                                self.assertEqual(error["request_id"], native.requests[index]["id"])
                    # Every refusal is followed only by the next explicit call.
                    self.assertEqual(len(native.requests), len(calls))
                    for name, _, arguments in pagination:
                        for offset in (False, 1.5, "37"):
                            with self.subTest(tool=name, invalid_offset=offset):
                                response = await client.call_tool(name, {**arguments, "offset": offset})
                                self.assertTrue(response.is_error, response.content)
                                message = next(item.text for item in response.content if item.type == "text")
                                self.assertIn("offset", message)
                                self.assertEqual(len(native.requests), len(calls))
            self.assertEqual(native.connections, 1)
            self.assertEqual(len({request["id"] for request in native.requests}), len(calls))


class SubnauticaCliTests(unittest.TestCase):
    def test_gothic_cli_arguments_fail_before_server_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            for argument in ("--enable-ingame-tools", "--source-revision"):
                with self.subTest(argument=argument):
                    stderr = io.StringIO()
                    with patch("sn2_mcp.server.create_server") as server_factory, \
                         redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                        main(["--workspace-root", directory, argument, "synthetic-value"])
                    self.assertEqual(raised.exception.code, 2)
                    self.assertEqual(stderr.getvalue(),
                                     "Invalid Subnautica 2 MCP arguments; use --help\n")
                    server_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
