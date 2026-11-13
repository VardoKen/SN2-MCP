"""MCP build/lifecycle wrappers preserve actual receipts and invalidate old sessions."""

from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import asyncio
import threading
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.build_workflow import BuildError
from sn2_mcp.development import DevelopmentError
from sn2_mcp.server import create_server


class MCPBuildLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        temporary = tempfile.TemporaryDirectory(prefix="sn2-mcp-workflow-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.native = Mock()
        self.runtime = Mock()
        self.build = Mock()
        self.lifecycle = Mock()
        self.target = {"pid": 1234, "creation_time": "132000000000000000",
                       "executable": "E:/isolated/game/Subnautica2-Win64-Shipping.exe"}
        self.runtime.validate_target.side_effect = lambda target: dict(target)
        patches = ExitStack()
        self.addCleanup(patches.close)
        self.native_factory = patches.enter_context(patch("sn2_mcp.server.NativeClient", return_value=self.native))
        self.runtime_factory = patches.enter_context(patch("sn2_mcp.server.RuntimeService", return_value=self.runtime))
        self.build_factory = patches.enter_context(patch("sn2_mcp.server.BuildWorkflow", return_value=self.build))
        self.lifecycle_factory = patches.enter_context(patch("sn2_mcp.server.RuntimeLifecycle", return_value=self.lifecycle))
        self.server = create_server(self.root, runtime_root=self.root / "runtime-sources",
                                    game_root=self.root / "game", native_port=28223)

    async def result(self, name, arguments):
        result = await self.server.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def failure(self, name, arguments):
        with self.assertRaises(ToolError) as raised:
            await self.server.call_tool(name, arguments)
        message = str(raised.exception)
        return json.loads(message[message.index("{"):])

    async def select(self):
        self.native.request.return_value = {
            "process_id": self.target["pid"], "process_creation_filetime": self.target["creation_time"],
            "session_id": "old-session", "game_ready": True,
        }
        await self.result("sn2_native_status", {"target_process": self.target})
        self.native.request.reset_mock()

    async def test_build_tools_use_one_lazy_service_and_preserve_receipts_without_game_probes(self):
        receipts = {
            "recipe": {"source": "native_build_recipe", "compiled": False, "target": "SN2_MCP"},
            "build": {"build_id": "build-a", "state": "failed", "exact_source_compiled": False,
                      "stages": [{"stage": "build", "returncode": 1}], "artifacts": [],
                      "stdout": "supplied compiler output", "outcome_unknown": False},
            "status": {"build_id": "build-a", "state": "failed", "worker_running": False},
            "diagnostics": {"build_id": "build-a", "text": "raw report\r\n",
                            "compiled": False, "exact_source_compiled": False},
        }
        for operation, receipt in receipts.items():
            getattr(self.build, operation).return_value = receipt
        self.assertEqual(await self.result("sn2_build_recipe", {}), receipts["recipe"])
        self.assertEqual(await self.result("sn2_build", {
            "target": "SN2_MCP", "configuration": "Debug", "run_tests": False,
            "timeout_seconds": 321.5, "parallel": 3,
        }), receipts["build"])
        self.assertEqual(await self.result("sn2_build_status", {"build_id": "build-a"}), receipts["status"])
        self.assertEqual(await self.result("sn2_compile_diagnostics", {"build_id": "build-a"}), receipts["diagnostics"])
        self.build_factory.assert_called_once_with((self.root / "runtime-sources").resolve(), workspace_root=self.root)
        self.build.recipe.assert_called_once_with(target="all", configuration="Release", run_tests=True, project_path="native")
        self.build.build.assert_called_once_with(target="SN2_MCP", configuration="Debug", run_tests=False,
                                                 timeout_seconds=321.5, parallel=3, project_path="native")
        self.build.status.assert_called_once_with(build_id="build-a")
        self.build.diagnostics.assert_called_once_with(build_id="build-a")
        self.runtime_factory.assert_not_called()
        self.lifecycle_factory.assert_not_called()
        self.native.request.assert_not_called()
        self.native.close.assert_not_called()

    async def test_build_errors_keep_partial_receipt_and_do_not_retry_or_probe_game(self):
        receipt = {"build_id": "build-partial", "state": "outcome_unknown", "artifacts": [],
                   "outcome_unknown": True, "stdout": "complete partial report\n"}
        self.build.build.side_effect = BuildError("build_process_unconfirmed", "Build did not confirm exit", receipt)
        error = await self.failure("sn2_build", {"run_tests": False, "timeout_seconds": 12.0})
        self.assertEqual(error, {"code": "build_process_unconfirmed", "message": "Build did not confirm exit",
                                 "receipt": receipt})
        self.assertEqual(self.build.build.call_count, 1)
        self.build.status.assert_not_called()
        self.runtime_factory.assert_not_called()
        self.native.request.assert_not_called()

    async def test_content_or_old_dll_project_selection_directs_to_cooker_before_native_build(self):
        for project_path in ("projects/ContentMod", "mods/opacity"):
            for tool in ("sn2_build_recipe", "sn2_build"):
                with self.subTest(project_path=project_path, tool=tool):
                    error = await self.failure(tool, {"project_path": project_path, "run_tests": False})
                    self.assertEqual(error["code"], "unsupported_project")
                    self.assertIn("sn2_cook_mod", error["message"])
                    self.assertIsNone(error["receipt"])
        self.build_factory.assert_not_called()
        self.build.recipe.assert_not_called()
        self.build.build.assert_not_called()
        self.runtime_factory.assert_not_called()
        self.native.request.assert_not_called()

    async def test_start_receipt_does_not_select_target_or_claim_native_readiness(self):
        receipt = {"state": "started", "process": self.target, "outcome_unknown": False,
                   "native_readiness_checked": False, "launch_receipt": {"state": "launch_requested"}}
        self.lifecycle.start.return_value = receipt
        self.assertEqual(await self.result("sn2_development_start", {"discovery_timeout_seconds": 45.0}), receipt)
        self.lifecycle_factory.assert_called_once_with(self.runtime)
        self.lifecycle.start.assert_called_once_with(discovery_timeout_seconds=45.0)
        status = await self.result("sn2_status", {})
        self.assertIsNone(status["target_process"])
        self.assertFalse(status["game_ready_known"])
        self.native.request.assert_not_called()
        self.native.close.assert_not_called()

    async def test_start_failure_keeps_launch_unknown_effect_without_automatic_cleanup_or_retry(self):
        receipt = {"launch_receipt": {"state": "launch_requested"}, "process_snapshot": {"processes": []}}
        self.lifecycle.start.side_effect = DevelopmentError("game_process_not_observed", "No inner process observed",
                                                            outcome_unknown=True, receipt=receipt)
        error = await self.failure("sn2_development_start", {})
        self.assertEqual(error, {"code": "game_process_not_observed", "message": "No inner process observed",
                                 "outcome_unknown": True, "receipt": receipt})
        self.lifecycle.start.assert_called_once_with(discovery_timeout_seconds=30.0)
        self.lifecycle.stop.assert_not_called()
        self.native.request.assert_not_called()
        self.native.close.assert_not_called()

    async def test_stop_clears_selected_identity_and_shared_native_connection(self):
        await self.select()
        receipt = {"state": "stopped", "process": self.target, "remaining_processes": [], "outcome_unknown": False}
        self.lifecycle.stop.return_value = receipt
        self.assertEqual(await self.result("sn2_development_stop", {"target_process": self.target}), receipt)
        self.lifecycle.stop.assert_called_once_with(self.target)
        self.native.close.assert_called_once_with()
        status = await self.result("sn2_status", {})
        self.assertIsNone(status["target_process"])
        self.native.request.return_value = {"source": "fixture"}
        await self.result("sn2_find_objects", {"query": "fresh"})
        self.native.request.assert_called_once_with("objects", {"query": "fresh", "class_query": "", "limit": 50})

    async def test_restart_returns_fresh_process_and_requires_explicit_new_native_selection(self):
        await self.select()
        fresh = {**self.target, "pid": 5678, "creation_time": "132000000000000001"}
        receipt = {"state": "restarted", "previous_process": self.target, "process": fresh,
                   "native_readiness_checked": False, "outcome_unknown": False}
        self.lifecycle.restart.return_value = receipt
        self.assertEqual(await self.result("sn2_development_restart", {
            "target_process": self.target, "discovery_timeout_seconds": 77.5,
        }), receipt)
        self.lifecycle.restart.assert_called_once_with(self.target, discovery_timeout_seconds=77.5)
        self.native.close.assert_called_once_with()
        self.assertIsNone((await self.result("sn2_status", {}))["target_process"])
        self.native.request.assert_not_called()

    async def test_stop_and_restart_errors_invalidate_stale_identity_and_preserve_unknown_outcome(self):
        for name, operation in (("sn2_development_stop", "stop"), ("sn2_development_restart", "restart")):
            with self.subTest(tool=name):
                await self.select()
                self.native.close.reset_mock()
                receipt = {"previous_process": self.target, "cleanup": [], "outcome_unknown": True}
                getattr(self.lifecycle, operation).side_effect = DevelopmentError(
                    "session_cleanup_incomplete", "The selected instance did not confirm exit",
                    outcome_unknown=True, receipt=receipt)
                error = await self.failure(name, {"target_process": self.target})
                self.assertEqual(error, {"code": "session_cleanup_incomplete",
                                         "message": "The selected instance did not confirm exit",
                                         "outcome_unknown": True, "receipt": receipt})
                self.assertIsNone((await self.result("sn2_status", {}))["target_process"])
                self.native.close.assert_called_once_with()
                self.native.request.assert_not_called()
                self.assertEqual(getattr(self.lifecycle, operation).call_count, 1)

    async def test_known_pretermination_errors_preserve_the_selected_native_session(self):
        failures = (
            ("invalid_timeout", None),
            ("process_changed", {"process_snapshot": {"processes": []}}),
            ("tool_unavailable", None),
            ("game_stop_not_started", {"state": "not_started", "process": self.target,
                                       "cleanup": [], "outcome_unknown": False}),
        )
        for name, operation in (("sn2_development_stop", "stop"), ("sn2_development_restart", "restart")):
            for code, receipt in failures:
                with self.subTest(tool=name, code=code):
                    await self.select()
                    self.native.close.reset_mock()
                    getattr(self.lifecycle, operation).side_effect = DevelopmentError(
                        code, "The termination command did not start", receipt=receipt)
                    error = await self.failure(name, {"target_process": self.target})
                    self.assertEqual(error["code"], code)
                    self.assertFalse(error["outcome_unknown"])
                    self.assertEqual(error["receipt"], receipt)
                    self.assertEqual((await self.result("sn2_status", {}))["target_process"], self.target)
                    self.native.close.assert_not_called()
                    self.native.request.assert_not_called()

    async def test_known_failure_after_completed_stop_invalidates_the_old_native_session(self):
        await self.select()
        receipt = {"previous_process": self.target,
                   "stop_receipt": {"state": "stopped", "process": self.target},
                   "launch_error_receipt": {"state": "not_started", "outcome_unknown": False}}
        self.lifecycle.restart.side_effect = DevelopmentError(
            "game_launch_not_started", "The old game stopped but the new launch did not start", receipt=receipt)
        error = await self.failure("sn2_development_restart", {"target_process": self.target})
        self.assertFalse(error["outcome_unknown"])
        self.assertEqual(error["receipt"], receipt)
        self.native.close.assert_called_once_with()
        self.assertIsNone((await self.result("sn2_status", {}))["target_process"])

    async def test_concurrent_stop_does_not_erase_status_request_identity_check(self):
        entered = threading.Event()
        released = threading.Event()
        fresh = {**self.target, "pid": 5678, "creation_time": "132000000000000001"}

        def delayed_status(operation, parameters):
            self.assertEqual(operation, "status")
            self.assertEqual(parameters["expected_process_id"], self.target["pid"])
            self.assertEqual(parameters["expected_creation_filetime"], self.target["creation_time"])
            entered.set()
            self.assertTrue(released.wait(5), "The deterministic status barrier was not released")
            return {"process_id": fresh["pid"], "process_creation_filetime": fresh["creation_time"],
                    "session_id": "fresh-session"}

        self.native.request.side_effect = delayed_status
        self.lifecycle.stop.return_value = {"state": "stopped", "process": self.target,
                                            "outcome_unknown": False}
        status_task = asyncio.create_task(self.failure("sn2_native_status", {"target_process": self.target}))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 5), "The status request did not enter its barrier")
            await self.result("sn2_development_stop", {"target_process": self.target})
            self.assertIsNone((await self.result("sn2_status", {}))["target_process"])
        finally:
            released.set()
        error = await status_task
        self.assertEqual(error["code"], "native_process_mismatch")


if __name__ == "__main__":
    unittest.main()
