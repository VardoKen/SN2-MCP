"""Проверки фактической Git-identity источников на изолированных репозиториях."""

from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.sources import SourceCatalog


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.container = Path(self.temporary.name)
        self.root = self.container / "Modkit"
        (self.root / "Script").mkdir(parents=True)
        self.source = self.root / "Script" / "Fixture.as"
        self.source.write_bytes(b"void Fixture() {}\n")

    def git(self, *arguments, cwd=None):
        if shutil.which("git") is None:
            self.skipTest("Git отсутствует для изолированной функциональной проверки")
        return subprocess.run(["git", "-c", "core.autocrlf=false", *arguments],
                              cwd=cwd or self.root, capture_output=True, text=True, check=True)

    def repository(self, root=None, commit=True):
        root = root or self.root
        self.git("init", "--quiet", cwd=root)
        if commit:
            self.git("add", "--", ".", cwd=root)
            self.git("-c", "user.name=Source Identity Test", "-c", "user.email=source-identity@example.invalid",
                     "-c", "commit.gpgsign=false", "commit", "--quiet", "-m", "Изолированная fixture", cwd=root)
            return self.git("rev-parse", "HEAD", cwd=root).stdout.strip()
        return None

    def assert_source_context(self, result, revision):
        self.assertEqual(result["snapshot_kind"], "sequential_git_reads")
        self.assertEqual(result["configured_revision"], revision)
        self.assertEqual(result["revision_source"], "caller_label")
        self.assertIs(result["source_only"], True)
        self.assertIs(result["game_compatibility_checked"], False)
        inspected = datetime.fromisoformat(result["inspected_at"].replace("Z", "+00:00"))
        self.assertEqual(inspected.utcoffset(), timezone.utc.utcoffset(inspected))

    def test_actual_head_is_distinct_from_the_configured_label_and_clean_state(self):
        head = self.repository()
        before = self.source.read_bytes()
        for revision, matches in ((head, True), ("0" * 40, False), (None, None)):
            with self.subTest(revision=revision):
                result = SourceCatalog(self.root, revision=revision).source_identity()
                self.assert_source_context(result, revision)
                observed = result["git"]
                self.assertIs(observed["available"], True)
                self.assertEqual(Path(observed["repository_root"]).resolve(), self.root.resolve())
                self.assertIs(observed["repository_matches_source_root"], True)
                self.assertEqual(observed["head"], head)
                self.assertIs(observed["working_tree_changed"], False)
                self.assertIs(observed["configured_revision_matches_head"], matches)
                self.assertEqual(observed["read_status"], {"repository": "ok", "head": "ok", "working_tree": "ok"})
                self.assertEqual(observed["exit_codes"], {"repository": 0, "head": 0, "working_tree": 0})
        self.assertEqual(self.source.read_bytes(), before)

    def test_dirty_state_includes_tracked_changes_and_untracked_files_without_writing_sources(self):
        head = self.repository()
        catalog = SourceCatalog(self.root, revision=head)
        self.source.write_bytes(b"void Fixture() { Changed(); }\n")
        modified = self.source.read_bytes()
        self.assertIs(catalog.source_identity()["git"]["working_tree_changed"], True)
        self.assertEqual(self.source.read_bytes(), modified)
        self.source.write_bytes(b"void Fixture() {}\n")
        untracked = self.root / "Script" / "New.as"
        untracked.write_bytes(b"void NewFunction() {}\n")
        before = {path: path.read_bytes() for path in (self.source, untracked)}
        result = catalog.source_identity()
        self.assertEqual(result["git"]["head"], head)
        self.assertIs(result["git"]["working_tree_changed"], True)
        self.assertIs(result["git"]["configured_revision_matches_head"], True)
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_parent_repository_does_not_become_the_modkit_repository_identity(self):
        self.repository(self.container)
        result = SourceCatalog(self.root).source_identity()
        self.assert_source_context(result, None)
        observed = result["git"]
        self.assertIs(observed["available"], True)
        self.assertEqual(Path(observed["repository_root"]).resolve(), self.container.resolve())
        self.assertIs(observed["repository_matches_source_root"], False)
        self.assertIsNone(observed["head"])
        self.assertIsNone(observed["working_tree_changed"])
        self.assertIsNone(observed["configured_revision_matches_head"])
        self.assertEqual(observed["read_status"], {"repository": "foreign_repository", "head": "not_inspected", "working_tree": "not_inspected"})
        self.assertIsNone(observed["exit_codes"]["head"])
        self.assertIsNone(observed["exit_codes"]["working_tree"])

    def test_nonrepository_and_unborn_head_report_unknown_without_claiming_clean_identity(self):
        if shutil.which("git") is None:
            self.skipTest("Git отсутствует для изолированной функциональной проверки")
        absent = SourceCatalog(self.root, revision="configured-label").source_identity()["git"]
        self.assertIs(absent["available"], True)
        self.assertEqual(absent["read_status"]["repository"], "failed")
        self.assertIsNone(absent["repository_matches_source_root"])
        self.assertIsNone(absent["head"])
        self.assertIsNone(absent["working_tree_changed"])
        self.assertIsNone(absent["configured_revision_matches_head"])
        self.repository(commit=False)
        unborn = SourceCatalog(self.root, revision="configured-label").source_identity()["git"]
        self.assertIs(unborn["repository_matches_source_root"], True)
        self.assertEqual(unborn["read_status"]["head"], "failed")
        self.assertIsNone(unborn["head"])
        self.assertIsNone(unborn["configured_revision_matches_head"])
        self.assertEqual(unborn["read_status"]["working_tree"], "ok")
        self.assertIs(unborn["working_tree_changed"], True)

    def test_git_is_inspected_lazily_and_missing_executable_remains_unavailable(self):
        with patch("subprocess.run", side_effect=FileNotFoundError("Git отсутствует")) as runner:
            catalog = SourceCatalog(self.root, revision="configured-label")
            self.assertEqual(catalog.list_functions()["items"][0]["name"], "Fixture")
            runner.assert_not_called()
            result = catalog.source_identity()
        self.assert_source_context(result, "configured-label")
        observed = result["git"]
        self.assertIs(observed["available"], False)
        self.assertIsNone(observed["repository_root"])
        self.assertIsNone(observed["head"])
        self.assertIsNone(observed["working_tree_changed"])
        self.assertIsNone(observed["configured_revision_matches_head"])
        self.assertEqual(set(observed["read_status"].values()), {"unavailable"})
        self.assertEqual(set(observed["exit_codes"].values()), {None})

    def test_failed_head_or_worktree_reads_keep_independent_facts_and_unknown_fields(self):
        head = self.repository()
        run = subprocess.run
        for command, failed_status in ((["rev-parse", "HEAD"], "head"), (["status"], "working_tree")):
            with self.subTest(failed=failed_status):
                def interrupted(arguments, *args, **kwargs):
                    is_selected = all(part in arguments for part in command)
                    if is_selected:
                        return subprocess.CompletedProcess(arguments, 7, stdout="", stderr="Изолированная ошибка чтения Git")
                    return run(arguments, *args, **kwargs)
                with patch("subprocess.run", side_effect=interrupted):
                    result = SourceCatalog(self.root, revision=head).source_identity()
                self.assert_source_context(result, head)
                observed = result["git"]
                self.assertIs(observed["repository_matches_source_root"], True)
                self.assertEqual(observed["read_status"][failed_status], "failed")
                self.assertEqual(observed["exit_codes"][failed_status], 7)
                if failed_status == "head":
                    self.assertIsNone(observed["head"])
                    self.assertIsNone(observed["configured_revision_matches_head"])
                    self.assertIs(observed["working_tree_changed"], False)
                else:
                    self.assertEqual(observed["head"], head)
                    self.assertIs(observed["configured_revision_matches_head"], True)
                    self.assertIsNone(observed["working_tree_changed"])


if __name__ == "__main__":
    unittest.main()
