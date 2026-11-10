"""Изолированная проверка HANDLE-lifecycle; игровые процессы не используются."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


REPO = Path(__file__).resolve().parents[1]
HELPER = REPO / "tools" / "game_process_common.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Нужны Windows и PowerShell")
class GameProcessCommonTests(unittest.TestCase):
    def _powershell(self, body: str, timeout: float = 35) -> dict:
        command = ". " + _quote(str(HELPER)) + "; " + body
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout.strip())

    @contextmanager
    def _sleeper(self, signal: Path | None = None):
        if signal is None:
            code = "import time; time.sleep(90)"
        else:
            code = (
                "from pathlib import Path; import sys,time; p=Path(sys.argv[1]); "
                "\nwhile not p.exists(): time.sleep(.02)"
            )
        args = [sys.executable, "-B", "-c", code]
        if signal is not None:
            args.append(str(signal))
        child = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            identity = self._powershell(
                f"Get-G1RProcessIdentity -ProcessId {child.pid} | ConvertTo-Json -Compress"
            )
            self.assertEqual(identity["pid"], child.pid)
            yield child, identity
        finally:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=5)

    @staticmethod
    def _open(identity: dict, *, terminate: bool = True) -> str:
        return (
            "$lease = Open-G1RCheckedProcess -ExpectedPid " + str(identity["pid"])
            + " -ExpectedCreationTime " + _quote(identity["creation_time"])
            + " -ExpectedExecutable " + _quote(identity["executable"])
            + (" -ForTermination" if terminate else "") + "; "
        )

    @staticmethod
    def _catch_code() -> str:
        return (
            "$e=$_.Exception; while($null -ne $e.InnerException){$e=$e.InnerException}; "
            "@{code=$e.Code; outcome_unknown=$e.OutcomeUnknown} | ConvertTo-Json -Compress"
        )

    def test_identity_is_exact_and_dot_source_is_idempotent(self):
        result = self._powershell(
            ". " + _quote(str(HELPER)) + "; "
            "Get-G1RProcessIdentity -ProcessId $PID | ConvertTo-Json -Compress"
        )
        self.assertGreater(result["pid"], 0)
        self.assertRegex(result["creation_time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z$")
        self.assertTrue(Path(result["executable"]).is_absolute())

    def test_wrong_creation_or_path_rejected_before_termination(self):
        with self._sleeper() as (child, identity):
            for key, value in (
                ("creation_time", "2000-01-01T00:00:00.0000000Z"),
                ("executable", str(Path(identity["executable"]).with_name("wrong.exe"))),
            ):
                bad = dict(identity)
                bad[key] = value
                body = "try { " + self._open(bad) + "throw 'unexpected_open' } catch { " + self._catch_code() + " }"
                result = self._powershell(body)
                self.assertEqual(result["code"], "process_changed")
                self.assertFalse(result["outcome_unknown"])
                self.assertIsNone(child.poll())

    def test_force_terminates_only_original_handle_and_disposes(self):
        with self._sleeper() as (child, identity):
            body = self._open(identity) + (
                "try { $r=Stop-G1RCheckedProcess -Lease $lease -GraceMilliseconds 0 "
                "-ForceWaitMilliseconds 5000 -AllowForce; $exitTime=$lease.GetExitTime() } finally {$lease.Dispose()}; "
                "@{result=$r; exit_time=$exitTime; disposed=$lease.IsDisposed} | ConvertTo-Json -Depth 6 -Compress"
            )
            result = self._powershell(body)
            self.assertEqual(result["result"]["state"], "stopped")
            self.assertEqual(result["result"]["mode"], "forced")
            self.assertEqual(result["result"]["process"], identity)
            self.assertTrue(result["disposed"])
            self.assertFalse(result["result"]["outcome_unknown"])
            self.assertRegex(result["exit_time"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z$")
            self.assertGreaterEqual(result["exit_time"], identity["creation_time"])
            child.wait(timeout=5)

    def test_readonly_handle_rejects_force_and_disposal_rejects_wait(self):
        with self._sleeper() as (child, identity):
            body = self._open(identity, terminate=False) + (
                "try { Stop-G1RCheckedProcess -Lease $lease -GraceMilliseconds 0 -AllowForce } "
                "catch { " + self._catch_code() + " } finally {$lease.Dispose()}"
            )
            result = self._powershell(body)
            self.assertEqual(result["code"], "termination_not_enabled")
            self.assertIsNone(child.poll())
            body = self._open(identity, terminate=False) + (
                "try {$lease.GetExitTime()} catch { " + self._catch_code() + " } finally {$lease.Dispose()}"
            )
            result = self._powershell(body)
            self.assertEqual(result["code"], "process_still_running")
            self.assertIsNone(child.poll())
            body = self._open(identity, terminate=False) + (
                "$lease.Dispose(); try {$lease.Wait(0)} catch { " + self._catch_code() + " }"
            )
            result = self._powershell(body)
            self.assertEqual(result["code"], "process_handle_closed")
            self.assertIsNone(child.poll())

    def test_waits_for_natural_exit_without_force(self):
        with tempfile.TemporaryDirectory(prefix="g1r-stop-test-") as root:
            signal = Path(root) / "exit"
            with self._sleeper(signal) as (child, identity):
                timer = threading.Timer(5, lambda: signal.touch())
                timer.start()
                try:
                    body = self._open(identity) + (
                        "try {Stop-G1RCheckedProcess -Lease $lease -GraceMilliseconds 12000 "
                        "-ForceWaitMilliseconds 5000 -AllowForce | ConvertTo-Json -Depth 6 -Compress} "
                        "finally {$lease.Dispose()}"
                    )
                    result = self._powershell(body)
                finally:
                    timer.cancel()
                    timer.join(timeout=2)
                self.assertEqual(result["state"], "stopped")
                self.assertEqual(result["mode"], "graceful")
                self.assertEqual(result["process"], identity)
                child.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
