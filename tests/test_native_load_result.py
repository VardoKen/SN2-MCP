"""Реальный PowerShell parser результата C++ loader, без процессов игры."""
import json
import os
from pathlib import Path
import subprocess
import unittest


class NativeLoadResultTests(unittest.TestCase):
    def test_actual_native_success_and_failure_contract(self):
        if os.name != "nt":
            self.skipTest("Проверка Windows PowerShell")
        root = Path(__file__).resolve().parents[1]
        helper = str(root / "tools" / "native_load_result.ps1").replace("'", "''")
        command = "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false); . '" + helper + "'; $accepted=@();foreach($status in @('loaded','already_loaded')){$accepted += (ConvertFrom-G1RNativeLoadResult -Source ('{\"ok\":true,\"status\":\"'+$status+'\",\"pid\":123}') -ExpectedPid 123).status};$refused=0;foreach($raw in @('{\"ok\":true,\"status\":\"loaded\",\"pid\":124}','{\"ok\":false,\"code\":\"bad\"}','{}','{\"loaded\":true,\"pid\":123}')){try{$null=ConvertFrom-G1RNativeLoadResult -Source $raw -ExpectedPid 123}catch{$refused++}};[pscustomobject]@{accepted=$accepted;refused=$refused}|ConvertTo-Json -Compress"
        result = subprocess.run([str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"),
                                 "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report, {"accepted": ["loaded", "already_loaded"], "refused": 4})


if __name__ == "__main__":
    unittest.main()
