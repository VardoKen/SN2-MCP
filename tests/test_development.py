"""Изолированный контур разработки: подмена только исполнителя процессов."""

import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.development import (CommandResult, DevelopmentError, DevelopmentService,
                                 execute_command, PROCESS_TIMEOUT_SECONDS)
from sn2_mcp.window_input import WindowInputError


class DevelopmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "tools").mkdir(parents=True)
        for name in ("game_process_state.ps1", "start_sn2_development.ps1",
                     "stop_sn2_development.ps1", "load_native.ps1"):
            (self.repository / "tools" / name).write_text("# isolated", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.modkit = self.root / "modkit"
        self.modkit.mkdir()
        self.game = self.root / "game"
        self.game.mkdir()
        self.package = self.workspace / "Nested" / "Probe"
        (self.package / "Script").mkdir(parents=True)
        (self.package / "Script" / "Probe.as").write_text("class UProbe {}", encoding="utf-8")
        (self.package / "GothicMod.json").write_text(json.dumps({"name": "Probe", "title": "Probe",
                                                                 "author": "Test", "modVersion": "1.0.0"}), encoding="utf-8")
        self.calls = []
        self.processes = []
        self.responses = {}
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
                                           modkit_root=self.modkit, game_root=self.game,
                                           executor=self.execute, powershell="fixed-powershell")
        self.identity = {"pid": 123, "creation_time": "2026-10-30T01:02:03.1234567Z",
                         "executable": str(self.service.inner_executable)}

    def execute(self, arguments, timeout):
        self.calls.append((arguments, timeout))
        script = Path(arguments[arguments.index("-File") + 1]).name
        if script == "game_process_state.ps1":
            return CommandResult(0, json.dumps({"processes": self.processes}))
        if script in self.responses:
            return self.responses[script]
        if script == "start_sn2_development.ps1":
            result = {"state": "launch_requested", "package_name": "Probe",
                      "mods": arguments[arguments.index("-ModsCsv") + 1].split(","),
                      "mods_directory": arguments[arguments.index("-ModsDirectory") + 1]}
        elif script == "stop_sn2_development.ps1":
            result = {"state": "stopped", "process": self.identity}
        else:
            result = {"state": "loaded", "process": self.identity,
                      "dll_sha256": arguments[arguments.index("-ExpectedDllSha256") + 1]}
        return CommandResult(0, json.dumps(result))

    def recipe(self):
        return self.service.recipe("Nested/Probe")

    def test_recipe_hashes_full_package_without_runtime_claim(self):
        (self.package / "Content").mkdir()
        (self.package / "Content" / "asset.bin").write_bytes(b"Asset")
        result = self.recipe()
        manifest = result["manifest"]
        self.assertEqual([entry["path"] for entry in manifest["files"]],
                         ["Content/asset.bin", "GothicMod.json", "Script/Probe.as"])
        self.assertIsNone(result["launch"]["mods_directory"])
        self.assertEqual(result["launch"]["staging_created_by"], "development_start")
        self.assertEqual(result["launch"]["edit_target"], "staged_copy_only")
        self.assertFalse((self.workspace / "artifacts").exists())
        self.assertEqual(result["launch"]["mods"], ["Probe"])
        self.assertFalse(result["compiled"])
        self.assertFalse(result["diagnostics_available"])
        self.assertFalse(result["process_state"]["running"])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], 30)
        changed = manifest["manifest_sha256"]
        (self.package / "Content" / "asset.bin").write_bytes(b"Changed")
        self.assertNotEqual(self.recipe()["manifest"]["manifest_sha256"], changed)

    def test_start_binds_recipe_hash_and_requests_only_fixed_command(self):
        recipe = self.recipe()
        source_bytes = {path.relative_to(self.package).as_posix(): path.read_bytes()
                        for path in self.package.rglob("*") if path.is_file()}
        result = self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"])
        self.assertEqual(result["state"], "launch_requested")
        self.assertFalse(result["compiled"])
        self.assertFalse(result["game_ready"])
        arguments = self.calls[-1][0]
        self.assertEqual(arguments[0], "fixed-powershell")
        self.assertEqual(arguments[arguments.index("-ModsCsv") + 1], "Probe")
        stage = result["development_staging"]
        self.assertEqual(arguments[arguments.index("-ModsDirectory") + 1], stage["mods_directory"])
        self.assertNotEqual(stage["mods_directory"], str(self.package.parent))
        self.assertNotIn("-Mods", arguments)
        active = result["active_staging_packages"][0]
        staged_package = self.workspace / active["relative_package"]
        self.assertEqual({path.relative_to(staged_package).as_posix(): path.read_bytes()
                          for path in staged_package.rglob("*") if path.is_file()}, source_bytes)
        self.assertEqual({path.relative_to(self.package).as_posix(): path.read_bytes()
                          for path in self.package.rglob("*") if path.is_file()}, source_bytes)
        self.assertEqual(self.service._manifest(active["relative_package"])["manifest_sha256"],
                         active["manifest_sha256_at_launch"])
        self.assertEqual(result["manifest_sha256"], recipe["manifest"]["manifest_sha256"])
        self.assertFalse(result["source_packages_modified"])
        self.assertEqual(arguments[-1], "-Json")
        (self.package / "Script" / "Probe.as").write_text("class UChanged {}", encoding="utf-8")
        before = len(self.calls)
        with self.assertRaises(DevelopmentError) as captured:
            self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"])
        self.assertEqual(captured.exception.code, "package_changed")
        self.assertEqual(len(self.calls), before)

    def test_ingame_tools_default_and_explicit_false_bind_the_same_manifest(self):
        default = self.service.recipe("Nested/Probe")
        disabled = self.service.recipe("Nested/Probe", enable_ingame_tools=False)
        enabled = self.service.recipe("Nested/Probe", enable_ingame_tools=True)
        self.assertEqual(default["manifest"]["launch_options"], {"enable_ingame_tools": False})
        self.assertEqual(disabled["manifest"]["launch_options"], {"enable_ingame_tools": False})
        self.assertEqual(enabled["manifest"]["launch_options"], {"enable_ingame_tools": True})
        self.assertEqual(default["manifest"]["manifest_sha256"], disabled["manifest"]["manifest_sha256"])
        self.assertNotEqual(default["manifest"]["manifest_sha256"], enabled["manifest"]["manifest_sha256"])
        self.assertEqual(default["manifest"]["files"], enabled["manifest"]["files"])
        self.assertFalse(enabled["compiled"])

    def test_ingame_tools_option_is_strict_bool_before_executor_dispatch(self):
        digest = self.service.recipe("Nested/Probe")["manifest"]["manifest_sha256"]
        invalid_values = (None, 0, 1, -1, 1.0, "false", "true", [], {})
        for value in invalid_values:
            with self.subTest(operation="recipe", value=value):
                before = len(self.calls)
                with self.assertRaises(DevelopmentError) as captured:
                    self.service.recipe("Nested/Probe", enable_ingame_tools=value)
                self.assertEqual(captured.exception.code, "invalid_arguments")
                self.assertEqual(len(self.calls), before)
            with self.subTest(operation="start", value=value):
                before = len(self.calls)
                with self.assertRaises(DevelopmentError) as captured:
                    self.service.start("Nested/Probe", digest, enable_ingame_tools=value)
                self.assertEqual(captured.exception.code, "invalid_arguments")
                self.assertEqual(len(self.calls), before)

    def test_ingame_tools_manifest_mismatch_refuses_before_any_executor_call(self):
        digests = {flag: self.service.recipe("Nested/Probe", enable_ingame_tools=flag)["manifest"]["manifest_sha256"]
                   for flag in (False, True)}
        for requested in (False, True):
            with self.subTest(enable_ingame_tools=requested):
                before = len(self.calls)
                with self.assertRaises(DevelopmentError) as captured:
                    self.service.start("Nested/Probe", digests[not requested], enable_ingame_tools=requested)
                self.assertEqual(captured.exception.code, "package_changed")
                self.assertEqual(len(self.calls), before)

    def test_ingame_tools_start_passes_exactly_one_switch_only_when_enabled(self):
        for requested in (False, True):
            with self.subTest(enable_ingame_tools=requested):
                recipe = self.service.recipe("Nested/Probe", enable_ingame_tools=requested)
                result = self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"],
                                            enable_ingame_tools=requested)
                self.assertEqual(result["state"], "launch_requested")
                arguments = self.calls[-1][0]
                self.assertEqual(arguments.count("-EnableIngameTools"), 1 if requested else 0)
                self.assertEqual(arguments[arguments.index("-ModsCsv") + 1], "Probe")
                self.assertFalse(result["compiled"])

    def test_restart_requires_current_exact_process_identity(self):
        self.processes = [self.identity]
        recipe = self.recipe()
        with self.assertRaises(DevelopmentError) as captured:
            self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"])
        self.assertEqual(captured.exception.code, "game_running")
        for identity in (None, {**self.identity, "pid": 124},
                         {**self.identity, "creation_time": "2026-10-30T01:02:03.0000000Z"}):
            with self.subTest(identity=identity), self.assertRaises(DevelopmentError):
                self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"],
                                   restart=True, expected_process=identity)
        result = self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"],
                                    restart=True, expected_process=self.identity)
        self.assertEqual(result["state"], "launch_requested")
        arguments = self.calls[-1][0]
        self.assertIn("-Restart", arguments)
        self.assertEqual(arguments[arguments.index("-ExpectedPid") + 1], "123")
        self.assertEqual(arguments[arguments.index("-ExpectedCreationTime") + 1], self.identity["creation_time"])

    def test_queue_key_uses_exact_process_and_window_without_replacing_observer(self):
        permanent_lease = Mock()
        self.service._process_lease = permanent_lease
        self.addCleanup(self.service.close)
        window = 0x100000123
        receipt = {"status": "input_queued", "process": self.identity, "selected_hwnd": window,
                   "window_thread_id": 456, "virtual_key": 0x1B,
                   "messages": [{"message": "WM_KEYDOWN", "queued": True},
                                {"message": "WM_KEYUP", "queued": True}],
                   "handling_confirmed": False, "outcome_unknown": False, "do_not_retry": False}
        with patch.object(self.service, "process_state", return_value={"processes": [self.identity]}), \
                patch("sn2_mcp.development.queue_key", return_value=receipt) as queued:
            result = self.service.queue_key(self.identity, 0x1B, expected_window=window)
        queued.assert_called_once_with(self.identity, 0x1B, expected_window=window)
        self.assertEqual(result, receipt)
        self.assertFalse(result["handling_confirmed"])
        self.assertIs(self.service._process_lease, permanent_lease)
        permanent_lease.close.assert_not_called()
        permanent_lease.poll.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_queue_key_rejects_changed_identity_and_closed_service_before_dispatch(self):
        with patch.object(self.service, "process_state", return_value={"processes": [self.identity]}) as observed, \
                patch("sn2_mcp.development.queue_key") as queued:
            for expected, code in (
                ({**self.identity, "pid": 124}, "process_changed"),
                ({**self.identity, "creation_time": "2026-10-30T01:02:03.0000000Z"}, "process_changed"),
                ({**self.identity, "executable": str(self.service.bootstrap_executable)}, "process_path_mismatch"),
            ):
                with self.subTest(expected=expected), self.assertRaises(DevelopmentError) as captured:
                    self.service.queue_key(expected, 0x1B)
                self.assertEqual(captured.exception.code, code)
                self.assertFalse(captured.exception.outcome_unknown)
            self.service.close()
            observed.reset_mock()
            with self.assertRaises(DevelopmentError) as captured:
                self.service.queue_key(self.identity, 0x1B)
            self.assertEqual(captured.exception.code, "service_closed")
            self.assertFalse(captured.exception.outcome_unknown)
            observed.assert_not_called()
            queued.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_queue_key_preserves_partial_receipt_and_uncertain_effect_without_retry(self):
        receipt = {"status": "input_queue_failed", "process": self.identity, "selected_hwnd": 321,
                   "window_thread_id": 456, "virtual_key": 0x1B,
                   "messages": [{"message": "WM_KEYDOWN", "message_id": 0x100, "wparam": 0x1B,
                                 "lparam": 0x10001, "queued": True},
                                {"message": "WM_KEYUP", "message_id": 0x101, "wparam": 0x1B,
                                 "lparam": 0xC0010001, "queued": False, "win32_error": 5}],
                   "handling_confirmed": False, "outcome_unknown": True, "do_not_retry": True}
        failure = WindowInputError("key_up_queue_failed", "Не удалось поставить отпускание в очередь", receipt)
        with patch.object(self.service, "process_state", return_value={"processes": [self.identity]}), \
                patch("sn2_mcp.development.queue_key", side_effect=failure) as queued:
            with self.assertRaises(DevelopmentError) as captured:
                self.service.queue_key(self.identity, 0x1B)
        queued.assert_called_once_with(self.identity, 0x1B, expected_window=None)
        self.assertEqual(captured.exception.code, failure.code)
        self.assertEqual(str(captured.exception), str(failure))
        self.assertTrue(captured.exception.outcome_unknown)
        self.assertEqual(captured.exception.receipt, receipt)
        self.assertTrue(captured.exception.receipt["do_not_retry"])
        self.assertIs(captured.exception.__cause__, failure)
        self.assertEqual(self.calls, [])

    def test_stop_and_load_require_identity_and_load_requires_dll_hash(self):
        self.processes = [self.identity]
        result = self.service.stop(self.identity)
        self.assertEqual(result["process"], self.identity)
        dll = self.repository / "build" / "native" / "Release" / "SN2_MCP.dll"
        dll.parent.mkdir(parents=True)
        dll.write_bytes(b"isolated dll")
        digest = hashlib.sha256(dll.read_bytes()).hexdigest()
        with self.assertRaises(DevelopmentError) as captured:
            self.service.load_native(self.identity, "0" * 64)
        self.assertEqual(captured.exception.code, "dll_changed")
        result = self.service.load_native(self.identity, digest)
        self.assertFalse(result["game_ready"])
        self.assertEqual(result["dll_sha256"], digest)
        arguments = self.calls[-1][0]
        self.assertEqual(arguments[arguments.index("-ExpectedDllSha256") + 1], digest)
        self.processes = [{**self.identity, "pid": 999}]
        with self.assertRaises(DevelopmentError) as captured:
            self.service.stop(self.identity)
        self.assertEqual(captured.exception.code, "process_changed")

    def test_bootstrap_stop_accepted_but_load_and_restart_rejected(self):
        bootstrap = {**self.identity, "executable": str(self.service.bootstrap_executable)}
        self.processes = [bootstrap]
        self.responses["stop_sn2_development.ps1"] = CommandResult(0, json.dumps({
            "state": "stopped", "process": bootstrap, "cleanup": [], "outcome_unknown": False}))
        result = self.service.stop(bootstrap)
        self.assertEqual(result["process"], bootstrap)
        self.assertEqual(result["state"], "stop_result")
        self.assertEqual(self.calls[-1][0][self.calls[-1][0].index("-ExpectedPid") + 1], str(bootstrap["pid"]))
        with self.assertRaises(DevelopmentError) as captured:
            self.service.load_native(bootstrap, "0" * 64)
        self.assertEqual(captured.exception.code, "process_path_mismatch")
        self.assertFalse(any("load_native.ps1" in argument for call, _ in self.calls for argument in call))
        recipe = self.recipe()
        with self.assertRaises(DevelopmentError) as captured:
            self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"],
                               restart=True, expected_process=bootstrap)
        self.assertEqual(captured.exception.code, "process_path_mismatch")

    def test_bootstrap_stop_rejects_changed_identity_before_dispatch(self):
        bootstrap = {**self.identity, "executable": str(self.service.bootstrap_executable)}
        self.processes = [bootstrap]
        for wrong in ({**bootstrap, "pid": 999},
                      {**bootstrap, "creation_time": "2026-10-30T01:02:03.0000000Z"},
                      {**bootstrap, "executable": str(self.game / "unrelated.exe")}):
            with self.subTest(expected=wrong), self.assertRaises(DevelopmentError):
                self.service.stop(wrong)
        self.assertFalse(any("stop_sn2_development.ps1" in argument for call, _ in self.calls for argument in call))

    def test_protocol_failure_after_mutation_does_not_repeat_and_marks_unknown(self):
        self.responses["start_sn2_development.ps1"] = CommandResult(0, "not-json")
        recipe = self.recipe()
        with self.assertRaises(DevelopmentError) as captured:
            self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"])
        self.assertTrue(captured.exception.outcome_unknown)
        starts = [args for args, _ in self.calls if any("start_sn2_development.ps1" in argument for argument in args)]
        self.assertEqual(len(starts), 1)
        self.responses["start_sn2_development.ps1"] = CommandResult(0, "{}")
        with self.assertRaises(DevelopmentError) as captured:
            self.service.start("Nested/Probe", recipe["manifest"]["manifest_sha256"])
        self.assertTrue(captured.exception.outcome_unknown)

    def test_large_source_manifest_and_staging_preserve_exact_bytes(self):
        source = b"UCLASS()\nclass UProbe : UObject\n{\n}\n/*" + b"x" * (16 * 1024 * 1024) + b"*/\n"
        (self.package / "Script" / "Probe.as").write_bytes(source)

        manifest = self.service._composition_manifest("Nested/Probe", [])
        entry = next(item for item in manifest["files"] if item["path"] == "Script/Probe.as")
        self.assertEqual(entry["bytes"], len(source))
        self.assertEqual(entry["sha256"], hashlib.sha256(source).hexdigest())

        companion = self.workspace / "Companion"
        (companion / "Script").mkdir(parents=True)
        (companion / "Script" / "Companion.as").write_text("class UCompanion {}", encoding="utf-8")
        (companion / "GothicMod.json").write_text(json.dumps({"name": "Companion", "title": "Companion",
                                                           "author": "Test", "modVersion": "0.0.0"}),
                                                encoding="utf-8")
        composition = self.service._composition_manifest("Nested/Probe", ["Companion"])
        stage = self.service._stage_composition(composition)
        staged = self.workspace / stage["packages"][0]["relative_package"] / "Script" / "Probe.as"
        self.assertEqual(staged.read_bytes(), source)
        self.assertEqual(stage["packages"][0]["manifest"]["files"], manifest["files"])
        self.assertEqual(self.calls, [])

    def test_manifest_accepts_directories_and_rejects_redirected_package(self):
        self.assertEqual(len(self.recipe()["manifest"]["files"]), 2)
        external = self.root / "external"
        external.mkdir()
        try:
            (self.package / "foreign").symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("Система не разрешает isolated symlink")
        with self.assertRaises(DevelopmentError) as captured:
            self.recipe()
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_manifest_refuses_unreadable_tree_instead_of_partial_hash(self):
        def failing_walk(path, *, followlinks, onerror):
            onerror(PermissionError("isolated denied directory"))
            return iter(())

        with patch("sn2_mcp.development.os.walk", side_effect=failing_walk):
            with self.assertRaises(DevelopmentError) as captured:
                self.recipe()
        self.assertEqual(captured.exception.code, "access_error")

    def test_manifest_ties_validation_to_exact_hashed_descriptor(self):
        from sn2_mcp.packages import validate_package
        original = validate_package
        calls = 0

        def change_after_validation(*arguments):
            nonlocal calls
            calls += 1
            result = original(*arguments)
            if calls == 1:
                (self.package / "GothicMod.json").write_text("invalid-json", encoding="utf-8")
            return result

        with patch("sn2_mcp.development.validate_package", side_effect=change_after_validation):
            with self.assertRaises(DevelopmentError) as captured:
                self.recipe()
        self.assertIn(captured.exception.code, ("package_changed", "invalid_json"))

    def test_manifest_preserves_complete_serialized_response(self):
        manifest = self.recipe()["manifest"]
        self.assertEqual([entry["path"] for entry in manifest["files"]],
                         ["GothicMod.json", "Script/Probe.as"])
        self.assertEqual(manifest["total_bytes"], sum(entry["bytes"] for entry in manifest["files"]))

    def test_process_state_rejects_unrelated_paths_and_duplicate_json(self):
        self.processes = [{**self.identity, "executable": str(self.root / "other.exe")}]
        with self.assertRaises(DevelopmentError) as captured:
            self.service.process_state()
        self.assertEqual(captured.exception.code, "process_path_mismatch")
        self.processes = [self.identity, self.identity]
        with self.assertRaises(DevelopmentError):
            self.service.process_state()

    def test_real_executor_has_one_deadline_and_no_shell(self):
        from unittest.mock import Mock
        process = Mock(stdout=io.BytesIO(b'{"ok":true}'), stderr=io.BytesIO())
        process.wait.return_value = 0
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as run:
            result = execute_command(["fixed", "argument"], PROCESS_TIMEOUT_SECONDS)
        self.assertEqual(result.stdout, '{"ok":true}')
        process.wait.assert_called_once_with(timeout=30)
        self.assertNotIn("shell", run.call_args.kwargs)
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = [subprocess.TimeoutExpired(["fixed"], 30), 0]
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process):
            with self.assertRaises(DevelopmentError) as captured:
                execute_command(["fixed"], 30)
        self.assertTrue(captured.exception.outcome_unknown)
        process.kill.assert_called_once()

    def test_executor_preserves_complete_pipe_output(self):
        from unittest.mock import Mock
        output = b"x" * (256 * 1024 + 1)
        process = Mock(stdout=io.BytesIO(output), stderr=io.BytesIO())
        process.wait.return_value = 0
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process):
            result = execute_command(["fixed"], 30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, output.decode("utf-8"))
        self.assertEqual(result.stderr, "")
        process.kill.assert_not_called()

    def test_executor_keyboard_interrupt_settles_helper_without_replay(self):
        from unittest.mock import Mock
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as created:
            with self.assertRaises(KeyboardInterrupt) as captured:
                execute_command(["fixed"], 30)
        created.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(captured.exception.__notes__)


if __name__ == "__main__":
    unittest.main()
