"""Автоматический compiler report проходит существующий MCP и общий нативный клиент."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from mcp.server.mcpserver.exceptions import ToolError

from g1r_mcp.development import DevelopmentService
from g1r_mcp.native_client import NativeError
from g1r_mcp.server import create_server


class RuntimeCompileDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        temporary = tempfile.TemporaryDirectory(prefix="g1r-runtime-diagnostic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository, self.workspace, self.modkit, self.game = [self.root / name
            for name in ("repository", "workspace", "modkit", "game")]
        for directory in (self.repository, self.workspace, self.modkit, self.game):
            directory.mkdir()
        (self.modkit / "Script").mkdir()
        (self.modkit / "Script" / "Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
        self.package_path = "active/Probe"
        self.package = self.workspace / self.package_path
        (self.package / "Script").mkdir(parents=True)
        self.script = self.package / "Script" / "Проба.as"
        self.script.write_bytes("class UПроба {}\r\n".encode("utf-8"))
        (self.package / "GothicMod.json").write_text(json.dumps({
            "name": "Probe", "title": "Probe", "author": "Test", "modVersion": "1.0.0"}), encoding="utf-8")
        self.executor = Mock(side_effect=AssertionError("Диагностика не должна создавать процесс"))
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
            modkit_root=self.modkit, game_root=self.game, executor=self.executor, powershell="isolated-powershell")
        self.addCleanup(self.service.close)
        self.native = Mock()
        native_patch = patch("g1r_mcp.server.NativeClient", return_value=self.native)
        self.native_factory = native_patch.start()
        self.addCleanup(native_patch.stop)
        service_patch = patch("g1r_mcp.server.DevelopmentService", return_value=self.service)
        service_patch.start()
        self.addCleanup(service_patch.stop)
        self.server = create_server(self.modkit, self.workspace, game_root=self.game,
                                    native_port=28123, transport="streamable-http")
        self.manifest = self.service._manifest(self.package_path, show_script_errors=True)
        self.arguments = {"relative_package": self.package_path,
            "expected_manifest_sha256": self.manifest["manifest_sha256"], "show_script_errors": True}

    async def tool(self, arguments):
        result = await self.server.call_tool("g1r_compile_diagnostics", arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    def response(self, text):
        return {"text": text, "process_id": 12345, "source": "FAngelscriptManager::FormatDiagnostics",
                "status": "available"}

    async def test_rich_multiline_text_is_complete_parsed_and_uses_one_shared_native_client(self):
        message = "No matching symbol Ёж 🐉\nTemplate TArray<AActor>\n" + "x" * 5000
        text = ("Angelscript code failed to compile. Please fix errors and save script files to proceed.\r\n\r\n"
            + "<RichTextBlock.Bold>" + str(self.script) + "</>:\r\n"
            + " <Log.Error>(48:1) " + message + "</>\r\n"
            + " <Log.Warning>(12:3) Unused variable</>\r\n"
            + " <NormalText.Subdued>(0:0) Compiler note</>\r\n"
            + " Compiling Проба\r\n")
        producer = self.response(text)
        self.native.request.return_value = producer
        first = await self.tool(self.arguments)
        second = await self.tool({**self.arguments, "report_path": None})
        self.assertEqual(first, second)
        self.assertEqual(first["text"], text)
        self.assertEqual(first["native_report"], producer)
        self.assertEqual(first["report_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())
        self.assertEqual(first["manifest_sha256"], self.manifest["manifest_sha256"])
        self.assertEqual([item["severity"] for item in first["diagnostics"]], ["error", "warning", "info", "info"])
        self.assertEqual(first["diagnostics"][0]["message"], message)
        self.assertEqual(first["diagnostics"][0]["file"], "Script/Проба.as")
        self.assertEqual(first["diagnostics"][0]["source_sha256"], hashlib.sha256(self.script.read_bytes()).hexdigest())
        self.assertEqual((first["diagnostics"][0]["line"], first["diagnostics"][0]["column"]), (48, 1))
        self.assertEqual(first["compilation_state"], "errors_reported")
        self.assertIs(first["compiler_api_available"], True)
        self.assertEqual(first["report_provenance"], "native_compiler_formatter")
        self.assertEqual(first["source_current_correlation"], "unknown")
        self.assertFalse(first["truncated"])
        self.assertFalse(first["compiled"])
        self.assertFalse(first["report_matches_runtime_compile"])
        self.assertEqual(first["process_id"], 12345)
        self.native.request.assert_has_calls([call("compile_diagnostics", {}), call("compile_diagnostics", {})])
        self.assertEqual(self.native.request.call_count, 2)
        self.native_factory.assert_called_once_with(port=28123)
        self.native.close.assert_not_called()
        self.executor.assert_not_called()

    async def test_provided_report_path_uses_original_file_mode_without_native_requests(self):
        text = "\ufeffScript/Проба.as (7:9) : Error: file mode\r\n"
        raw = text.encode("utf-8")
        (self.workspace / "errors.log").write_bytes(raw)
        result = await self.tool({**self.arguments, "report_path": "errors.log"})
        self.assertEqual(result["report_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["manifest_sha256"], self.manifest["manifest_sha256"])
        self.assertEqual(result["diagnostics"][0]["message"], "file mode")
        self.assertEqual(result["diagnostics"][0]["file"], "Script/Проба.as")
        self.assertEqual(result["report_provenance"], "caller_selected_workspace_file")
        self.assertFalse(result["compiler_api_available"])
        self.assertFalse(result["compiled"])
        self.assertFalse(result["report_matches_runtime_compile"])
        self.assertNotIn("native_report", result)
        self.native.request.assert_not_called()
        self.native_factory.assert_called_once_with(port=28123)
        self.executor.assert_not_called()

    async def test_empty_native_formatter_text_does_not_claim_success(self):
        self.native.request.return_value = self.response("")
        result = await self.tool(self.arguments)
        self.assertEqual(result["text"], "")
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["compilation_state"], "unknown")
        self.assertFalse(result["compiled"])
        self.assertFalse(result["report_matches_runtime_compile"])
        self.native.request.assert_called_once_with("compile_diagnostics", {})
        self.executor.assert_not_called()

    def reload_response(self, path, *, success=True):
        return {**self.response(""), "last_completed_reload": {
            "attempt_id": 9, "completion_id": 9, "mode": 1, "success": success,
            "script_generation_before": 3, "script_generation_after": 4 if success else 3,
            "changed_files": [{"first": str(path), "second": "opaque-pair-value"}],
            "capture_complete": True}, "reload_attempt_started": 9, "reload_attempt_completed": 9,
            "compiler_read_reload_context_consistent": True}

    async def test_successful_staged_reload_matches_selected_source_without_compiled_byte_claim(self):
        staged_path = "artifacts/session/packages/Probe"
        staged = self.workspace / staged_path
        (staged / "Script").mkdir(parents=True)
        (staged / "GothicMod.json").write_bytes((self.package / "GothicMod.json").read_bytes())
        staged_script = staged / "Script" / self.script.name
        staged_script.write_bytes("class UСтадия {}\r\n".encode("utf-8"))
        self.service._launch_observation = {"launch_id": "launch-1", "stage": "process_running",
            "process": {"pid": 12345}, "development_staging": {"packages": [{
                "source_package_path": self.package_path, "relative_package": staged_path}]}}
        producer = self.reload_response(str(staged_script).replace("/", "\\").upper())
        self.native.request.return_value = producer
        selections = [self.package_path, staged_path]
        if os.name == "nt":
            selections += ["ACTIVE/Probe", "ARTIFACTS/session/packages/Probe"]
        for selected in selections:
            with self.subTest(selected=selected):
                result = await self.tool({**self.arguments, "relative_package": selected,
                    "expected_manifest_sha256": self.service._manifest(selected,
                        show_script_errors=True)["manifest_sha256"]})
                self.assertTrue(result["observed_successful_reload"])
                self.assertTrue(result["reload_event_matches_selected_file"])
                self.assertTrue(result["reload_event_matches_selected_package"])
                self.assertEqual(result["last_completed_reload"], producer["last_completed_reload"])
                self.assertEqual(result["reload_attempt_started"], 9)
                self.assertEqual(result["reload_attempt_completed"], 9)
                match, = result["reload_event_file_matches"]
                self.assertEqual((match["role"], match["file_path"], match["pair_field"]),
                                 ("staged", "Script/Проба.as", "first"))
                self.assertEqual(match["current_file_sha256"], hashlib.sha256(staged_script.read_bytes()).hexdigest())
                self.assertNotEqual(match["current_file_sha256"], hashlib.sha256(self.script.read_bytes()).hexdigest())
                self.assertTrue(result["manifest_stable_during_read"])
                self.assertTrue(result["package_observations_complete"])
                self.assertTrue(result["compiler_read_reload_context_consistent"])
                self.assertEqual(result["reload_event_file_pair_semantics"], "unknown")
                self.assertFalse(result["compiled"])
                self.assertFalse(result["exact_source_compiled"])
                self.assertEqual(result["source_current_correlation"], "unknown")
        (staged / "GothicMod.json").unlink()
        self.native.request.return_value = self.reload_response(self.script)
        result = await self.tool(self.arguments)
        self.assertTrue(result["observed_successful_reload"])
        self.assertTrue(result["reload_event_matches_selected_file"])
        self.assertFalse(result["package_observations_complete"])
        unavailable, = [item for item in result["package_observations"] if item["role"] == "staged"]
        self.assertFalse(unavailable["available_before"])
        self.assertFalse(unavailable["available_after"])
        self.assertEqual(unavailable["error_after"]["code"], "invalid_package")
        self.assertFalse(result["compiled"])
        self.assertFalse(result["exact_source_compiled"])
        self.native.request.return_value = producer
        self.service._launch_observation["process"]["pid"] = 12346
        result = await self.tool(self.arguments)
        self.assertTrue(result["observed_successful_reload"])
        self.assertFalse(result["reload_event_matches_selected_package"])
        self.assertEqual([item["role"] for item in result["package_observations"]], ["selected"])
        self.executor.assert_not_called()

    async def test_other_package_same_basename_and_failed_reload_remain_distinct(self):
        cases = [(self.workspace / "other" / "Probe" / "Script" / self.script.name, True, False),
                 (self.script, False, True)]
        for path, success, matched in cases:
            with self.subTest(path=path, success=success):
                self.native.request.return_value = self.reload_response(path, success=success)
                result = await self.tool(self.arguments)
                self.assertIs(result["observed_successful_reload"], success)
                self.assertIs(result["reload_event_matches_selected_file"], matched)
                self.assertIs(result["reload_event_matches_selected_package"], matched)
                self.assertFalse(result["exact_source_compiled"])
                self.assertFalse(result["compiled"])
                self.assertFalse(result["report_matches_runtime_compile"])
        self.executor.assert_not_called()

    async def test_matching_reload_path_with_changed_source_reports_current_hash_and_instability(self):
        original_hash = hashlib.sha256(self.script.read_bytes()).hexdigest()
        producer = self.reload_response(self.script)
        def change_source(*_):
            self.script.write_bytes("class UИзменённаяПроба {}\r\n".encode("utf-8"))
            return producer
        self.native.request.side_effect = change_source
        result = await self.tool(self.arguments)
        self.assertTrue(result["observed_successful_reload"])
        self.assertTrue(result["reload_event_matches_selected_file"])
        self.assertFalse(result["manifest_stable_during_read"])
        self.assertFalse(result["expected_manifest_matches_current"])
        match, = result["reload_event_file_matches"]
        self.assertEqual(match["file_sha256_before"], original_hash)
        self.assertEqual(match["current_file_sha256"], hashlib.sha256(self.script.read_bytes()).hexdigest())
        self.assertFalse(match["file_stable_during_read"])
        self.assertFalse(result["exact_source_compiled"])
        self.assertFalse(result["compiled"])
        self.assertEqual(result["source_current_correlation"], "unknown")
        self.native.request.assert_called_once_with("compile_diagnostics", {})
        self.executor.assert_not_called()

    async def test_native_error_keeps_request_identity_and_effect_and_is_not_retried(self):
        effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        self.native.request.side_effect = NativeError("compiler_unavailable", "Нет runtime formatter",
                                                      False, "request-compiler-1", effect)
        with self.assertRaises(ToolError) as caught:
            await self.server.call_tool("g1r_compile_diagnostics", self.arguments)
        rendered = str(caught.exception)
        error = json.loads(rendered[rendered.index("{"):])
        self.assertEqual(error["code"], "compiler_unavailable")
        self.assertEqual(error["request_id"], "request-compiler-1")
        self.assertEqual(error["effect"], effect)
        self.assertIs(error["may_have_executed"], False)
        self.native.request.assert_called_once_with("compile_diagnostics", {})
        self.native_factory.assert_called_once_with(port=28123)
        self.executor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
