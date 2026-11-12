"""Compiled package and mod MCP orchestration preserve identities and effects."""

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, call, patch

from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.development import DevelopmentError
from sn2_mcp.mod_projects import ModProjectError
from sn2_mcp.server import create_server


class MCPModWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        temporary = tempfile.TemporaryDirectory(prefix="sn2-mcp-mod-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime_root = self.root / "runtime"
        self.runtime_root.mkdir()
        self.bridge = self.runtime_root / "SN2_MCP.dll"
        self.bridge.write_bytes(b"isolated bridge identity")
        self.bridge_sha = hashlib.sha256(self.bridge.read_bytes()).hexdigest()
        self.old = {"pid": 1234, "creation_time": "132000000000000000",
                    "executable": "E:/isolated/game/Subnautica2-Win64-Shipping.exe"}
        self.fresh = {**self.old, "pid": 5678, "creation_time": "132000000000000001"}
        self.package_path = "artifacts/mod-packages/revision-b"
        self.package = {
            "manifest_sha256": "a" * 64, "source_revision": "b" * 64,
            "artifact": {"path": str(self.root / "artifacts/mod-packages/revision-b/Probe.dll"),
                         "sha256": "c" * 64},
            "metadata": {"mod_id": "Probe"}, "verified": True,
        }
        self.projects = Mock()
        self.projects.verify.return_value = self.package
        self.runtime = Mock()
        self.runtime.validate_target.side_effect = lambda target: dict(target)
        self.mod = Mock()
        self.lifecycle = Mock()
        self.native_old = Mock()
        self.native_fresh = Mock()
        self.native_factory_count = 0

        def native_factory(**kwargs):
            self.native_factory_count += 1
            return self.native_old if self.native_factory_count == 1 else self.native_fresh

        patches = ExitStack()
        self.addCleanup(patches.close)
        self.native_factory = patches.enter_context(patch("sn2_mcp.server.NativeClient", side_effect=native_factory))
        self.runtime_factory = patches.enter_context(patch("sn2_mcp.server.RuntimeService", return_value=self.runtime))
        self.mod_factory = patches.enter_context(patch("sn2_mcp.server.ModRuntime", return_value=self.mod))
        self.projects_factory = patches.enter_context(patch("sn2_mcp.server.ModProjects", return_value=self.projects))
        self.lifecycle_factory = patches.enter_context(patch("sn2_mcp.server.RuntimeLifecycle", return_value=self.lifecycle))
        self.server = create_server(self.root, runtime_root=self.runtime_root, game_root=self.root / "game")

    async def result(self, name, arguments):
        result = await self.server.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def failure(self, name, arguments):
        with self.assertRaises(ToolError) as raised:
            await self.server.call_tool(name, arguments)
        message = str(raised.exception)
        return json.loads(message[message.index("{"):])

    def arguments(self, **updates):
        return {"target_process": self.old, "package_path": self.package_path,
                "expected_manifest_sha256": self.package["manifest_sha256"], **updates}

    def reload_arguments(self, **updates):
        return {**self.arguments(bridge_dll_path=str(self.bridge), expected_bridge_sha256=self.bridge_sha,
                                 discovery_timeout_seconds=41.0, readiness_timeout_seconds=12.0,
                                 timeout_seconds=23.0), **updates}

    def mod_receipt(self, *, operation="load", state="activation_pending"):
        return {"process": self.old, "operation": operation, "module_loaded": True,
                "outcome_unknown": False, "dll_sha256": self.package["artifact"]["sha256"],
                "mod_receipt": {"status": {"mod_id": "Probe", "source_revision": self.package["source_revision"],
                                            "state": state, "semantic_effect_verified": False}},
                "residency": "process_lifetime", "hot_unload_supported": False}

    async def test_create_validate_package_and_verify_return_actual_service_results_without_game_actions(self):
        receipts = {
            "create": {"project_path": "mods/probe", "kind": "native_widget_opacity", "compiled": False},
            "validate": {"valid": True, "project_path": "mods/probe", "compiled": False},
            "package": {"destination": self.package_path, "compiled_package": True, "installed": False},
        }
        for operation, receipt in receipts.items():
            getattr(self.projects, operation).return_value = receipt
        self.assertEqual(await self.result("sn2_create_mod_project", {"project_path": "mods/probe", "mod_id": "Probe"}),
                         receipts["create"])
        self.assertEqual(await self.result("sn2_validate_mod_project", {"project_path": "mods/probe"}), receipts["validate"])
        self.assertEqual(await self.result("sn2_package_artifact", {
            "build_receipt_path": "artifacts/builds/build-b/receipt.json", "destination": self.package_path,
        }), receipts["package"])
        self.assertEqual(await self.result("sn2_verify_artifact_package", {"package_path": self.package_path}), self.package)
        self.projects.create.assert_called_once_with("mods/probe", "Probe")
        self.projects.validate.assert_called_once_with("mods/probe")
        self.projects.package.assert_called_once_with("artifacts/builds/build-b/receipt.json", self.package_path)
        self.projects.verify.assert_called_once_with(self.package_path)
        self.runtime_factory.assert_not_called()
        self.mod_factory.assert_not_called()
        self.native_old.request.assert_not_called()

    async def test_load_status_and_disable_use_exact_verified_artifact_and_preserve_effect_receipts(self):
        for tool, operation in (("sn2_mod_load", "load"), ("sn2_mod_status", "status"), ("sn2_mod_disable", "disable")):
            with self.subTest(tool=tool):
                receipt = self.mod_receipt(operation=operation, state="disabled" if operation == "disable" else "activation_pending")
                getattr(self.mod, operation).return_value = receipt
                arguments = self.arguments(timeout_seconds=17.5)
                expected_kwargs = {"timeout_seconds": 17.5}
                if operation == "status":
                    arguments["request_id"] = 7823
                    expected_kwargs["request_id"] = 7823
                result = await self.result(tool, arguments)
                self.assertEqual(result, {**receipt, "package": self.package})
                getattr(self.mod, operation).assert_called_once_with(
                    self.old, self.package["artifact"]["path"], self.package["artifact"]["sha256"], **expected_kwargs)
        self.assertEqual(self.projects.verify.call_count, 3)
        self.assertEqual(self.mod_factory.call_args, call(self.runtime, workspace_root=self.root))
        self.lifecycle_factory.assert_not_called()
        self.native_old.request.assert_not_called()
        self.native_old.close.assert_not_called()

    async def test_package_and_artifact_failures_precede_game_mutation(self):
        for tool in ("sn2_mod_load", "sn2_mod_disable", "sn2_mod_reload"):
            with self.subTest(tool=tool):
                self.projects.verify.side_effect = ModProjectError("package_corrupt", "The compiled artifact hash changed")
                arguments = self.reload_arguments() if tool == "sn2_mod_reload" else self.arguments()
                error = await self.failure(tool, arguments)
                self.assertEqual(error["code"], "package_corrupt")
        self.projects.verify.side_effect = None
        error = await self.failure("sn2_mod_reload", self.reload_arguments(expected_manifest_sha256="d" * 64))
        self.assertEqual(error["code"], "package_changed")
        error = await self.failure("sn2_mod_reload", self.reload_arguments(expected_bridge_sha256="e" * 64))
        self.assertEqual(error["code"], "bridge_changed")
        self.lifecycle_factory.assert_not_called()
        self.runtime_factory.assert_not_called()
        self.mod_factory.assert_not_called()
        self.native_old.close.assert_not_called()

    async def test_runtime_unknown_effect_keeps_controller_receipt_and_does_not_replay(self):
        receipt = {"request_id": 99, "process": self.old, "dll_sha256": self.package["artifact"]["sha256"],
                   "module_loaded": True, "outcome_unknown": True, "effect": {"activation": "unknown"}}
        self.mod.load.side_effect = DevelopmentError("mod_control_unconfirmed", "Mod activation did not confirm completion",
                                                    outcome_unknown=True, receipt=receipt)
        error = await self.failure("sn2_mod_load", self.arguments())
        self.assertEqual(error, {"code": "mod_control_unconfirmed", "message": "Mod activation did not confirm completion",
                                 "outcome_unknown": True, "receipt": receipt})
        self.mod.load.assert_called_once()
        self.mod.status.assert_not_called()
        self.mod.disable.assert_not_called()
        self.lifecycle_factory.assert_not_called()

    async def test_initialized_revision_mismatch_is_reported_after_actual_call_without_retry(self):
        receipt = self.mod_receipt()
        receipt["mod_receipt"]["status"]["source_revision"] = "f" * 64
        self.mod.load.return_value = receipt
        error = await self.failure("sn2_mod_load", self.arguments())
        self.assertEqual(error["code"], "mod_revision_mismatch")
        self.assertEqual(error["receipt"], receipt)
        self.mod.load.assert_called_once()
        self.mod.disable.assert_not_called()
        self.lifecycle_factory.assert_not_called()

    async def test_reload_is_restart_then_fresh_bridge_readiness_then_mod_initialization(self):
        restart = {"state": "restarted", "previous_process": self.old, "process": self.fresh,
                   "native_readiness_checked": False, "outcome_unknown": False}
        self.lifecycle.restart.return_value = restart
        loaded = {"state": "loaded", "process": self.fresh, "game_ready": False, "outcome_unknown": False}
        self.runtime.load_native.return_value = loaded
        native_status = {"process_id": self.fresh["pid"], "process_creation_filetime": self.fresh["creation_time"],
                         "session_id": "fresh-native", "ready": False}
        enabled = {**native_status, "ready": True, "tick_count": 7}
        self.native_fresh.request.side_effect = [native_status, native_status, enabled]
        receipt = self.mod_receipt()
        receipt["process"] = self.fresh
        self.mod.load.return_value = receipt
        order = Mock()
        order.attach_mock(self.lifecycle.restart, "restart")
        order.attach_mock(self.native_old.close, "close_old")
        order.attach_mock(self.runtime.load_native, "load_bridge")
        order.attach_mock(self.native_fresh.request, "request")
        order.attach_mock(self.mod.load, "load_mod")
        result = await self.result("sn2_mod_reload", self.reload_arguments())
        self.assertEqual(result["state"], "revision_initialization_requested")
        self.assertEqual(result["iteration"], "controlled_process_restart")
        self.assertFalse(result["hot_reload"])
        self.assertEqual(result["process"], self.fresh)
        self.assertEqual(result["previous_process"], self.old)
        self.assertEqual(result["native_status"], enabled)
        self.assertEqual(result["mod"], {**receipt, "package": self.package})
        self.assertEqual([event[0] for event in order.mock_calls],
                         ["restart", "close_old", "load_bridge", "request", "request", "request", "load_mod"])
        self.lifecycle.restart.assert_called_once_with(self.old, discovery_timeout_seconds=41.0)
        self.runtime.load_native.assert_called_once_with(self.fresh, str(self.bridge.resolve()), self.bridge_sha)
        self.assertEqual([event.args[0] for event in self.native_fresh.request.call_args_list],
                         ["status", "status", "enable_game"])
        self.assertTrue(all(event.args[1]["expected_process_id"] == self.fresh["pid"]
                            and event.args[1]["expected_creation_filetime"] == self.fresh["creation_time"]
                            for event in self.native_fresh.request.call_args_list))
        self.mod.load.assert_called_once_with(self.fresh, self.package["artifact"]["path"],
                                              self.package["artifact"]["sha256"], timeout_seconds=23.0)
        self.native_old.request.assert_not_called()
        self.assertEqual(self.native_factory_count, 2)

    async def test_reload_failure_after_restart_preserves_fresh_process_and_does_not_initialize_mod(self):
        restart = {"state": "restarted", "previous_process": self.old, "process": self.fresh, "outcome_unknown": False}
        self.lifecycle.restart.return_value = restart
        receipt = {"process": self.fresh, "dll_sha256": self.bridge_sha, "outcome_unknown": True,
                   "module_loaded": True}
        self.runtime.load_native.side_effect = DevelopmentError("native_load_unconfirmed", "Native loading did not confirm exit",
                                                               outcome_unknown=True, receipt=receipt)
        error = await self.failure("sn2_mod_reload", self.reload_arguments())
        self.assertEqual(error["code"], "native_load_unconfirmed")
        self.assertTrue(error["outcome_unknown"])
        self.assertEqual(error["receipt"], receipt)
        self.lifecycle.restart.assert_called_once()
        self.runtime.load_native.assert_called_once()
        self.mod.load.assert_not_called()
        self.native_old.request.assert_not_called()
        self.native_fresh.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
