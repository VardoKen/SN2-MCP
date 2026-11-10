"""Режим ошибок компилятора: совместимость рецептов, запуск и публичный MCP bool-контракт."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from g1r_mcp.development import CommandResult, DevelopmentError, DevelopmentService
from g1r_mcp.server import create_server


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class ErrorModeFixture:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="g1r-error-mode-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "tools").mkdir(parents=True)
        for name in ("game_process_state.ps1", "start_g1r_development.ps1"):
            (self.repository / "tools" / name).write_text("# executor fixture\n", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.modkit = self.root / "modkit"
        self.game = self.root / "game"
        for directory in (self.workspace, self.modkit, self.game):
            directory.mkdir()
        (self.modkit / "Script").mkdir()
        (self.modkit / "Script" / "Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
        self.main_path = "user/Main"
        self.helper_path = "owned/Helper"
        for relative, name in ((self.main_path, "Main"), (self.helper_path, "Helper")):
            package = self.workspace / relative
            (package / "Script").mkdir(parents=True)
            (package / "Script" / "Entry.as").write_text("class U" + name + " {}\n", encoding="utf-8")
            (package / "GothicMod.json").write_text(json.dumps({
                "name": name, "title": name, "author": "Test", "modVersion": "1.0.0"}), encoding="utf-8")
        self.calls = []
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
            modkit_root=self.modkit, game_root=self.game, executor=self.execute, powershell="isolated-powershell")
        self.addCleanup(self.service.close)

    def execute(self, arguments, timeout):
        self.calls.append(list(arguments))
        script = Path(arguments[arguments.index("-File") + 1]).name
        if script == "game_process_state.ps1":
            return CommandResult(0, json.dumps({"processes": []}))
        if script == "start_g1r_development.ps1":
            if "-ModsCsv" in arguments:
                names = arguments[arguments.index("-ModsCsv") + 1].split(",")
                result = {"state": "launch_requested", "package_name": None, "mods": names,
                          "mods_directory": arguments[arguments.index("-ModsDirectory") + 1]}
            else:
                result = {"state": "launch_requested", "package_name": "Main"}
            return CommandResult(0, json.dumps(result))
        raise AssertionError("Неожиданный процессный вызов " + script)

    def start_calls(self):
        return [call for call in self.calls
                if Path(call[call.index("-File") + 1]).name == "start_g1r_development.ps1"]

    def legacy_single_digest(self, manifest):
        return digest({key: manifest[key] for key in (
            "package_path", "package_name", "workspace_root", "modkit_root", "game_root", "files", "launch_options")})

    def legacy_composition_digest(self, manifest):
        return digest({key: manifest[key] for key in (
            "composition", "package_path", "package_name", "workspace_root", "modkit_root", "game_root",
            "packages", "selected_mods", "launch_options")})


class DevelopmentErrorModeTests(ErrorModeFixture, unittest.TestCase):
    def test_false_preserves_historical_single_and_composition_hashes(self):
        for companions in (None, [self.helper_path]):
            with self.subTest(companions=companions):
                default = self.service.recipe(self.main_path, companion_packages=companions)["manifest"]
                disabled = self.service.recipe(self.main_path, companion_packages=companions,
                                               show_script_errors=False)["manifest"]
                self.assertEqual(default, disabled)
                self.assertEqual(disabled["launch_options"], {"enable_ingame_tools": False})
                if companions:
                    for package in disabled["packages"]:
                        self.assertEqual(package["launch_options"], {"enable_ingame_tools": False})
                        self.assertEqual(package["manifest_sha256"], self.legacy_single_digest(package))
                    expected = self.legacy_composition_digest(disabled)
                else:
                    expected = self.legacy_single_digest(disabled)
                self.assertEqual(disabled["manifest_sha256"], expected)
        self.assertFalse((self.workspace / "artifacts").exists())
        self.assertEqual(self.start_calls(), [])

    def test_true_is_bound_to_each_manifest_and_refuses_opposite_mode(self):
        for companions in (None, [self.helper_path]):
            with self.subTest(companions=companions):
                normal = self.service.recipe(self.main_path, companion_packages=companions)["manifest"]
                shown = self.service.recipe(self.main_path, companion_packages=companions,
                                            show_script_errors=True)["manifest"]
                self.assertNotEqual(normal["manifest_sha256"], shown["manifest_sha256"])
                self.assertEqual(shown["launch_options"], {"enable_ingame_tools": False, "show_script_errors": True})
                for package in shown.get("packages", []):
                    self.assertIs(package["launch_options"]["show_script_errors"], True)
                for manifest, flag in ((normal, True), (shown, False)):
                    calls_before = list(self.calls)
                    with self.assertRaises(DevelopmentError) as caught:
                        self.service.start(self.main_path, manifest["manifest_sha256"],
                                           companion_packages=companions, show_script_errors=flag)
                    self.assertEqual(caught.exception.code, "package_changed")
                    self.assertEqual(self.calls, calls_before)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_mode_controls_launcher_and_observer_for_single_and_staged_packages(self):
        for companions in (None, [self.helper_path]):
            for flag in (False, True):
                with self.subTest(companions=companions, flag=flag):
                    recipe = self.service.recipe(self.main_path, companion_packages=companions,
                                                  show_script_errors=flag)
                    started = self.service.start(self.main_path, recipe["manifest"]["manifest_sha256"],
                        companion_packages=companions, show_script_errors=flag)
                    arguments = self.start_calls()[-1]
                    self.assertEqual(arguments.count("-ShowScriptErrors"), int(flag))
                    observation = started["launch_observation"]
                    self.assertIs(observation["show_script_errors"], flag)
                    self.assertIs(observation["compiler_exit_on_error"], not flag)
                    self.assertIs(observation["compiled_confirmed"], False)
                    self.assertIs(observation["compiler_failure_confirmed"], False)
                    if companions:
                        stage = started["development_staging"]
                        for package in stage["packages"]:
                            options = package["manifest"]["launch_options"]
                            self.assertEqual(options.get("show_script_errors", False), flag)
                            current = self.service._manifest(package["relative_package"], show_script_errors=flag)
                            self.assertEqual(current["manifest_sha256"], package["manifest_sha256_at_launch"])

    def test_service_rejects_non_booleans_before_read_or_executor(self):
        for value in (0, 1, "true", "false", None, [], {}):
            for companions in (None, [self.helper_path]):
                for operation in ("recipe", "start"):
                    with self.subTest(value=value, companions=companions, operation=operation):
                        calls_before = list(self.calls)
                        with self.assertRaises(DevelopmentError) as caught:
                            arguments = (self.main_path,) if operation == "recipe" else (self.main_path, "a" * 64)
                            getattr(self.service, operation)(*arguments, companion_packages=companions,
                                                            show_script_errors=value)
                        self.assertEqual(caught.exception.code, "invalid_arguments")
                        self.assertEqual(self.calls, calls_before)
        self.assertFalse((self.workspace / "artifacts").exists())


class MCPDevelopmentErrorModeTests(ErrorModeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        factory = patch("g1r_mcp.server.DevelopmentService", return_value=self.service)
        factory.start()
        self.addCleanup(factory.stop)
        self.server = create_server(self.modkit, self.workspace, game_root=self.game)

    async def tool(self, name, arguments):
        result = await self.server.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def test_tools_accept_true_and_false_and_forward_mode_into_launch(self):
        for flag in (False, True):
            arguments = {"relative_package": self.main_path, "companion_packages": [self.helper_path],
                         "show_script_errors": flag}
            recipe = await self.tool("g1r_development_recipe", arguments)
            self.assertEqual(recipe["manifest"]["launch_options"].get("show_script_errors", False), flag)
            started = await self.tool("g1r_development_start", {
                **arguments, "expected_manifest_sha256": recipe["manifest"]["manifest_sha256"]})
            self.assertEqual(self.start_calls()[-1].count("-ShowScriptErrors"), int(flag))
            self.assertIs(started["launch_observation"]["compiler_exit_on_error"], not flag)

    async def test_tools_strict_bool_rejects_coercion_before_executor(self):
        for value in (0, 1, "true", "false", None, [], {}):
            for tool in ("g1r_development_recipe", "g1r_development_start"):
                with self.subTest(tool=tool, value=value):
                    arguments = {"relative_package": self.main_path, "show_script_errors": value}
                    if tool == "g1r_development_start":
                        arguments["expected_manifest_sha256"] = "a" * 64
                    calls_before = list(self.calls)
                    with self.assertRaises(ToolError):
                        await self.server.call_tool(tool, arguments)
                    self.assertEqual(self.calls, calls_before)
        self.assertFalse((self.workspace / "artifacts").exists())

    async def test_true_mode_compiler_report_accepts_recipe_hash_and_binds_source(self):
        recipe = await self.tool("g1r_development_recipe", {
            "relative_package": self.main_path, "show_script_errors": True})
        manifest = recipe["manifest"]
        source = next(item for item in manifest["files"] if item["path"] == "Script/Entry.as")
        (self.workspace / "compiler-report.txt").write_text(
            "Script/Entry.as (3:2) : Error: isolated diagnostic", encoding="utf-8")
        result = await self.tool("g1r_compile_diagnostics", {
            "relative_package": self.main_path, "expected_manifest_sha256": manifest["manifest_sha256"],
            "report_path": "compiler-report.txt", "show_script_errors": True})
        self.assertIs(manifest["launch_options"]["show_script_errors"], True)
        self.assertEqual(result["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(result["compilation_state"], "errors_reported")
        self.assertEqual(len(result["diagnostics"]), 1)
        diagnostic = result["diagnostics"][0]
        self.assertEqual(diagnostic["file"], "Script/Entry.as")
        self.assertEqual(diagnostic["source_sha256"], source["sha256"])
        self.assertEqual((diagnostic["line"], diagnostic["column"]), (3, 2))
        self.assertFalse(result["compiled"])
        self.assertFalse(result["report_matches_runtime_compile"])
        self.assertEqual(self.start_calls(), [])


if __name__ == "__main__":
    unittest.main()
