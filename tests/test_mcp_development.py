"""Публичные MCP-вызовы управления разработкой с изолированным executor."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver.exceptions import ToolError

import sn2_mcp.server as server_module
from sn2_mcp.development import CommandResult, DevelopmentService
from sn2_mcp.server import create_server


class MCPDevelopmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "tools").mkdir(parents=True)
        for name in ("game_process_state.ps1", "start_sn2_development.ps1",
                     "stop_sn2_development.ps1", "load_native.ps1"):
            (self.repository / "tools" / name).write_text("# isolated fixture", encoding="utf-8")
        dll = self.repository / "build" / "native" / "Release" / "SN2_MCP.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"isolated DLL bytes")
        self.dll_sha256 = hashlib.sha256(dll.read_bytes()).hexdigest()
        self.workspace = self.root / "workspace"
        self.modkit = self.root / "Modkit"
        self.game = self.root / "Game"
        for directory in (self.workspace, self.modkit, self.game):
            directory.mkdir()
        (self.modkit / "Script").mkdir()
        (self.modkit / "Script" / "Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
        self.package = self.workspace / "Nested" / "Probe"
        (self.package / "Script").mkdir(parents=True)
        self.script = self.package / "Script" / "Probe.as"
        self.script.write_text("class UProbe {}\n", encoding="utf-8")
        (self.package / "GothicMod.json").write_text(json.dumps({"name": "Probe", "title": "Probe",
            "author": "Fixture", "modVersion": "1.0.0"}), encoding="utf-8")
        self.calls = []
        self.processes = []
        self.responses = {}
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
            modkit_root=self.modkit, game_root=self.game, executor=self.execute, powershell="fixture-powershell")
        self.identity = {"pid": 123, "creation_time": "2026-10-31T00:01:02.1234567Z",
                         "executable": str(self.service.inner_executable)}
        factory_patch = patch("sn2_mcp.server.DevelopmentService", return_value=self.service)
        self.factory = factory_patch.start()
        self.addCleanup(factory_patch.stop)
        self.server = create_server(self.modkit, self.workspace, game_root=self.game)

    def execute(self, arguments, timeout):
        self.calls.append((list(arguments), timeout))
        script = Path(arguments[arguments.index("-File") + 1]).name
        if script == "game_process_state.ps1":
            return CommandResult(0, json.dumps({"processes": self.processes}))
        if script in self.responses:
            return self.responses[script]
        if script == "start_sn2_development.ps1":
            value = {"state": "launch_requested", "package_name": "Probe",
                     "mods_directory": arguments[arguments.index("-ModsDirectory") + 1],
                     "mods": arguments[arguments.index("-ModsCsv") + 1].split(",")}
        elif script == "stop_sn2_development.ps1":
            value = {"state": "stopped", "process": self.identity}
        else:
            value = {"state": "loaded", "process": self.identity, "dll_sha256": self.dll_sha256}
        return CommandResult(0, json.dumps(value))

    def mutation_calls(self):
        return [(arguments, timeout) for arguments, timeout in self.calls
                if not arguments[arguments.index("-File") + 1].endswith("game_process_state.ps1")]

    async def tool(self, name, arguments=None):
        result = await self.server.call_tool(name, arguments or {})
        self.assertFalse(result.is_error, result.content)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    async def error(self, name, arguments, code, *, unknown=False):
        with self.assertRaises(ToolError) as caught:
            await self.server.call_tool(name, arguments)
        error = json.loads(str(caught.exception)[str(caught.exception).index("{"):])
        self.assertEqual(error["code"], code)
        self.assertIs(error["outcome_unknown"], unknown)
        self.assertEqual(error["retry"], "only_explicit_new_request")
        return error

    async def test_stdio_claim_survives_request_contexts_and_releases_normally(self):
        bootstrap = """from pathlib import Path
import sys
from unittest.mock import Mock, patch
from sn2_mcp.server import create_server
service = Mock()
service.process_state.return_value = {"processes": [], "running": False}
with patch("sn2_mcp.server.DevelopmentService", return_value=service):
    create_server(Path(sys.argv[1]), Path(sys.argv[2]), game_root=Path(sys.argv[3])).run(transport="stdio")
