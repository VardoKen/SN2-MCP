"""Проверки stop-обёртки на фиктивных процессах; игру не читают и не запускают."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[1]
STOP_SCRIPT = REPOSITORY / "tools" / "stop_sn2_development.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
PARENT_CREATED = "2026-10-31T00:00:00.0000000Z"
CHILD_CREATED = "2026-10-31T00:00:01.0000000Z"
PARENT_EXITED = "2026-10-31T00:00:03.0000000Z"


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


HARNESS = r"""
param([string]$ScenarioPath, [string]$StopPath, [string]$GameRoot)
$ErrorActionPreference = 'Stop'
$global:scenario = Get-Content -LiteralPath $ScenarioPath -Raw -Encoding UTF8 | ConvertFrom-Json
$global:actions = New-Object System.Collections.ArrayList
$global:disposed = New-Object System.Collections.ArrayList
$global:opened = New-Object System.Collections.ArrayList
$global:parentStopped = $false
$global:records = @{}
foreach ($item in $scenario.processes) {
    $item.created = ([string]$item.created).Substring(9)
    $item.exited = ([string]$item.exited).Substring(9)
    $path = if ($item.kind -eq 'bootstrap') { Join-Path $GameRoot 'G1R-Win64-Shipping.exe' }
            elseif ($item.kind -eq 'inner') { Join-Path $GameRoot 'G1R\Binaries\Win64\G1R-Win64-Shipping.exe' }
            else { Join-Path $GameRoot 'Other.exe' }
    $global:records[[int]$item.pid] = [pscustomobject]@{
        pid = [int]$item.pid; created = [string]$item.created; path = $path;
        parent = [int]$item.parent; kind = [string]$item.kind;
        late = [bool]$item.late; running = $true; exited = [string]$item.exited
    }
}
function Get-FakeVisibleRecords {
    @($global:records.Values | Where-Object { $_.running -and (-not $_.late -or $global:parentStopped) })
}
function Get-Process {
    param($Name, $Id, $ErrorAction)
    foreach ($item in @(Get-FakeVisibleRecords)) {
        if ($null -ne $Id -and $item.pid -notin @($Id)) { continue }
        [pscustomobject]@{ Id = $item.pid; ProcessId = $item.pid; Path = $item.path;
            ExecutablePath = $item.path; StartTime = [DateTime]::Parse($item.created);
            HasExited = -not $item.running }
    }
}
function Get-CimInstance {
    param($ClassName, $Filter, $ErrorAction)
    foreach ($item in @(Get-FakeVisibleRecords)) {
        if ($Filter -match 'ProcessId\s*=\s*(\d+)' -and $item.pid -ne [int]$Matches[1]) { continue }
        if ($Filter -match 'ParentProcessId\s*=\s*(\d+)' -and $item.parent -ne [int]$Matches[1]) { continue }
        $created = [DateTime]::Parse($item.created)
        $parent = $item.parent
        if ($Filter -like 'Name=*' -and $item.pid -eq $scenario.selected_pid) {
            $created = $created.AddMilliseconds([int]$scenario.stale_creation_ms)
            $parent += [int]$scenario.stale_parent_delta
        }
        [pscustomobject]@{ ProcessId = $item.pid; ParentProcessId = $parent;
            Name = 'G1R-Win64-Shipping.exe'; ExecutablePath = $item.path;
            CreationDate = $created }
    }
}
function Get-G1RProcessIdentity {
    param([uint32]$ProcessId)
    $item = $global:records[[int]$ProcessId]
    if ($null -eq $item -or -not $item.running -or ($item.late -and -not $global:parentStopped)) { throw 'process unavailable' }
    [pscustomobject]@{ pid = $item.pid; creation_time = $item.created; executable = $item.path }
}
function ConvertTo-G1RProcessIdentity {
    param($Identity)
    [pscustomobject]@{ pid = [int]$Identity.Pid; creation_time = [string]$Identity.CreationTime;
        executable = [string]$Identity.Executable }
}
function Open-G1RCheckedProcess {
    param([uint32]$ExpectedPid, [string]$ExpectedCreationTime, [string]$ExpectedExecutable, [switch]$ForTermination)
    $item = $global:records[[int]$ExpectedPid]
    if ($null -eq $item -or $item.created -cne $ExpectedCreationTime -or $item.path -ine $ExpectedExecutable) { throw 'process changed' }
    $null = $global:opened.Add([int]$ExpectedPid)
    $lease = [pscustomobject]@{ Identity = [pscustomobject]@{ Pid = $item.pid;
        CreationTime = $item.created; Executable = $item.path }; Pid = $item.pid; IsDisposed = $false }
    $lease | Add-Member -MemberType ScriptMethod -Name IsRunning -Value { $global:records[[int]$this.Pid].running }
    $lease | Add-Member -MemberType ScriptMethod -Name Wait -Value { param($milliseconds); -not $global:records[[int]$this.Pid].running }
    $lease | Add-Member -MemberType ScriptMethod -Name GetExitTime -Value { $global:records[[int]$this.Pid].exited }
    $lease | Add-Member -MemberType ScriptMethod -Name Dispose -Value {
        $null = $global:disposed.Add([int]$this.Pid); $this.IsDisposed = $true
    }
    $lease
}
function Stop-G1RCheckedProcess {
    param($Lease, [int]$GraceMilliseconds, [int]$ForceWaitMilliseconds, [switch]$AllowForce)
    $item = $global:records[[int]$Lease.Pid]
    $null = $global:actions.Add([int]$Lease.Pid)
    if ($scenario.fail_stop_pid -eq $item.pid) { throw 'isolated stop failure' }
    $item.running = $false
    if ($item.kind -eq 'bootstrap') { $global:parentStopped = $true }
    [pscustomobject]@{ state = 'stopped'; mode = 'forced'; process = ConvertTo-G1RProcessIdentity $Lease.Identity;
        outcome_unknown = $false }
}
function Start-Sleep { param($Milliseconds, $Seconds) }
$result = $null
$failure = $null
try {
    $result = & $StopPath -GameRoot $GameRoot -ExpectedPid ([uint32]$scenario.selected_pid) `
        -ExpectedCreationTime (([string]$scenario.expected_created).Substring(9)) -Confirm:$false -Json:([bool]$scenario.json)
    if ($scenario.json) { $result = $result | ConvertFrom-Json }
} catch { $failure = $_.Exception.Message }
[pscustomobject]@{ result = $result; error = $failure; actions = @($global:actions);
    opened = @($global:opened); disposed = @($global:disposed);
    running = @($global:records.Values | Where-Object running | ForEach-Object pid) } |
    ConvertTo-Json -Depth 12 -Compress
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Нужны Windows и PowerShell")
class BootstrapCleanupTests(unittest.TestCase):
    def _process(self, pid: int, kind: str, *, parent: int = 0,
                 created: str = CHILD_CREATED, late: bool = False) -> dict:
        return {"pid": pid, "kind": kind, "parent": parent, "created": created,
                "late": late, "exited": PARENT_EXITED}

    def _run(self, processes: list[dict], *, selected_pid: int = 100,
             expected_created: str = PARENT_CREATED, fail_stop_pid: int = 0,
             json_result: bool = False, stale_creation_ms: int = 0,
             stale_parent_delta: int = 0) -> dict:
        with tempfile.TemporaryDirectory(prefix="g1r-bootstrap-model-") as temporary:
            base = Path(temporary)
            game = base / "game"
            inner = game / "G1R" / "Binaries" / "Win64"
            inner.mkdir(parents=True)
            (game / "G1R-Win64-Shipping.exe").write_bytes(b"fixture")
            (inner / "G1R-Win64-Shipping.exe").write_bytes(b"fixture")
            (game / "Other.exe").write_bytes(b"fixture")
            source = STOP_SCRIPT.read_text(encoding="utf-8-sig")
            import_line = ". (Join-Path $PSScriptRoot 'game_process_common.ps1')"
            self.assertEqual(source.count(import_line), 1, "Изменился import seam stop-обёртки")
            source = source.replace(import_line, "# изолированные lease/process функции задаёт test harness")
            stop = base / "stop_model.ps1"
            stop.write_text(source, encoding="utf-8-sig")
            scenario = base / "scenario.json"
            encoded_processes = [{**item, "created": "identity:" + item["created"],
                                  "exited": "identity:" + item["exited"]} for item in processes]
            # Префикс сохраняет строковый FILETIME: ConvertFrom-Json иначе
            # автоматически преобразует ISO-дату в локальный DateTime.
            scenario.write_text(json.dumps({"processes": encoded_processes, "selected_pid": selected_pid,
                                           "expected_created": "identity:" + expected_created,
                                           "fail_stop_pid": fail_stop_pid,
                                           "json": json_result, "stale_creation_ms": stale_creation_ms,
                                           "stale_parent_delta": stale_parent_delta}), encoding="utf-8")
            harness = base / "harness.ps1"
            harness.write_text(HARNESS, encoding="utf-8-sig")
            result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                                     "-File", str(harness), "-ScenarioPath", str(scenario), "-StopPath", str(stop),
                                     "-GameRoot", str(game)], capture_output=True, text=True,
                                    encoding="utf-8", timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
            parsed = json.loads(result.stdout.strip())
            parsed["bootstrap_path"] = str(game / "G1R-Win64-Shipping.exe")
            return parsed

    def test_selected_bootstrap_stops_without_inner(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED)])
        self.assertIsNone(result["error"])
        self.assertEqual(result["actions"], [100])
        self.assertEqual(result["result"]["process"]["pid"], 100)
        self.assertEqual(result["result"]["process"]["executable"], result["bootstrap_path"])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_stale_bootstrap_identity_rejects_before_any_stop(self):
        result = self._run([self._process(100, "bootstrap", created=CHILD_CREATED)])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["actions"], [])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_stale_cim_creation_snapshot_rejects_before_handle_or_action(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED)],
                           stale_creation_ms=-1000)
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["disposed"], [])

    def test_stale_cim_parent_snapshot_rejects_before_handle_or_action(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED)],
                           stale_parent_delta=9)
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["actions"], [])
        self.assertEqual(result["opened"], [])
        self.assertEqual(result["disposed"], [])

    def test_bound_child_is_closed_after_bootstrap(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100)])
        self.assertIsNone(result["error"])
        self.assertEqual(result["actions"], [100, 200])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_late_child_inside_parent_lifetime_is_closed(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100, late=True)])
        self.assertIsNone(result["error"])
        self.assertEqual(result["actions"], [100, 200])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_foreign_same_path_inner_remains_untouched(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=999)])
        self.assertNotIn(200, result["actions"])
        self.assertEqual(result["actions"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn(200, result["running"])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_late_child_after_original_parent_exit_is_not_authorized(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100, late=True,
                                          created="2026-10-31T00:00:04.0000000Z")])
        self.assertNotIn(200, result["actions"])
        self.assertEqual(result["actions"], [100])
        self.assertIsNotNone(result["error"])
        self.assertIn(200, result["running"])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_child_older_than_parent_is_not_authorized(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100,
                                          created="2026-10-30T23:59:59.0000000Z")])
        self.assertNotIn(200, result["actions"])
        self.assertEqual(result["actions"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn(200, result["running"])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_selected_inner_does_not_stop_unbound_bootstrap(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=999)], selected_pid=200,
                           expected_created=CHILD_CREATED)
        self.assertNotIn(100, result["actions"])
        self.assertEqual(result["actions"], [])
        self.assertIsNotNone(result["error"])
        self.assertIn(100, result["running"])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_selected_inner_stops_bound_bootstrap_then_original_inner(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100)], selected_pid=200,
                           expected_created=CHILD_CREATED)
        self.assertIsNone(result["error"])
        self.assertEqual(result["actions"], [100, 200])
        self.assertEqual(result["result"]["process"]["pid"], 200)
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_ambiguous_inner_instances_refuse_before_any_stop(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100),
                            self._process(201, "inner", parent=100)])
        self.assertIsNotNone(result["error"])
        self.assertEqual(result["actions"], [])
        self.assertCountEqual(result["disposed"], result["opened"])

    def test_failure_after_bootstrap_stop_reports_unknown_outcome(self):
        result = self._run([self._process(100, "bootstrap", created=PARENT_CREATED),
                            self._process(200, "inner", parent=100)],
                           fail_stop_pid=200, json_result=True)
        self.assertIsNone(result["error"])
        self.assertEqual(result["actions"], [100, 200])
        self.assertEqual(result["result"]["state"], "failed")
        self.assertTrue(result["result"]["outcome_unknown"])
        self.assertCountEqual(result["disposed"], result["opened"])


if __name__ == "__main__":
    unittest.main()
