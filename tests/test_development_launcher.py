"""Проверка production launcher через Windows PowerShell -File и -WhatIf."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
LAUNCHER = REPO / "tools" / "start_sn2_development.ps1"
POWERSHELL = Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"


@unittest.skipUnless(os.name == "nt" and POWERSHELL.is_file(), "Нужен Windows PowerShell")
class DevelopmentLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="g1r-launcher-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game = self.root / "game"
        (self.game / "G1R/Binaries/Win64").mkdir(parents=True)
        (self.game / "G1R-Win64-Shipping.exe").write_bytes(b"")
        (self.game / "G1R/Binaries/Win64/G1R-Win64-Shipping.exe").write_bytes(b"")
        self.steam = self.root / "steam.exe"
        self.steam.write_bytes(b"")
        self.mods = self.root / "packages"
        self.mods.mkdir()
        for name in ("Main", "Helper", "Second", "Third", "Fourth"):
            self.package(name)

    def package(self, name, *, descriptor_name=None):
        package = self.mods / name
        package.mkdir(exist_ok=True)
        (package / "GothicMod.json").write_text(
            json.dumps({"name": name if descriptor_name is None else descriptor_name}),
            encoding="utf-8",
        )

    def launch(self, *arguments):
        # Даже ошибочная регрессия не сможет выполнить эти пустые EXE. -WhatIf
        # дополнительно исключает запрос Steam и управление игровым процессом.
        return subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(LAUNCHER), "-GameRoot", str(self.game), "-SteamExe", str(self.steam),
             "-WhatIf", "-Json", *arguments],
            capture_output=True, text=True, encoding="utf-8", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

    def success(self, *arguments):
        result = self.launch(*arguments)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        # ShouldProcess печатает отдельную строку WhatIf перед JSON.
        return json.loads(result.stdout.splitlines()[-1])

    def failure(self, raw, *, extra=()):
        result = self.launch("-ModsDirectory", str(self.mods), "-ModsCsv", raw, *extra)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertTrue(result.stderr)
        self.assertNotIn("launch_requested", result.stdout)

    def test_two_packages_cross_native_file_binding_and_keep_order(self):
        result = self.success("-ModsDirectory", str(self.mods), "-ModsCsv", "Helper,Main")
        self.assertEqual(result["mods"], ["Helper", "Main"])
        self.assertIsNone(result["package_name"])
        self.assertEqual(Path(result["mods_directory"]), self.mods.resolve())
        self.assertIn('-Mods="Helper,Main"', result["Arguments"])
        self.assertIn('-ModsDirectory="' + self.mods.as_posix() + '"', result["Arguments"])
        self.assertNotIn("-ingametools", result["Arguments"])
        self.assertIn("-as-exit-on-error", result["Arguments"])

    def test_single_legacy_mod_and_no_selection_remain_supported(self):
        selected = self.success("-ModsDirectory", str(self.mods), "-Mods", "Main")
        self.assertEqual(selected["package_name"], "Main")
        self.assertEqual(selected["mods"], ["Main"])
        self.assertIn('-Mods="Main"', selected["Arguments"])
        default = self.success()
        self.assertIsNone(default["package_name"])
        self.assertEqual(default["mods"], [])
        self.assertIsNone(default["mods_directory"])

    def test_four_csv_packages_are_accepted(self):
        result = self.success("-ModsDirectory", str(self.mods), "-ModsCsv",
                              "Main,Helper,Second,Third,Fourth")
        self.assertEqual(result["mods"], ["Main", "Helper", "Second", "Third", "Fourth"])

    def test_rejects_empty_and_oversized_csv(self):
        invalid = ("", " ", "M" * 516,
                   "Main,", ",Main", "Main,,Helper")
        for raw in invalid:
            with self.subTest(raw=raw[:80]):
                self.failure(raw)

    def test_rejects_duplicate_names_case_insensitively_and_unsafe_names(self):
        for raw in ("Main,Main", "Main,main", "../Main", "Main/Helper", "Main Helper",
                    "Main, Helper", "Main\nHelper", "Main\tHelper", "Main\n", "Main\r",
                    '"Main"', "M" * 129):
            with self.subTest(raw=raw):
                self.failure(raw)

    def test_rejects_both_interfaces_and_missing_directory(self):
        self.failure("Main", extra=("-Mods", "Main"))
        result = self.launch("-ModsCsv", "Main")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ModsDirectory", result.stderr)

    def test_checks_every_selected_descriptor_and_exact_name(self):
        self.failure("Main,Absent")
        self.package("Helper", descriptor_name="helper")
        self.failure("Main,Helper")
        (self.mods / "Helper/GothicMod.json").write_text("{", encoding="utf-8")
        result = self.launch("-ModsDirectory", str(self.mods), "-ModsCsv", "Main,Helper")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("launch_requested", result.stdout)


if __name__ == "__main__":
    unittest.main()
