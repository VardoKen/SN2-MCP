"""Изолированное управление общим MCP-процессом; сервер и игра не запускаются."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


def quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Нужны Windows и PowerShell")
class SharedMCPScriptsTests(unittest.TestCase):
    def run_script(self, body: str, *, fake_root: Path | None = None) -> dict:
        command = (
            "$ErrorActionPreference='Stop'; "
            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
            ". " + quote(str(ROOT / "tools" / "stop_shared_mcp.ps1")) + "; "
        )
        if fake_root is not None:
            command += "$configuration=Get-G1RSharedConfiguration " + quote(str(fake_root)) + "; "
        command += body
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, encoding="utf-8", timeout=35,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout.strip())

    def test_both_scripts_parse_and_dot_source_has_no_process_effect(self):
        result = self.run_script(
            "$errors=@(); foreach($path in @(" + ",".join(quote(str(ROOT / "tools" / name)) for name in ("start_shared_mcp.ps1", "stop_shared_mcp.ps1")) + ")) { "
            "$tokens=$null; $parseErrors=$null; $null=[Management.Automation.Language.Parser]::ParseFile($path,[ref]$tokens,[ref]$parseErrors); $errors += $parseErrors }; "
            "@{errors=$errors.Count; start=[bool](Get-Command Invoke-G1RSharedStart); stop=[bool](Get-Command Invoke-G1RSharedStop)} | ConvertTo-Json -Compress"
        )
        self.assertEqual(result, {"errors": 0, "start": True, "stop": True})

    def test_stop_without_manifest_is_idempotent_and_conflict_never_stops(self):
        with tempfile.TemporaryDirectory(prefix="g1r-shared-test-") as folder:
            fake_root = Path(folder) / "repository"
            fake_root.mkdir()
            result = self.run_script(
                "function Get-G1RSharedListeners { @() }; "
                "function Assert-G1RSharedNoUntrackedProcess {}; "
                "function Stop-G1RCheckedProcess { throw 'unexpected_stop' }; "
                "$first=Invoke-G1RSharedStop $configuration; $second=Invoke-G1RSharedStop $configuration; "
                "function Get-G1RSharedListeners { @([pscustomobject]@{LocalPort=27124;LocalAddress='127.0.0.1';OwningProcess=123}) }; "
                "$message=''; try {Invoke-G1RSharedStop $configuration} catch {$message=$_.Exception.Message}; "
                "@{first=$first.state;second=$second.state;error=$message} | ConvertTo-Json -Compress",
                fake_root=fake_root,
            )
            self.assertEqual(result["first"], "already_stopped")
            self.assertEqual(result["second"], "already_stopped")
            self.assertIn("shared_port_conflict", result["error"])

    def test_manifest_rejects_changed_command_and_changed_pid_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="g1r-shared-test-") as folder:
            fake_root = Path(folder) / "repository"
            fake_root.mkdir()
            result = self.run_script(
                "$null=New-Item -ItemType Directory -Path $configuration.artifacts; "
                "$record=[pscustomobject]@{schema_version=1;owner='SN2_MCP_shared_server';repository_root=$configuration.root;server_script=$configuration.script;host=$configuration.host;port=$configuration.port;native_port=$configuration.native_port;arguments=$configuration.arguments;command_line=$configuration.command_line;pid=456;creation_time='2026-10-31T00:00:00.0000000Z';executable=$configuration.base_python;launcher=[pscustomobject]@{pid=455;creation_time='2026-10-31T00:00:00.0000000Z';executable=$configuration.python;command_line=$configuration.command_line}}; "
                "Write-G1RSharedManifest $configuration $record; $loaded=Read-G1RSharedManifest $configuration; "
                "function Get-CimInstance { @([pscustomobject]@{ExecutablePath='C:\\foreign.exe';CommandLine='foreign'}) }; "
                "function Stop-G1RCheckedProcess { throw 'unexpected_stop' }; "
                "$identityError=''; try {Invoke-G1RSharedStop $configuration} catch {$identityError=$_.Exception.Message}; "
                "$preserved=Test-Path -LiteralPath $configuration.manifest; $record.command_line='foreign'; Write-G1RSharedManifest $configuration $record; "
                "$manifestError=''; try {Read-G1RSharedManifest $configuration} catch {$manifestError=$_.Exception.Message}; "
                "@{loaded_pid=$loaded.pid;identity_error=$identityError;preserved=$preserved;manifest_error=$manifestError} | ConvertTo-Json -Compress",
                fake_root=fake_root,
            )
            self.assertEqual(result["loaded_pid"], 456)
            self.assertTrue(result["preserved"])
            self.assertIn("shared_process_changed", result["identity_error"])
            self.assertIn("shared_invalid_manifest", result["manifest_error"])

    def test_existing_owned_server_does_not_start_duplicate_and_stop_uses_lease(self):
        with tempfile.TemporaryDirectory(prefix="g1r-shared-test-") as folder:
            fake_root = Path(folder) / "repository"
            (fake_root / ".venv" / "Scripts").mkdir(parents=True)
            (fake_root / ".venv" / "Scripts" / "python.exe").write_bytes(b"isolated")
            (fake_root / "src" / "sn2_mcp").mkdir(parents=True)
            (fake_root / "src" / "sn2_mcp" / "server.py").write_bytes(b"isolated")
            (fake_root.parent / "G1R-Modkit").mkdir()
            result = self.run_script(
                "$null=New-Item -ItemType Directory -Path $configuration.artifacts; "
                "$record=[pscustomobject]@{schema_version=1;owner='SN2_MCP_shared_server';repository_root=$configuration.root;server_script=$configuration.script;host=$configuration.host;port=$configuration.port;native_port=$configuration.native_port;arguments=$configuration.arguments;command_line=$configuration.command_line;pid=456;creation_time='2026-10-31T00:00:00.0000000Z';executable=$configuration.base_python;launcher=[pscustomobject]@{pid=455;creation_time='2026-10-31T00:00:00.0000000Z';executable=$configuration.python;command_line=$configuration.command_line}}; Write-G1RSharedManifest $configuration $record; "
                "function Open-G1RSharedProcess {$l=[pscustomobject]@{};$l|Add-Member ScriptMethod Dispose {};return $l}; "
                "function Get-G1RSharedListeners {@([pscustomobject]@{LocalPort=27124;LocalAddress='127.0.0.1';OwningProcess=456})}; "
                "function Start-Process {throw 'unexpected_duplicate'}; "
                "function Stop-G1RCheckedProcess {param($Lease,$GraceMilliseconds,$ForceWaitMilliseconds,[switch]$AllowForce); if($null -eq $Lease -or -not $AllowForce){throw 'wrong_lease'};[pscustomobject]@{state='stopped';mode='forced'}}; "
                "$first=Invoke-G1RSharedStart $configuration 1; $second=Invoke-G1RSharedStart $configuration 1; $stop=Invoke-G1RSharedStop $configuration; "
                "@{first=$first.state;second=$second.state;stop=$stop.state;manifest_exists=(Test-Path -LiteralPath $configuration.manifest)} | ConvertTo-Json -Compress",
                fake_root=fake_root,
            )
            self.assertEqual(result, {"first": "already_running", "second": "already_running", "stop": "stopped", "manifest_exists": False})

    def test_real_venv_redirector_listener_is_tracked_and_both_owned_processes_stop(self):
        if not (ROOT / ".venv" / "Scripts" / "python.exe").exists():
            self.skipTest("Нет локального Python venv")
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        with tempfile.TemporaryDirectory(prefix="g1r-shared-stub-") as folder:
            root = Path(folder)
            script = root / "listener.py"
            script.write_text(
                "import socket,sys,time\ns=socket.socket()\ns.bind(('127.0.0.1',int(sys.argv[1])))\ns.listen()\ntime.sleep(45)\n",
                encoding="utf-8",
            )
            result = self.run_script(
                "$configuration=Get-G1RSharedConfiguration " + quote(str(ROOT)) + "; "
                "$configuration.artifacts=" + quote(str(root)) + "; "
                "$configuration.manifest=" + quote(str(root / "sharedserver.json")) + "; "
                "$configuration.stdout=" + quote(str(root / "stdout.log")) + "; "
                "$configuration.stderr=" + quote(str(root / "stderr.log")) + "; "
                "$configuration.port=" + str(port) + "; "
                "$configuration.arguments='-B " + '"' + str(script) + '" ' + str(port) + "'; "
                "$configuration.command_line='\"'+$configuration.python+'\" '+$configuration.arguments; "
                "$started=$null; try {$started=Invoke-G1RSharedStart $configuration 10; "
                "$listeners=@(Get-G1RSharedListeners $configuration); $duplicate=Invoke-G1RSharedStart $configuration 1; "
                "$stopped=Invoke-G1RSharedStop $configuration; "
                "@{state=$started.state;duplicate=$duplicate.state;server_pid=$started.process.pid;launcher_pid=$started.process.launcher.pid;listener_pids=@($listeners.OwningProcess);stopped=$stopped.state;server_remains=@(Get-CimInstance Win32_Process -Filter ('ProcessId='+$started.process.pid)).Count;launcher_remains=@(Get-CimInstance Win32_Process -Filter ('ProcessId='+$started.process.launcher.pid)).Count} | ConvertTo-Json -Compress "
                "} finally {if(Test-Path -LiteralPath $configuration.manifest){$null=Invoke-G1RSharedStop $configuration}}",
            )
            self.assertEqual(result["state"], "started")
            self.assertEqual(result["duplicate"], "already_running")
            self.assertEqual(result["listener_pids"], [result["server_pid"]])
            self.assertEqual(result["server_remains"], 0)
            self.assertEqual(result["launcher_remains"], 0)
            self.assertIn(result["stopped"], {"stopped", "already_exited"})


if __name__ == "__main__":
    unittest.main()
