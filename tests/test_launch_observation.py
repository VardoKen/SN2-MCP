"""Наблюдение последнего собственного запуска; только mock helpers/read-only lease."""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.development import CommandResult, DevelopmentError, DevelopmentService


def timestamp(value):
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond:06d}0Z"


class FakeLease:
    def __init__(self, process):
        self.process = dict(process)
        self.state = "running"
        self.exit_code = 1
        self.close_count = 0
        self.poll_count = 0

    def poll(self):
        self.poll_count += 1
        if self.state == "exited":
            return {"state": self.state, "process": self.process, "exit_code": self.exit_code,
                    "exit_time": timestamp(datetime.now(timezone.utc))}
        return {"state": self.state, "process": self.process}

    def close(self):
        self.close_count += 1


class LaunchObservationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        (self.repository / "tools").mkdir()
        for tool in ("game_process_state.ps1", "start_sn2_development.ps1"):
            (self.repository / "tools" / tool).write_text("fixture", encoding="utf-8")
        self.workspace = self.root / "workspace"
        package = self.workspace / "Probe"
        (package / "Script").mkdir(parents=True)
        (package / "Script" / "Probe.as").write_text("class UProbe {}", encoding="utf-8")
        (package / "GothicMod.json").write_text(json.dumps({"name": "Probe", "title": "Probe",
            "author": "Fixture", "modVersion": "1.0.0"}), encoding="utf-8")
        for name in ("game", "modkit"):
            (self.root / name).mkdir()
        self.processes = []
        self.leases = []
        self.calls = []
        self.capture_error = None
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
            modkit_root=self.root / "modkit", game_root=self.root / "game", executor=self.execute,
            powershell="mock-powershell", process_lease_factory=self.capture)
        self.addCleanup(self.service.close)

    def execute(self, arguments, timeout):
        tool = Path(arguments[arguments.index("-File") + 1]).name
        self.calls.append(tool)
        if tool == "game_process_state.ps1":
            return CommandResult(0, json.dumps({"processes": self.processes}))
        return CommandResult(0, json.dumps({"state": "launch_requested", "package_name": "Probe",
                                            "mods": arguments[arguments.index("-ModsCsv") + 1].split(","),
                                            "mods_directory": arguments[arguments.index("-ModsDirectory") + 1],
                                            "SteamRequestPid": 999}))

    def capture(self, identity):
        if self.capture_error:
            raise self.capture_error
        lease = FakeLease(identity)
        self.leases.append(lease)
        return lease

    def start(self):
        manifest = self.service._manifest("Probe")
        return self.service.start("Probe", manifest["manifest_sha256"])

    def inner(self, pid=111, *, age=1):
        return {"pid": pid, "creation_time": timestamp(datetime.now(timezone.utc) + timedelta(seconds=age)),
                "executable": str(self.service.inner_executable)}

    def test_no_launch_does_not_capture_unrelated_game(self):
        self.processes = [self.inner()]
        state = self.service.process_state()
        self.assertTrue(state["running"])
        self.assertIsNone(state["launch_observation"])
        self.assertEqual(self.leases, [])

    def test_observed_exit_is_retained_without_claiming_compiler_failure(self):
        launched = self.start()
        record = launched["launch_observation"]
        self.assertEqual(record["manifest_sha256"], launched["manifest_sha256"])
        self.assertEqual(record["stage"], "launch_unobserved")
        self.assertFalse(record["compiled_confirmed"])
        self.assertTrue(record["compiler_exit_on_error"])
        self.processes = [self.inner()]
        state = self.service.process_state()
        self.assertEqual(state["launch_observation"]["stage"], "process_running")
        self.assertNotEqual(state["launch_observation"]["process"]["pid"], 999)
        state["launch_observation"]["process"]["pid"] = 7
        self.assertEqual(self.service.process_state()["launch_observation"]["process"]["pid"], 111)
        self.processes = []
        self.leases[0].state = "exited"
        ended = self.service.process_state()["launch_observation"]
        self.assertEqual(ended["stage"], "process_exited")
        self.assertEqual(ended["exit_code"], 1)
        self.assertTrue(ended["process_failed"])
        self.assertEqual(ended["termination_cause"], "unknown")
        self.assertFalse(ended["compiler_failure_confirmed"])
        self.assertFalse(ended["compiled_confirmed"])
        self.assertEqual(self.leases[0].close_count, 1)
        self.assertEqual(self.service.process_state()["launch_observation"], ended)

    def test_bootstrap_only_and_missed_process_are_unknown(self):
        launched = self.start()
        self.processes = [{"pid": 15, "creation_time": timestamp(datetime.now(timezone.utc)),
                           "executable": str(self.service.bootstrap_executable)}]
        bootstrap = self.service.process_state()["launch_observation"]
        self.assertEqual(bootstrap["stage"], "launch_unobserved")
        self.assertFalse(bootstrap["process_observed"])
        self.processes = []
        missed = self.service.process_state()["launch_observation"]
        self.assertEqual(missed["launch_id"], launched["launch_observation"]["launch_id"])
        self.assertIsNone(missed["exit_code"])
        self.assertTrue(missed["outcome_unknown"])
        self.assertEqual(self.leases, [])

    def test_capture_failure_is_not_retried_or_compiler_diagnosis(self):
        self.start()
        self.capture_error = OSError("mock denied")
        self.processes = [self.inner()]
        state = self.service.process_state()["launch_observation"]
        self.assertEqual(state["stage"], "capture_unavailable")
        self.assertTrue(state["outcome_unknown"])
        self.assertFalse(state["compiler_failure_confirmed"])
        self.capture_error = None
        self.processes = []
        self.assertEqual(self.service.process_state()["launch_observation"], state)
        self.assertEqual(self.leases, [])

    def test_process_that_predates_request_is_never_captured(self):
        self.start()
        self.processes = [self.inner(age=-20)]
        state = self.service.process_state()["launch_observation"]
        self.assertEqual(state["stage"], "capture_unavailable")
        self.assertEqual(state["observation_error_code"], "process_predates_launch")
        self.assertFalse(state["process_observed"])
        self.assertEqual(self.leases, [])

    def test_reused_pid_cannot_replace_retained_identity(self):
        self.start()
        first = self.inner()
        self.processes = [first]
        self.service.process_state()
        self.leases[0].state = "exited"
        self.leases[0].exit_code = 259  # STILL_ACTIVE code is valid after signaled wait.
        replacement = {**first, "creation_time": timestamp(datetime.now(timezone.utc) + timedelta(seconds=2))}
        self.processes = [replacement]
        state = self.service.process_state()
        self.assertEqual(state["processes"], [replacement])
        self.assertEqual(state["launch_observation"]["process"], first)
        self.assertEqual(state["launch_observation"]["exit_code"], 259)
        self.assertEqual(len(self.leases), 1)

    def test_next_launch_and_server_close_release_once(self):
        old = self.start()
        self.processes = [self.inner()]
        self.service.process_state()
        self.processes = []
        new = self.start()
        self.assertNotEqual(old["launch_observation"]["launch_id"], new["launch_observation"]["launch_id"])
        self.assertEqual(self.leases[0].close_count, 1)
        self.processes = [self.inner(pid=222)]
        self.service.process_state()
        self.service.close()
        self.service.close()
        self.assertEqual(self.leases[1].close_count, 1)
        self.assertTrue(self.service.process_state()["launch_observation"]["observer_closed"])
        with self.assertRaises(DevelopmentError) as captured:
            self.start()
        self.assertEqual(captured.exception.code, "service_closed")
        for mutation in (lambda: self.service.stop(self.inner()),
                         lambda: self.service.load_native(self.inner(), "a" * 64)):
            count = len(self.calls)
            with self.assertRaises(DevelopmentError) as captured:
                mutation()
            self.assertEqual(captured.exception.code, "service_closed")
            self.assertEqual(len(self.calls), count)

    def test_unknown_poll_keeps_cause_and_effect_unknown(self):
        self.start(); self.processes = [self.inner()]
        self.service.process_state()
        self.leases[0].state = "observation_unknown"
        result = self.service.process_state()["launch_observation"]
        self.assertEqual(result["stage"], "observation_unknown")
        self.assertTrue(result["outcome_unknown"])
        self.assertIsNone(result["exit_code"])
        self.assertFalse(result["compiler_failure_confirmed"])

    def test_close_waits_for_existing_poll_and_does_not_close_handle_mid_read(self):
        self.start(); self.processes = [self.inner()]
        self.service.process_state()
        lease = self.leases[0]
        original = lease.poll
        entered, continue_poll, close_done = threading.Event(), threading.Event(), threading.Event()
        results = []
        def poll():
            entered.set()
            continue_poll.wait(2)
            self.assertEqual(lease.close_count, 0)
            return original()
        lease.poll = poll
        reader = threading.Thread(target=lambda: results.append(self.service.process_state()))
        def close():
            self.service.close()
            close_done.set()
        closer = threading.Thread(target=close)
        reader.start()
        self.assertTrue(entered.wait(2))
        closer.start()
        self.assertFalse(close_done.wait(.05))
        continue_poll.set()
        reader.join(2); closer.join(2)
        self.assertFalse(reader.is_alive()); self.assertFalse(closer.is_alive())
        self.assertEqual(lease.close_count, 1)
        self.assertEqual(results[0]["launch_observation"]["stage"], "process_running")
        self.assertEqual(self.service.process_state()["launch_observation"]["stage"], "observation_closed")

    def test_invalid_poll_identity_or_exitcode_never_becomes_process_exit_fact(self):
        self.start(); self.processes = [self.inner()]
        self.service.process_state()
        lease = self.leases[0]
        for observed in ({"state": "exited", "process": {**lease.process, "pid": 987}, "exit_code": 1, "exit_time": "T"},
                         {"state": "exited", "process": lease.process, "exit_code": True, "exit_time": "T"}):
            lease.poll = lambda: observed
            result = self.service.process_state()["launch_observation"]
            self.assertEqual(result["stage"], "observation_unknown")
            self.assertIsNone(result["exit_code"])
            self.assertFalse(result["compiler_failure_confirmed"])
            self.assertEqual(lease.close_count, 0)

    def test_close_failure_cannot_happen_after_new_steam_launch_or_erase_exit_fact(self):
        self.start(); self.processes = [self.inner()]
        self.service.process_state()
        lease = self.leases[0]
        def failed_close():
            lease.close_count += 1
            raise OSError("mock close failure")
        lease.close = failed_close
        self.processes = []
        calls = self.calls.count("start_sn2_development.ps1")
        with self.assertRaises(DevelopmentError) as caught:
            self.start()
        self.assertEqual(caught.exception.code, "observer_close_failed")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(self.calls.count("start_sn2_development.ps1"), calls)
        self.assertEqual(lease.close_count, 1)
        # Новый независимый HANDLE: error освобождения не меняет доказанный exit.
        self.start(); self.processes = [self.inner(pid=222)]
        self.service.process_state()
        last = self.leases[1]
        last.state = "exited"
        last.close = failed_close
        self.processes = []
        result = self.service.process_state()["launch_observation"]
        self.assertEqual(result["stage"], "process_exited")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["observer_close_error"], "process_handle_close_failed")

    def test_failed_new_launch_does_not_leave_old_running_observation_after_handle_closed(self):
        old = self.start(); self.processes = [self.inner()]
        self.service.process_state()
        self.processes = []
        original = self.service.executor
        def failure(arguments, timeout):
            if Path(arguments[arguments.index("-File") + 1]).name == "start_sn2_development.ps1":
                return CommandResult(2, "mock failure")
            return original(arguments, timeout)
        self.service.executor = failure
        with self.assertRaises(DevelopmentError): self.start()
        result = self.service.process_state()["launch_observation"]
        self.assertEqual(result["launch_id"], old["launch_observation"]["launch_id"])
        self.assertEqual(result["stage"], "observation_closed")
        self.assertTrue(result["outcome_unknown"])
        self.assertEqual(self.leases[0].close_count, 1)


if __name__ == "__main__":
    unittest.main()
