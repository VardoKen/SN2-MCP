"""Content/Blueprint frontend composition with neutral files and simulated runtime receipts."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.content_projects import ContentProjectError
from sn2_mcp.development import DevelopmentError
from sn2_mcp.native_client import NativeError
from sn2_mcp.server import create_server


def failure(response):
    text = next(item.text for item in response.content if item.type == "text")
    return json.loads(text[text.index("{"):])


async def tool_call(server, name, arguments):
    try:
        return await server.call_tool(name, arguments)
    except ToolError as error:
        return SimpleNamespace(is_error=True, content=[SimpleNamespace(type="text", text=str(error))])


class ContentFrontendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_create_validate_and_blocked_cook_do_not_claim_authored_or_compiled_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory:
                server = create_server(root, modkit_root=root / "missing-modkit",
                    engine_root=root / "missing-engine", game_root=root / "missing-game")
                created = await tool_call(server, "sn2_create_mod_project", {
                    "project_path": "projects/Neutral", "mod_id": "Neutral"})
                self.assertFalse(created.is_error, created.content)
                self.assertFalse(created.structured_content["compiled"])
                validated = await tool_call(server, "sn2_validate_mod_project", {"project_path": "projects/Neutral"})
                self.assertFalse(validated.is_error, validated.content)
                self.assertTrue(validated.structured_content["valid"])
                self.assertFalse(validated.structured_content["authored_assets_present"])
                cooked = await tool_call(server, "sn2_cook_mod", {"project_path": "projects/Neutral"})
                self.assertFalse(cooked.is_error, cooked.content)
                receipt = cooked.structured_content
                self.assertEqual(receipt["state"], "blocked")
                self.assertFalse(receipt["exact_source_compiled"])
                self.assertFalse(receipt["installed_in_game"])
                self.assertTrue(receipt["cleanup"]["tree_exit_confirmed"])
                status = await tool_call(server, "sn2_cook_status", {"build_id": receipt["build_id"]})
                self.assertEqual(status.structured_content["state"], "blocked")
                packaged = await tool_call(server, "sn2_package_artifact", {
                    "cook_receipt_path": Path(receipt["receipt_path"]).relative_to(root).as_posix(),
                    "destination": "packages/Neutral"})
                self.assertTrue(packaged.is_error)
                self.assertFalse((root / "packages" / "Neutral").exists())
                runtime_factory.assert_not_called()
                native.request.assert_not_called()

    async def test_configured_authoring_roots_are_local_metadata_and_cooker_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cooker = Mock()
            cooker.inspect.return_value = {"missing": ["engine"], "compiled": False}
            with patch("sn2_mcp.server.NativeClient"), \
                 patch("sn2_mcp.server.ContentCooker", return_value=cooker) as factory, \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory:
                server = create_server(root, modkit_root=root / "Modkit", engine_root=root / "Engine")
                status = await tool_call(server, "sn2_status", {})
                self.assertEqual(status.structured_content["authoring_dependencies"], {
                    "modkit_root": str(root / "Modkit"), "engine_root": str(root / "Engine"), "checked": False})
                factory.assert_not_called()
                await tool_call(server, "sn2_content_environment", {})
                self.assertEqual(factory.call_args.kwargs["workspace_root"], root)
                self.assertEqual(factory.call_args.kwargs["modkit_root"], root / "Modkit")
                self.assertEqual(factory.call_args.kwargs["engine_root"], root / "Engine")
                runtime_factory.assert_not_called()

    async def test_asset_blueprint_wire_preserves_identity_labels_and_unknown_effect_without_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = {"pid": 100, "creation_time": "1234567"}
            native, runtime = Mock(), Mock()
            runtime.validate_target.return_value = target
            native.request.side_effect = [
                {"value": {"handle": "asset"}, "stage": "loaded"},
                {"value": {"handle": "class"}},
                {"mod_id": "Neutral", "state": "spawn_finished"},
                NativeError("native_timeout", "A response was lost", True, "request-4",
                    {"process_event_started": True, "process_event_returned": False}),
                {"mod_id": "Neutral", "state": "destroy_observed"},
            ]
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService", return_value=runtime):
                server = create_server(Path(temporary))
                requests = [
                    ("sn2_load_asset", {"asset_path": "/Neutral/Content.Asset", "target_process": target}, "load_asset"),
                    ("sn2_load_class", {"asset_path": "/Neutral/BP_Neutral.BP_Neutral_C"}, "load_class"),
                    ("sn2_blueprint_mod_enable", {"mod_id": "Neutral", "entry_class_path": "/Neutral/BP_Neutral.BP_Neutral_C",
                        "package_revision": "different-label", "source_revision": "source-B"}, "blueprint_mod_enable"),
                    ("sn2_blueprint_mod_status", {"mod_id": "Neutral"}, "blueprint_mod_status"),
                    ("sn2_blueprint_mod_disable", {"mod_id": "Neutral"}, "blueprint_mod_disable"),
                ]
                for index, (name, arguments, operation) in enumerate(requests):
                    response = await tool_call(server, name, arguments)
                    self.assertEqual(response.is_error, index == 3, response.content)
                    self.assertEqual(native.request.call_count, index + 1)
                    call = native.request.call_args
                    self.assertEqual(call.args[0], operation)
                    self.assertEqual(call.args[1]["expected_process_id"], target["pid"])
                    self.assertEqual(call.args[1]["expected_creation_filetime"], target["creation_time"])
                    if index == 2:
                        self.assertEqual(call.args[1]["package_revision"], "different-label")
                    if index == 3:
                        error = failure(response)
                        self.assertTrue(error["may_have_executed"])
                        self.assertEqual(error["request_id"], "request-4")
                        self.assertFalse(error["effect"]["process_event_returned"])

    def package(self):
        return {"valid": True, "compiled": True, "plugin_id": "Neutral", "manifest_sha256": "a" * 64,
            "source_revision": "b" * 64, "activation": {"entry_class": "/Neutral/BP_Neutral.BP_Neutral_C"}}

    async def test_content_package_load_uses_blueprint_lifecycle_and_separate_installation_facts(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = {"pid": 100, "creation_time": "1234567"}
            native, runtime, projects, deployment = Mock(), Mock(), Mock(), Mock()
            runtime.validate_target.return_value = target
            package = self.package()
            projects.verify.return_value = package
            installation = {"identified_installation": True, "receipt": {"package_manifest_sha256": "a" * 64}}
            deployment.status.return_value = installation
            native.request.return_value = {"mod_id": "Neutral", "state": "spawn_finished", "authored_effect": "unverified"}
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService", return_value=runtime), \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(Path(temporary))
                result = await tool_call(server, "sn2_mod_load", {"target_process": target, "package_path": "packages/Neutral"})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(native.request.call_args.args[0], "blueprint_mod_enable")
                self.assertEqual(native.request.call_args.args[1]["entry_class_path"], package["activation"]["entry_class"])
                self.assertEqual(native.request.call_args.args[1]["package_revision"], "a" * 64)
                self.assertEqual(result.structured_content["installation"], installation)
                self.assertEqual(result.structured_content["plugin_mount"], "not_checked_by_this_composition")
                self.assertIn("independent_runtime", result.structured_content["authored_effect_verification"])
                runtime.load_native.assert_not_called()
                deployment.install.assert_not_called()

    async def test_mismatched_package_or_installation_never_loads_native_or_blueprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            native, projects, deployment = Mock(), Mock(), Mock()
            projects.verify.return_value = self.package()
            deployment.status.return_value = {"identified_installation": False, "installed_files": True}
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService") as runtime_factory, \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(Path(temporary))
                args = {"target_process": {"pid": 100, "creation_time": "1234567"}, "package_path": "packages/Neutral"}
                changed = await tool_call(server, "sn2_mod_load", {**args, "expected_manifest_sha256": "c" * 64})
                self.assertEqual(failure(changed)["code"], "package_changed")
                runtime_factory.assert_not_called()
                absent = await tool_call(server, "sn2_mod_load", args)
                self.assertEqual(failure(absent)["code"], "package_installation_unconfirmed")
                native.request.assert_not_called()
                deployment.install.assert_not_called()

    async def test_install_uninstall_forward_exact_inventory_and_preserve_failure_receipts(self):
        with tempfile.TemporaryDirectory() as temporary:
            deployment = Mock()
            deployment.install.return_value = {"state": "installed", "plugin_mount": "not_performed"}
            deployment.uninstall.side_effect = DevelopmentError("installation_changed", "Installed inventory changed",
                outcome_unknown=True, receipt={"stage": "remove", "outcome_unknown": True})
            with patch("sn2_mcp.server.NativeClient") as native_factory, \
                 patch("sn2_mcp.server.RuntimeService"), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(Path(temporary))
                result = await tool_call(server, "sn2_mod_install", {"package_path": "packages/Neutral",
                    "expected_manifest_sha256": "a" * 64, "expected_installed_inventory_sha256": "b" * 64})
                self.assertFalse(result.is_error)
                deployment.install.assert_called_once_with("packages/Neutral", "a" * 64,
                    expected_installed_inventory_sha256="b" * 64)
                removed = await tool_call(server, "sn2_mod_uninstall", {"plugin_id": "Neutral",
                    "expected_installed_inventory_sha256": "b" * 64})
                error = failure(removed)
                self.assertTrue(error["outcome_unknown"])
                self.assertEqual(error["receipt"]["stage"], "remove")
                self.assertEqual(deployment.uninstall.call_count, 1)
                native_factory.return_value.request.assert_not_called()

    async def test_reload_failed_install_preserves_stopped_process_receipt_without_starting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "bridge.dll"
            bridge.write_bytes(b"neutral bridge fixture")
            digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
            target = {"pid": 100, "creation_time": "1234567"}
            lifecycle, projects, deployment = Mock(), Mock(), Mock()
            lifecycle.stop.return_value = {"state": "stopped", "process": target, "exit_confirmed": True}
            projects.verify.return_value = self.package()
            deployment.status.return_value = {"installed_files": False}
            deployment.install.side_effect = DevelopmentError("stage_changed", "Staged inventory changed",
                receipt={"state": "failed", "installed": False})
            with patch("sn2_mcp.server.NativeClient"), patch("sn2_mcp.server.RuntimeService"), \
                 patch("sn2_mcp.server.RuntimeLifecycle", return_value=lifecycle), \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(root, runtime_root=root)
                result = await tool_call(server, "sn2_mod_reload", {"target_process": target,
                    "package_path": "packages/Neutral", "bridge_dll_path": str(bridge), "expected_bridge_sha256": digest})
                self.assertTrue(result.is_error)
                error = failure(result)
                self.assertEqual(error["code"], "stage_changed")
                self.assertEqual(error["iteration_receipt"]["stage"], "install")
                self.assertTrue(error["iteration_receipt"]["stop"]["exit_confirmed"])
                lifecycle.stop.assert_called_once_with(target)
                lifecycle.start.assert_not_called()
                lifecycle.restart.assert_not_called()

    async def test_reload_orders_stop_install_start_fresh_bridge_and_blueprint_activation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "bridge.dll"
            bridge.write_bytes(b"neutral bridge fixture")
            digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
            old = {"pid": 100, "creation_time": "1234567"}
            fresh = {"pid": 200, "creation_time": "1234568"}
            native, runtime, lifecycle, projects, deployment = Mock(), Mock(), Mock(), Mock(), Mock()
            order = []
            runtime.validate_target.side_effect = lambda target: dict(target)
            runtime.load_native.side_effect = lambda *args: order.append("bridge_load") or {"state": "loaded"}
            lifecycle.stop.side_effect = lambda target: order.append("stop") or {"exit_confirmed": True}
            lifecycle.start.side_effect = lambda **kwargs: order.append("start") or {"process": fresh}
            package = self.package()
            projects.verify.return_value = package
            installed = {"installed_files": True, "identified_installation": True,
                "installed_inventory_sha256": "c" * 64,
                "receipt": {"state": "installed", "package_manifest_sha256": "a" * 64}}
            deployment.status.side_effect = [installed, installed]
            deployment.install.side_effect = lambda *args, **kwargs: order.append("install") or {"state": "installed"}
            status = {"process_id": fresh["pid"], "process_creation_filetime": fresh["creation_time"],
                "session_id": "fresh-session", "ready": True}

            def native_boundary(operation, params, **kwargs):
                order.append(operation)
                self.assertEqual(params["expected_process_id"], fresh["pid"])
                self.assertEqual(params["expected_creation_filetime"], fresh["creation_time"])
                if operation in {"status", "enable_game"}:
                    return status
                self.assertEqual(operation, "blueprint_mod_enable")
                self.assertEqual(params["package_revision"], package["manifest_sha256"])
                return {"state": "spawn_finished", "authored_effect": "unverified"}

            native.request.side_effect = native_boundary
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService", return_value=runtime), \
                 patch("sn2_mcp.server.RuntimeLifecycle", return_value=lifecycle), \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(root, runtime_root=root)
                result = await tool_call(server, "sn2_mod_reload", {"target_process": old,
                    "package_path": "packages/Neutral", "bridge_dll_path": str(bridge), "expected_bridge_sha256": digest,
                    "expected_installed_inventory_sha256": "c" * 64})
                self.assertFalse(result.is_error, result.content)
                self.assertEqual(order, ["stop", "install", "start", "bridge_load", "status", "status",
                    "enable_game", "blueprint_mod_enable"])
                self.assertFalse(result.structured_content["hot_reload"])
                self.assertEqual(result.structured_content["process"], fresh)
                self.assertEqual(result.structured_content["previous_process"], old)
                deployment.install.assert_called_once_with("packages/Neutral", "a" * 64,
                    expected_installed_inventory_sha256="c" * 64)
                lifecycle.restart.assert_not_called()

    async def test_invalid_spawn_transform_is_rejected_before_restart_or_package_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            native, runtime, lifecycle, projects, deployment = Mock(), Mock(), Mock(), Mock(), Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                 patch("sn2_mcp.server.RuntimeService", return_value=runtime), \
                 patch("sn2_mcp.server.RuntimeLifecycle", return_value=lifecycle), \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(Path(temporary))
                for transform in ({"Rotation": {"X": 0}}, {"Rotation": {"X": True, "Y": 0, "Z": 0, "W": 1},
                        "Translation": {"X": 0, "Y": 0, "Z": 0}, "Scale3D": {"X": 1, "Y": 1, "Z": 1}}):
                    result = await tool_call(server, "sn2_mod_reload", {
                        "target_process": {"pid": 100, "creation_time": "1234567"},
                        "package_path": "packages/Neutral", "bridge_dll_path": "absent.dll",
                        "expected_bridge_sha256": "a" * 64, "spawn_transform": transform})
                    self.assertTrue(result.is_error)
                    self.assertEqual(failure(result)["code"], "invalid_argument")
                lifecycle.stop.assert_not_called()
                lifecycle.start.assert_not_called()
                deployment.install.assert_not_called()
                projects.verify.assert_not_called()
                native.request.assert_not_called()

    async def test_missing_or_unresolved_recorded_installation_is_rejected_before_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bridge = root / "bridge.dll"
            bridge.write_bytes(b"neutral bridge fixture")
            digest = hashlib.sha256(bridge.read_bytes()).hexdigest()
            lifecycle, projects, deployment = Mock(), Mock(), Mock()
            projects.verify.return_value = self.package()
            with patch("sn2_mcp.server.NativeClient"), patch("sn2_mcp.server.RuntimeService"), \
                 patch("sn2_mcp.server.RuntimeLifecycle", return_value=lifecycle), \
                 patch("sn2_mcp.server.ContentProjects", return_value=projects), \
                 patch("sn2_mcp.server.ContentDeployment", return_value=deployment):
                server = create_server(root, runtime_root=root)
                for state, code in (("installed", "installation_changed"), ("publish_requested", "deployment_unresolved")):
                    deployment.status.return_value = {"installed_files": False, "receipt": {"state": state, "outcome_unknown": True}}
                    result = await tool_call(server, "sn2_mod_reload", {
                        "target_process": {"pid": 100, "creation_time": "1234567"},
                        "package_path": "packages/Neutral", "bridge_dll_path": str(bridge), "expected_bridge_sha256": digest})
                    self.assertTrue(result.is_error)
                    self.assertEqual(failure(result)["code"], code)
                lifecycle.stop.assert_not_called()
                lifecycle.start.assert_not_called()
                deployment.install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
