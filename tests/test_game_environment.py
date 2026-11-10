"""Изолированные проверки сверки среды без обращения к установленной игре."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from inspect_game_environment import InspectionError, InspectionOptions, inspect_environment, main, parse_manifest


MANIFEST = '''"AppState"
{
    "appid" "655540"
    "buildid" "26295476"
    "installdir" "Game"
    "LastUpdated" "1793300000"
    "LastOwner" "private-owner-123"
    "UserConfig" { "betakey" "public" "private" "do-not-export" }
    "InstalledDepots" { "1" { "manifest" "private-depot" } }
}
'''


class ManifestTests(unittest.TestCase):
    def test_only_allowlisted_metadata_is_returned(self):
        metadata = parse_manifest(MANIFEST)
        self.assertEqual(metadata, {"appid": "655540", "buildid": "26295476", "installdir": "Game", "LastUpdated": "1793300000", "betakey": "public"})
        self.assertNotIn("private", json.dumps(metadata))

    def test_comments_and_bom_are_accepted(self):
        self.assertEqual(parse_manifest("\ufeff// Комментарий\n" + MANIFEST)["appid"], "655540")

    def test_fields_in_unrelated_sections_cannot_override_metadata(self):
        altered = MANIFEST.replace('"InstalledDepots" {', '"betakey" "not-selected" "InstalledDepots" { "buildid" "private-build"')
        self.assertEqual(parse_manifest(altered)["buildid"], "26295476")
        self.assertEqual(parse_manifest(altered)["betakey"], "public")
        without_user_beta = altered.replace('"betakey" "public"', '"other" "public"')
        self.assertNotIn("betakey", parse_manifest(without_user_beta))

    def test_malformed_and_duplicate_keys_do_not_expose_values(self):
        cases = (
            MANIFEST[:-3], MANIFEST + "private-garbage", MANIFEST.replace('"buildid" "26295476"', '"buildid" "secret"'),
            MANIFEST.replace('"appid" "655540"', '"appid" "655540" "APPID" "private-owner"'),
            MANIFEST.replace('"installdir" "Game"', '"installdir" "../private"'),
            MANIFEST.replace('"UserConfig" {', '"UserConfig" "private" {'),
        )
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(InspectionError) as captured:
                    parse_manifest(text)
                self.assertEqual(captured.exception.code, "invalid_manifest")
                self.assertNotIn("private", str(captured.exception))
                self.assertNotIn("secret", str(captured.exception))


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.game = self.root / "Game"
        self.modkit = self.root / "Modkit"
        self.source = self.modkit / "Script"
        self.target = self.game / "G1R" / "Script"
        self.source.mkdir(parents=True)
        self.target.mkdir(parents=True)
        self.manifest = self.root / "appmanifest.acf"
        self.manifest.write_text(MANIFEST, encoding="utf-8")

    def options(self, **changes):
        values = {"game_root": self.game, "modkit_root": self.modkit, "steam_manifest": self.manifest}
        values.update(changes)
        return InspectionOptions(**values)

    def write_pair(self, relative, source_data, game_data=None):
        source = self.source / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(source_data)
        if game_data is not None:
            target = self.target / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(game_data)

    def test_identical_different_missing_and_bounded_lists(self):
        self.write_pair("same.as", b"same", b"same")
        self.write_pair("nested/different.as", b"source", b"other")
        self.write_pair("missing-a.as", b"a")
        self.write_pair("missing-b.as", b"b")
        (self.source / "ignored.txt").write_text("not-a-script", encoding="utf-8")
        result = inspect_environment(self.options(max_list_entries=1))
        self.assertTrue(result["ok"])
        scripts = result["scripts"]
        self.assertEqual([scripts[key] for key in ("source_count", "compared", "identical", "different", "missing")], [4, 2, 1, 1, 2])
        self.assertEqual(scripts["different_files"], ["nested/different.as"])
        self.assertEqual(len(scripts["missing_files"]), 1)
        self.assertTrue(scripts["lists_truncated"])
        self.assertNotIn("executable", result)

    def test_none_returns_complete_difference_and_missing_lists_without_mutation(self):
        self.write_pair("same.as", b"same", b"same")
        different = [f"different/item{index:03}.as" for index in range(101)]
        missing = [f"missing/item{index:03}.as" for index in range(101)]
        for relative in different:
            self.write_pair(relative, b"source", b"game")
        for relative in missing:
            self.write_pair(relative, b"source")
        before = {path.relative_to(self.root): path.read_bytes()
                  for path in self.root.rglob("*") if path.is_file()}
        result = inspect_environment(self.options(max_list_entries=None))
        scripts = result["scripts"]
        self.assertEqual([scripts[key] for key in ("source_count", "compared", "identical", "different", "missing")],
                         [203, 102, 1, 101, 101])
        self.assertEqual(scripts["different_files"], different)
        self.assertEqual(scripts["missing_files"], missing)
        self.assertIsNone(scripts["max_list_entries"])
        self.assertFalse(scripts["lists_truncated"])
        self.assertEqual({path.relative_to(self.root): path.read_bytes()
                          for path in self.root.rglob("*") if path.is_file()}, before)

    def test_optional_executable_hash_is_streamed(self):
        self.write_pair("a.as", b"a", b"a")
        executable = self.game / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        data = b"isolated-executable" * 80_000
        executable.write_bytes(data)
        result = inspect_environment(self.options(hash_executable=True))
        self.assertEqual(result["executable"], {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)})

    def test_zero_source_and_missing_root_are_errors(self):
        for options, code in ((self.options(), "no_source_files"), (self.options(modkit_root=self.root / "missing"), "invalid_path")):
            with self.assertRaises(InspectionError) as captured:
                inspect_environment(options)
            self.assertEqual(captured.exception.code, code)

    def test_manifest_must_match_selected_game_directory(self):
        self.manifest.write_text(MANIFEST.replace('"installdir" "Game"', '"installdir" "Other"'), encoding="utf-8")
        with self.assertRaises(InspectionError) as captured:
            inspect_environment(self.options())
        self.assertEqual(captured.exception.code, "manifest_game_mismatch")

    def test_links_to_outside_files_and_directories_are_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "private.as").write_text("outside", encoding="utf-8")
        link = self.source / "linked"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Система не разрешает создавать symlink для изолированного теста")
        with self.assertRaises(InspectionError) as captured:
            inspect_environment(self.options())
        self.assertEqual(captured.exception.code, "unsafe_path")
        link.unlink()
        self.write_pair("private.as", b"local")
        target_link = self.target / "private.as"
        target_link.symlink_to(outside / "private.as")
        with self.assertRaises(InspectionError) as captured:
            inspect_environment(self.options())
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_dangling_link_cannot_be_reported_as_missing(self):
        self.write_pair("a.as", b"a")
        try:
            (self.target / "a.as").symlink_to(self.root / "does-not-exist")
        except OSError:
            self.skipTest("Система не разрешает создавать symlink для изолированного теста")
        with self.assertRaises(InspectionError) as captured:
            inspect_environment(self.options())
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_cli_failure_is_json_without_private_arguments(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--private-owner", "secret-value"])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertNotIn("secret-value", output.getvalue())

    def test_cli_success_does_not_modify_sources(self):
        self.write_pair("a.as", b"a", b"a")
        before = {file: file.read_bytes() for file in (self.manifest, self.source / "a.as", self.target / "a.as")}
        output = io.StringIO()
        with redirect_stdout(output):
            code = main(["--game-root", str(self.game), "--modkit-root", str(self.modkit), "--steam-manifest", str(self.manifest)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["scripts"]["identical"], 1)
        self.assertEqual(before, {file: file.read_bytes() for file in before})


class SourceGameWrapperTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.modkit = self.root / "Modkit"
        self.source = self.modkit / "Script"
        self.source.mkdir(parents=True)
        self.game = self.root / "steamapps" / "common" / "Game"
        self.target = self.game / "G1R" / "Script"
        self.target.mkdir(parents=True)
        self.manifest = self.game.parent.parent / "appmanifest_1297900.acf"
        self.manifest.write_text(MANIFEST.replace('"appid" "655540"', '"appid" "1297900"'), encoding="utf-8")

    def contents(self):
        return {path.relative_to(self.root): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def test_matching_subset_excludes_game_only_scripts_and_keeps_unknown_git_identity(self):
        from sn2_mcp.game_environment import verify_source_game
        raw = "// реальная изолированная копия\r\nvoid Same() {}\r\n".encode("utf-8")
        (self.source / "same.as").write_bytes(raw)
        (self.target / "same.as").write_bytes(raw)
        (self.target / "game-only.as").write_bytes(b"void ExtraGameOnly() {}\n")
        before = self.contents()
        with patch("subprocess.run", side_effect=FileNotFoundError("Git отсутствует")) as runner:
            result = verify_source_game(self.modkit, self.game, configured_revision="configured-label")
        self.assertTrue(result["ok"])
        self.assertEqual(result["correlation_scope"], "modkit_script_subset_byte_equality")
        self.assertTrue(result["subset_matches_game_scripts"])
        self.assertIs(result["game_only_scripts_checked"], False)
        self.assertIs(result["runtime_compatibility_checked"], False)
        self.assertEqual(result["snapshot_kind"], "sequential_file_reads")
        self.assertEqual(result["source_identity_observation"], "before_file_comparison")
        self.assertEqual(result["scripts"]["source_count"], 1)
        self.assertEqual(result["scripts"]["identical"], 1)
        self.assertIsNone(result["scripts"]["max_list_entries"])
        self.assertFalse(result["scripts"]["lists_truncated"])
        self.assertNotIn("executable", result)
        identity = result["source_identity"]
        self.assertEqual(identity["configured_revision"], "configured-label")
        self.assertEqual(identity["revision_source"], "caller_label")
        self.assertIs(identity["source_only"], True)
        self.assertIs(identity["game_compatibility_checked"], False)
        self.assertIs(identity["git"]["available"], False)
        self.assertIsNone(identity["git"]["head"])
        self.assertIsNone(identity["git"]["working_tree_changed"])
        runner.assert_called_once()
        times = [datetime.fromisoformat(result["inspection_started_at"].replace("Z", "+00:00")),
                 datetime.fromisoformat(identity["inspected_at"].replace("Z", "+00:00")),
                 datetime.fromisoformat(result["inspection_finished_at"].replace("Z", "+00:00"))]
        self.assertTrue(times[0] <= times[1] <= times[2])
        for instant in times:
            self.assertEqual(instant.utcoffset(), timezone.utc.utcoffset(instant))
        self.assertEqual(self.contents(), before)

    def test_wrapper_reports_mismatches_and_missing_files_with_optional_executable_hash(self):
        from sn2_mcp.game_environment import verify_source_game
        (self.source / "different.as").write_bytes(b"source")
        (self.target / "different.as").write_bytes(b"different")
        (self.source / "missing.as").write_bytes(b"source")
        executable = self.game / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"isolated optional executable")
        before = self.contents()
        with patch("subprocess.run", side_effect=FileNotFoundError("Git отсутствует")):
            result = verify_source_game(self.modkit, self.game, hash_executable=True)
        self.assertTrue(result["ok"])
        self.assertFalse(result["subset_matches_game_scripts"])
        self.assertEqual(result["scripts"]["different_files"], ["different.as"])
        self.assertEqual(result["scripts"]["missing_files"], ["missing.as"])
        self.assertEqual(result["executable"], {"sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                                               "size_bytes": len(executable.read_bytes())})
        self.assertEqual(result["manifest"]["appid"], "1297900")
        self.assertNotIn("private", json.dumps(result["manifest"]))
        self.assertIs(result["runtime_compatibility_checked"], False)
        self.assertEqual(self.contents(), before)


if __name__ == "__main__":
    unittest.main()