"""
        parameters = StdioServerParameters(command=sys.executable, args=[
            "-B", "-c", bootstrap, str(self.modkit), str(self.workspace), str(self.game)])
        async with stdio_client(parameters) as (incoming, outgoing):
            async with ClientSession(incoming, outgoing) as client:
                await client.initialize()
                acquired = await client.call_tool("sn2_session_acquire", {"reason": "Изолированная stdio-проверка"})
                self.assertFalse(acquired.is_error, acquired.content)
                original = acquired.structured_content["claim"]
                self.assertIsNone(original["process"])
                observed = await client.call_tool("sn2_session_status", {})
                self.assertFalse(observed.is_error, observed.content)
                self.assertEqual(observed.structured_content["claim"], original)
                released = await client.call_tool("sn2_session_release", {})
                self.assertFalse(released.is_error, released.content)
                self.assertTrue(released.structured_content["released"])
                self.assertEqual(released.structured_content["previous_claim"], original)
                self.assertFalse(released.structured_content["game_stopped"])
                final = await client.call_tool("sn2_session_status", {})
                self.assertFalse(final.is_error, final.content)
                self.assertFalse(final.structured_content["claimed"])
                self.assertIsNone(final.structured_content["claim"])
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.game.iterdir()), [])

    async def test_recipe_start_load_stop_bind_hashes_and_process_identity(self):
        # Статический статус не создаёт сервис и не запускает системных команд.
        await self.tool("sn2_status")
        self.factory.assert_not_called()
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        self.assertFalse(recipe["compiled"])
        self.assertFalse(recipe["process_state"]["running"])
        self.assertEqual(recipe["native_artifact"]["sha256"], self.dll_sha256)
        manifest_hash = recipe["manifest"]["manifest_sha256"]
        start = await self.tool("sn2_development_start", {"relative_package": "Nested/Probe",
                               "expected_manifest_sha256": manifest_hash})
        self.assertEqual(start["state"], "launch_requested")
        self.assertEqual(start["manifest_sha256"], manifest_hash)
        self.assertFalse(start["compiled"])
        self.assertFalse(start["game_ready"])
        self.processes = [self.identity]
        processes = await self.tool("sn2_development_processes")
        self.assertEqual(processes["processes"], [self.identity])
        loaded = await self.tool("sn2_native_load", {"expected_process": self.identity,
                                                  "expected_dll_sha256": self.dll_sha256})
        self.assertEqual(loaded["dll_sha256"], self.dll_sha256)
        self.assertFalse(loaded["game_ready"])
        stopped = await self.tool("sn2_development_stop", {"expected_process": self.identity})
        self.assertEqual(stopped["process"], self.identity)
        self.assertFalse(stopped["outcome_unknown"])
        self.factory.assert_called_once_with(repository_root=Path(server_module.__file__).resolve().parents[2], workspace_root=self.workspace,
            modkit_root=self.modkit, game_root=self.game)
        mutations = self.mutation_calls()
        self.assertEqual([Path(args[args.index("-File") + 1]).name for args, _ in mutations],
                         ["start_sn2_development.ps1", "load_native.ps1", "stop_sn2_development.ps1"])
        self.assertTrue(all(timeout == 30 for _, timeout in self.calls))
        for args, _ in mutations[1:]:
            self.assertEqual(args[args.index("-ExpectedPid") + 1], "123")
            self.assertEqual(args[args.index("-ExpectedCreationTime") + 1], self.identity["creation_time"])
        self.assertEqual(mutations[1][0][mutations[1][0].index("-ExpectedDllSha256") + 1], self.dll_sha256)

    async def test_compiler_report_mcp_reports_current_hash_and_does_not_claim_runtime(self):
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        report = self.workspace / "errors.txt"
        report.write_text("Script/Probe.as (48:1) : Error: fixture override", encoding="utf-8")
        response = await self.tool("sn2_compile_diagnostics", {"relative_package":"Nested/Probe",
            "expected_manifest_sha256":recipe["manifest"]["manifest_sha256"], "report_path":"errors.txt"})
        self.assertFalse(response["compiled"])
        self.assertFalse(response["report_matches_runtime_compile"])
        self.assertEqual(response["diagnostics"][0]["file"], "Script/Probe.as")
        self.assertEqual(response["diagnostics"][0]["line"], 48)
        self.assertEqual(response["diagnostics"][0]["source_sha256"], hashlib.sha256(self.script.read_bytes()).hexdigest())
        self.assertEqual(self.mutation_calls(), [])
        tools = {tool.name:tool for tool in await self.server.list_tools()}
        self.assertTrue(tools["sn2_compile_diagnostics"].annotations.read_only_hint)
        self.script.write_text("class UChanged {}", encoding="utf-8")
        response = await self.tool("sn2_compile_diagnostics", {"relative_package":"Nested/Probe",
            "expected_manifest_sha256":recipe["manifest"]["manifest_sha256"],"report_path":"errors.txt"})
        self.assertEqual(response["manifest_sha256"], self.service._manifest("Nested/Probe")["manifest_sha256"])
        self.assertEqual(response["diagnostics"][0]["source_sha256"], hashlib.sha256(self.script.read_bytes()).hexdigest())
        self.assertEqual(response["source_current_correlation"], "unknown")

    async def test_ingame_tools_is_optional_boolean_in_public_schema(self):
        tools = {tool.name: tool for tool in await self.server.list_tools()}
        for name in ("sn2_development_recipe", "sn2_development_start"):
            with self.subTest(tool=name):
                schema = tools[name].input_schema
                option = schema["properties"]["enable_ingame_tools"]
                self.assertEqual(option["type"], "boolean")
                self.assertIs(option["default"], False)
                self.assertNotIn("enable_ingame_tools", schema.get("required", []))

    async def test_ingame_tools_defaults_and_true_are_bound_to_manifest_and_launcher(self):
        default = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        disabled = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe",
                                                              "enable_ingame_tools": False})
        enabled = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe",
                                                             "enable_ingame_tools": True})
        self.assertEqual(default["manifest"]["manifest_sha256"], disabled["manifest"]["manifest_sha256"])
        self.assertEqual(default["manifest"]["launch_options"], {"enable_ingame_tools": False})
        self.assertEqual(enabled["manifest"]["launch_options"], {"enable_ingame_tools": True})
        self.assertNotEqual(default["manifest"]["manifest_sha256"], enabled["manifest"]["manifest_sha256"])

        await self.error("sn2_development_start", {"relative_package": "Nested/Probe",
                         "expected_manifest_sha256": default["manifest"]["manifest_sha256"],
                         "enable_ingame_tools": True}, "package_changed")
        await self.error("sn2_development_start", {"relative_package": "Nested/Probe",
                         "expected_manifest_sha256": enabled["manifest"]["manifest_sha256"]}, "package_changed")
        self.assertEqual(self.mutation_calls(), [])

        started = await self.tool("sn2_development_start", {"relative_package": "Nested/Probe",
                                  "expected_manifest_sha256": enabled["manifest"]["manifest_sha256"],
                                  "enable_ingame_tools": True})
        self.assertIs(started["enable_ingame_tools"], True)
        enabled_arguments, _ = self.mutation_calls()[-1]
        self.assertEqual(enabled_arguments.count("-EnableIngameTools"), 1)
        await self.tool("sn2_development_start", {"relative_package": "Nested/Probe",
                        "expected_manifest_sha256": default["manifest"]["manifest_sha256"]})
        disabled_arguments, _ = self.mutation_calls()[-1]
        self.assertNotIn("-EnableIngameTools", disabled_arguments)

    async def test_ingame_tools_non_booleans_are_rejected_before_executor(self):
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        manifest_hash = recipe["manifest"]["manifest_sha256"]
        for value in (0, 1, "true", "false", None, [], {}):
            for name in ("sn2_development_recipe", "sn2_development_start"):
                with self.subTest(tool=name, value=value):
                    arguments = {"relative_package": "Nested/Probe", "enable_ingame_tools": value}
                    if name == "sn2_development_start":
                        arguments["expected_manifest_sha256"] = manifest_hash
                    calls_before = list(self.calls)
                    with self.assertRaises(ToolError):
                        await self.server.call_tool(name, arguments)
                    self.assertEqual(self.calls, calls_before)

    async def test_changed_manifest_process_and_dll_refuse_before_mutation(self):
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        self.script.write_text("class UChangedProbe {}\n", encoding="utf-8")
        await self.error("sn2_development_start", {"relative_package": "Nested/Probe",
                         "expected_manifest_sha256": recipe["manifest"]["manifest_sha256"]}, "package_changed")
        self.assertEqual(self.mutation_calls(), [])
        self.processes = [{**self.identity, "pid": 124}]
        await self.error("sn2_development_stop", {"expected_process": self.identity}, "process_changed")
        self.processes = [self.identity]
        await self.error("sn2_native_load", {"expected_process": self.identity,
                         "expected_dll_sha256": "0" * 64}, "dll_changed")
        current = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        await self.error("sn2_development_start", {"relative_package": "Nested/Probe",
                         "expected_manifest_sha256": current["manifest"]["manifest_sha256"]}, "game_running")
        await self.error("sn2_development_start", {"relative_package": "Nested/Probe", "restart": True,
                         "expected_process": {**self.identity, "creation_time": "2026-10-31T00:01:02.0000000Z"},
                         "expected_manifest_sha256": current["manifest"]["manifest_sha256"]}, "process_changed")
        self.assertEqual(self.mutation_calls(), [])

    async def test_failed_mutation_reports_unknown_without_hidden_retry(self):
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        start = {"relative_package": "Nested/Probe", "expected_manifest_sha256": recipe["manifest"]["manifest_sha256"]}
        self.responses["start_sn2_development.ps1"] = CommandResult(1, "", "isolated failure")
        await self.error("sn2_development_start", start, "tool_failed", unknown=True)
        self.assertEqual(len(self.mutation_calls()), 1)
        # Следующая независимая проверка состояния не повторяет неясный запуск.
        await self.tool("sn2_development_processes")
        self.assertEqual(len(self.mutation_calls()), 1)
        self.responses["start_sn2_development.ps1"] = CommandResult(0, "not-json")
        await self.error("sn2_development_start", start, "tool_protocol_error", unknown=True)
        self.assertEqual(len(self.mutation_calls()), 2)
        self.responses.pop("start_sn2_development.ps1")
        explicit = await self.tool("sn2_development_start", start)
        self.assertEqual(explicit["state"], "launch_requested")
        self.assertEqual(len(self.mutation_calls()), 3)

    async def test_early_bootstrap_process_can_stop_but_never_receive_native_dll(self):
        bootstrap = {"pid": 321, "creation_time": "2026-10-31T00:04:05.7654321Z",
                     "executable": str(self.service.bootstrap_executable)}
        self.processes = [bootstrap]
        recipe = await self.tool("sn2_development_recipe", {"relative_package": "Nested/Probe"})
        self.assertTrue(recipe["process_state"]["running"])
        self.assertEqual(recipe["process_state"]["processes"], [bootstrap])
        # Bootstrap поддерживается только остановкой: DLL требует внутренний EXE.
        await self.error("sn2_native_load", {"expected_process": bootstrap,
                         "expected_dll_sha256": self.dll_sha256}, "process_path_mismatch")
        self.assertEqual(self.mutation_calls(), [])
        self.responses["stop_sn2_development.ps1"] = CommandResult(0, json.dumps({
            "state": "stopped", "process": bootstrap}))
        stopped = await self.tool("sn2_development_stop", {"expected_process": bootstrap})
        self.assertEqual(stopped["process"], bootstrap)
        self.assertFalse(stopped["outcome_unknown"])
        mutations = self.mutation_calls()
        self.assertEqual(len(mutations), 1)
        args, timeout = mutations[0]
        self.assertEqual(Path(args[args.index("-File") + 1]).name, "stop_sn2_development.ps1")
        self.assertEqual(args[args.index("-ExpectedPid") + 1], "321")
        self.assertEqual(args[args.index("-ExpectedCreationTime") + 1], bootstrap["creation_time"])
        self.assertEqual(timeout, 30)

    async def test_bootstrap_stop_rejects_stale_identity_and_does_not_repeat_unknown_effect(self):
        bootstrap = {"pid": 321, "creation_time": "2026-10-31T00:04:05.7654321Z",
                     "executable": str(self.service.bootstrap_executable)}
        self.processes = [bootstrap]
        foreign_time = {**bootstrap, "creation_time": "2026-10-31T00:04:05.0000000Z"}
        await self.error("sn2_development_stop", {"expected_process": foreign_time}, "process_changed")
        foreign_path = {**bootstrap, "executable": str(self.game / "unrelated.exe")}
        await self.error("sn2_development_stop", {"expected_process": foreign_path}, "process_path_mismatch")
        self.assertEqual(self.mutation_calls(), [])
        self.responses["stop_sn2_development.ps1"] = CommandResult(1, "", "isolated failure after dispatch")
        await self.error("sn2_development_stop", {"expected_process": bootstrap}, "tool_failed", unknown=True)
        self.assertEqual(len(self.mutation_calls()), 1)
        await self.tool("sn2_development_processes")
        self.assertEqual(len(self.mutation_calls()), 1)
        # Процесс уже ушёл: следующий явный запрос не вызывает Stop helper.
        self.processes = []
        await self.error("sn2_development_stop", {"expected_process": bootstrap}, "process_changed")
        self.assertEqual(len(self.mutation_calls()), 1)


if __name__ == "__main__":
    unittest.main()
