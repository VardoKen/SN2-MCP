"""Neutral executor checks for exact game process lifecycle composition."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sn2_mcp.development import CommandResult, DevelopmentError, RuntimeService, execute_command
from sn2_mcp.runtime_lifecycle import RuntimeLifecycle


class RuntimeLifecycleTests(unittest.TestCase):
    def fixture(self, root, replies):
        repository = root / "repository"
        tools = repository / "tools"
        tools.mkdir(parents=True)
        for name in ("game_process_state.ps1", "start_sn2_development.ps1", "stop_sn2_development.ps1"):
            (tools / name).write_text("# Neutral tool fixture\n", encoding="utf-8")
        game = root / "game"
        executable = game / "Subnautica2" / "Binaries" / "Win64" / "Subnautica2-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"neutral executable")
        bootstrap = game / "Subnautica2.exe"
        bootstrap.write_bytes(b"neutral bootstrap")
        target = {"pid": 123, "creation_time": "2026-11-10T10:11:12.1234567Z", "executable": str(executable)}
        calls = []

        def execute(arguments, timeout):
            calls.append((arguments, timeout))
            response = replies.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response if isinstance(response, CommandResult) else CommandResult(0, json.dumps(response))

        service = RuntimeService(repository_root=repository, game_root=game, executor=execute)
        return RuntimeLifecycle(service), target, calls

    def launch_receipt(self, lifecycle):
        return {"state": "launch_requested", "executable": str(lifecycle.service.bootstrap_executable),
                "arguments": [], "bootstrap_pid": 122, "outcome_unknown": False,
                "note": "A launch request does not prove an executor."}

    def stop_receipt(self, target, *, cleanup=None):
        if cleanup is None:
            cleanup = [{"state": "stopped", "mode": "graceful", "grace_requests": 1,
                        "process": target, "outcome_unknown": False}]
        return {"state": "stopped", "mode": cleanup[0]["mode"] if cleanup else "none",
                "process": target, "cleanup": cleanup, "outcome_unknown": False}

    def assert_code(self, code, callback):
        with self.assertRaises(DevelopmentError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_launch_waits_for_internal_process_and_preserves_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            launch = self.launch_receipt(lifecycle)
            bootstrap = {**target, "pid": 122, "executable": str(lifecycle.service.bootstrap_executable)}
            replies.extend([{"processes": []}, launch, {"processes": [bootstrap]}, {"processes": [target]}])
            with patch("sn2_mcp.runtime_lifecycle.time.sleep") as sleep:
                result = lifecycle.start(discovery_timeout_seconds=90)
            self.assertEqual(result["state"], "started")
            self.assertEqual(result["process"], RuntimeService._runtime_identity(target))
            self.assertEqual(result["launch_receipt"], launch)
            self.assertFalse(result["native_readiness_checked"])
            self.assertFalse(result["outcome_unknown"])
            sleep.assert_called_once()
            self.assertEqual(len(calls), 4)
            launch_args, launch_timeout = calls[1]
            self.assertIn("start_sn2_development.ps1", launch_args[6])
            self.assertNotIn("-Restart", launch_args)
            self.assertNotIn("-ModsCSV", launch_args)
            self.assertEqual(launch_timeout, 30)

    def test_no_launch_when_game_is_running_or_snapshot_is_ambiguous(self):
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, target, calls = self.fixture(Path(root), replies)
                processes = [target]
                if ambiguous:
                    processes.append({**target, "pid": 124})
                replies.append({"processes": processes})
                error = self.assert_code("ambiguous_game_process" if ambiguous else "game_already_running", lifecycle.start)
                self.assertFalse(error.outcome_unknown)
                self.assertEqual(len(calls), 1)

    def test_discovery_deadline_preserves_launch_and_last_snapshot_without_retry(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, _, calls = self.fixture(Path(root), replies)
            launch = self.launch_receipt(lifecycle)
            replies.extend([{"processes": []}, launch, {"processes": []}])
            with patch("sn2_mcp.runtime_lifecycle.time.monotonic", side_effect=[10.0, 11.0]):
                error = self.assert_code("game_process_not_observed", lambda: lifecycle.start(discovery_timeout_seconds=0.5))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt["launch_receipt"], launch)
            self.assertEqual(error.receipt["process_snapshot"]["processes"], [])
            self.assertEqual(len(calls), 3)

    def test_launch_failure_and_malformed_launch_are_not_replayed(self):
        for kind in ("timeout", "wrong_path", "invalid_pid", "unknown", "non_json", "failed_without_receipt"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, _, calls = self.fixture(Path(root), replies)
                launch = self.launch_receipt(lifecycle)
                if kind == "timeout":
                    result = DevelopmentError("operation_timeout", "Neutral timeout", outcome_unknown=True)
                    code = "operation_timeout"
                elif kind == "non_json":
                    result, code = CommandResult(0, "not JSON"), "tool_protocol_error"
                elif kind == "failed_without_receipt":
                    result, code = CommandResult(2, "", "Neutral failure"), "tool_failed"
                else:
                    if kind == "wrong_path":
                        launch["executable"] = "other.exe"
                    elif kind == "invalid_pid":
                        launch["bootstrap_pid"] = True
                    else:
                        launch["outcome_unknown"] = True
                    result, code = launch, "tool_protocol_error"
                replies.extend([{"processes": []}, result])
                error = self.assert_code(code, lifecycle.start)
                self.assertTrue(error.outcome_unknown)
                self.assertEqual(len(calls), 2)

    def test_failed_discovery_keeps_known_launch(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, _, calls = self.fixture(Path(root), replies)
            launch = self.launch_receipt(lifecycle)
            replies.extend([{"processes": []}, launch, CommandResult(2, "", "Neutral observation failure")])
            error = self.assert_code("tool_failed", lifecycle.start)
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt["launch_receipt"], launch)
            self.assertEqual(len(calls), 3)

    def test_scripts_not_started_receipts_preserve_known_no_effect(self):
        for operation in ("start", "stop"):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, target, calls = self.fixture(Path(root), replies)
                if operation == "start":
                    receipt = {"state": "not_started", "executable": str(lifecycle.service.bootstrap_executable),
                               "arguments": [], "restart_requested": False, "outcome_unknown": False}
                    replies.extend([{"processes": []}, receipt])
                    callback = lifecycle.start
                else:
                    receipt = {"state": "not_started", "process": target, "cleanup": [], "outcome_unknown": False}
                    replies.extend([{"processes": [target]}, receipt])
                    callback = lambda: lifecycle.stop(target)
                error = self.assert_code("game_launch_not_started" if operation == "start" else "game_stop_not_started", callback)
                self.assertFalse(error.outcome_unknown)
                self.assertEqual(error.receipt, receipt)
                self.assertEqual(len(calls), 2)

    def test_stop_checks_exact_identity_and_keeps_mixed_cleanup_modes(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            bootstrap = {**target, "pid": 122, "executable": str(lifecycle.service.bootstrap_executable)}
            cleanup = [{"state": "stopped", "mode": "graceful", "grace_requests": 1,
                        "process": bootstrap, "outcome_unknown": False},
                       {"state": "stopped", "mode": "forced", "grace_requests": 2,
                        "process": target, "outcome_unknown": False}]
            stop = self.stop_receipt(target, cleanup=cleanup)
            replies.extend([{"processes": [bootstrap, target]}, stop, {"processes": []}])
            result = lifecycle.stop(target)
            self.assertEqual(result["stop_receipt"], stop)
            self.assertEqual(result["remaining_processes"], [])
            self.assertFalse(result["outcome_unknown"])
            arguments = calls[1][0]
            self.assertEqual(arguments[arguments.index("-ExpectedPid") + 1], "123")
            self.assertEqual(arguments[arguments.index("-ExpectedCreationTime") + 1], "134387790721234567")
            self.assertFalse(any(argument.startswith("-Confirm") for argument in arguments))

    @unittest.skipUnless(os.name == "nt", "Requires the Windows PowerShell process boundary")
    def test_windows_stop_arguments_bind_shouldprocess_without_confirmation_switch(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            neutral_script = lifecycle.service.repository / "tools" / "stop_sn2_development.ps1"
            neutral_script.write_text(
                "[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]\n"
                "param([string]$GameRoot, [uint32]$ExpectedPid, [string]$ExpectedCreationTime, [switch]$Json)\n"
                "$ErrorActionPreference = 'Stop'\n"
                "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)\n"
                "$allowed = $PSCmdlet.ShouldProcess('Neutral fixture', 'Record parameter binding only')\n"
                "[pscustomobject]@{ allowed = $allowed; pid = $ExpectedPid; creation = $ExpectedCreationTime; "
                "confirm_preference = [string]$ConfirmPreference } | ConvertTo-Json -Compress\n",
                encoding="utf-8")
            replies.extend([{"processes": [target]}, self.stop_receipt(target), {"processes": []}])
            lifecycle.stop(target)
            # Reuse the actual generated arguments with a harmless substitute
            # script. It has no file, process, game or configuration operation.
            result = execute_command(calls[1][0], calls[1][1])
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertEqual(receipt["pid"], 123)
            self.assertEqual(receipt["creation"], "134387790721234567")
            self.assertEqual(receipt["confirm_preference"], "High")
            self.assertTrue(receipt["allowed"])

    def test_stop_accepts_already_exited_checked_handle(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, _ = self.fixture(Path(root), replies)
            stop = self.stop_receipt(target, cleanup=[{"state": "already_exited", "mode": "none", "grace_requests": 0,
                                                      "process": target, "outcome_unknown": False}])
            replies.extend([{"processes": [target]}, stop, {"processes": []}])
            self.assertEqual(lifecycle.stop(target)["state"], "stopped")

    def test_stale_identity_cannot_stop_or_restart(self):
        for restart in (False, True):
            with self.subTest(restart=restart), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, target, calls = self.fixture(Path(root), replies)
                replies.append({"processes": [{**target, "creation_time": "2026-11-10T10:11:12.1234568Z"}]})
                callback = lifecycle.restart if restart else lifecycle.stop
                error = self.assert_code("process_changed", lambda: callback(target))
                self.assertFalse(error.outcome_unknown)
                self.assertEqual(len(calls), 1)

    def test_invalid_stop_receipts_do_not_confirm_termination(self):
        for kind in ("different_target", "missing_target", "duplicate_cleanup", "unknown_cleanup", "invalid_mode"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, target, calls = self.fixture(Path(root), replies)
                stop = self.stop_receipt(target)
                if kind == "different_target":
                    stop["process"] = {**target, "pid": 124}
                elif kind == "missing_target":
                    stop["cleanup"] = []
                elif kind == "duplicate_cleanup":
                    stop["cleanup"].append(copy.deepcopy(stop["cleanup"][0]))
                elif kind == "unknown_cleanup":
                    stop["cleanup"][0]["outcome_unknown"] = True
                else:
                    stop["cleanup"][0]["mode"] = []
                replies.extend([{"processes": [target]}, stop])
                error = self.assert_code("tool_protocol_error", lambda: lifecycle.stop(target))
                self.assertTrue(error.outcome_unknown)
                self.assertEqual(error.receipt, stop)
                self.assertEqual(len(calls), 2)

    def test_stop_failure_schema_keeps_known_and_unknown_effects(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as root:
                replies = []
                lifecycle, target, calls = self.fixture(Path(root), replies)
                failure = {"state": "failed", "code": "session_cleanup_incomplete", "message": "Neutral failed session",
                           "process": target if unknown else None, "cleanup": [], "outcome_unknown": unknown}
                replies.extend([{"processes": [target]}, CommandResult(2, "\ufeff" + json.dumps(failure))])
                error = self.assert_code("session_cleanup_incomplete", lambda: lifecycle.stop(target))
                self.assertEqual(error.outcome_unknown, unknown)
                self.assertEqual(error.receipt, failure)
                self.assertEqual(len(calls), 2)

    def test_malformed_stop_failure_remains_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, _ = self.fixture(Path(root), replies)
            failure = {"state": "failed", "code": "session_cleanup_incomplete", "message": "Neutral failed session",
                       "process": {**target, "pid": 124}, "cleanup": [], "outcome_unknown": False}
            replies.extend([{"processes": [target]}, CommandResult(2, json.dumps(failure))])
            error = self.assert_code("tool_failed", lambda: lifecycle.stop(target))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt, failure)

    def test_remaining_process_cancels_restart_without_relaunch(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            stop = self.stop_receipt(target)
            remaining = {**target, "pid": 124}
            replies.extend([{"processes": [target]}, stop, {"processes": [remaining]}])
            error = self.assert_code("session_cleanup_incomplete", lambda: lifecycle.restart(target))
            self.assertFalse(error.outcome_unknown)
            self.assertEqual(error.receipt["stop_receipt"], stop)
            self.assertEqual(len(calls), 3)

    def test_stop_observation_failure_preserves_confirmed_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            stop = self.stop_receipt(target)
            replies.extend([{"processes": [target]}, stop, CommandResult(2, "", "Neutral observer failure")])
            error = self.assert_code("tool_failed", lambda: lifecycle.stop(target))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt["stop_receipt"], stop)
            self.assertEqual(len(calls), 3)

    def test_restart_uses_fresh_identity_without_inherited_restart_flags(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            fresh = {**target, "creation_time": "2026-11-10T10:11:13.1234567Z"}
            stop, launch = self.stop_receipt(target), self.launch_receipt(lifecycle)
            replies.extend([{"processes": [target]}, stop, {"processes": []}, {"processes": []}, launch,
                            {"processes": [fresh]}])
            result = lifecycle.restart(target, discovery_timeout_seconds=120)
            self.assertEqual(result["state"], "restarted")
            self.assertEqual(result["process"], RuntimeService._runtime_identity(fresh))
            self.assertEqual(result["previous_process"], RuntimeService._runtime_identity(target))
            self.assertEqual(result["stop_receipt"], stop)
            self.assertEqual(result["launch_receipt"], launch)
            self.assertNotIn("-Restart", calls[4][0])
            self.assertEqual(len(calls), 6)

    def test_restart_launch_failure_retains_stopped_session_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, calls = self.fixture(Path(root), replies)
            stop = self.stop_receipt(target)
            replies.extend([{"processes": [target]}, stop, {"processes": []}, {"processes": []},
                            DevelopmentError("operation_timeout", "Neutral timeout", outcome_unknown=True)])
            error = self.assert_code("operation_timeout", lambda: lifecycle.restart(target))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt["stop_receipt"], stop)
            self.assertEqual(error.receipt["previous_process"], RuntimeService._runtime_identity(target))
            self.assertEqual(len(calls), 5)

    def test_old_process_identity_is_not_a_successful_restart(self):
        with tempfile.TemporaryDirectory() as root:
            replies = []
            lifecycle, target, _ = self.fixture(Path(root), replies)
            stop, launch = self.stop_receipt(target), self.launch_receipt(lifecycle)
            replies.extend([{"processes": [target]}, stop, {"processes": []}, {"processes": []}, launch,
                            {"processes": [target]}])
            error = self.assert_code("process_changed", lambda: lifecycle.restart(target))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt["stop_receipt"], stop)

    def test_invalid_timeout_and_closed_service_do_not_run_tools(self):
        with tempfile.TemporaryDirectory() as root:
            lifecycle, target, calls = self.fixture(Path(root), [])
            for timeout in (0, -1, True, float("inf"), float("nan"), "30", 10 ** 1000):
                self.assert_code("invalid_timeout", lambda: lifecycle.start(discovery_timeout_seconds=timeout))
                self.assert_code("invalid_timeout", lambda: lifecycle.restart(target, discovery_timeout_seconds=timeout))
            lifecycle.service.close()
            self.assert_code("service_closed", lifecycle.start)
            self.assert_code("service_closed", lambda: lifecycle.stop(target))
            self.assert_code("service_closed", lambda: lifecycle.restart(target))
            self.assertEqual(calls, [])

    def test_invalid_or_foreign_identity_does_not_run_tools(self):
        with tempfile.TemporaryDirectory() as root:
            lifecycle, target, calls = self.fixture(Path(root), [])
            for creation in ("0", "-1", "2026-11-10T10:11:12Z", str(2 ** 64), "1500-11-10T10:11:12.1234567Z"):
                self.assert_code("invalid_process_identity", lambda: lifecycle.stop({**target, "creation_time": creation}))
            self.assert_code("invalid_process_identity", lambda: lifecycle.stop({**target, "pid": True}))
            self.assert_code("process_path_mismatch", lambda: lifecycle.stop({**target, "executable": "other.exe"}))
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
