"""Подготовка isolated ScriptPatch и локальная совместимость unified diff."""
import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from g1r_mcp.packages import PackageError
from g1r_mcp.workspace import WorkspaceError

spec = importlib.util.spec_from_file_location("prepare_patch_probe", ROOT / "tools/prepare_patch_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class PatchProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.workspace = self.artifacts / "patch-probe"
        self.workspace.mkdir()

    def test_prepare_exact_fixture_recipe_without_runtime_claim(self):
        report = prepare.prepare_probe(self.workspace, repository_root=self.root)
        package = self.workspace / prepare.PACKAGE_NAME
        self.assertTrue(report["validation"]["valid_structure"])
        self.assertEqual(report["validation"]["patch_count"], 1)
        self.assertEqual(report["validation"]["patch_validation"], "not_performed")
        self.assertEqual(report["verification"], "not_performed")
        self.assertEqual(report["runtime_probe"]["arguments"], {})
        self.assertEqual(report["runtime_probe"]["expected_signature"]["return"], "IntProperty")
        self.assertEqual(set(report["files_sha256"]), {"GothicMod.json", prepare.SCRIPT_RELATIVE, prepare.PATCH_RELATIVE})
        for name, digest in report["files_sha256"].items():
            raw = (package / name).read_bytes()
            self.assertEqual(raw, (prepare.FIXTURE_ROOT / name).read_bytes())
            self.assertEqual(hashlib.sha256(raw).hexdigest(), digest)
        self.assertFalse(any(path.name.startswith(".g1r-mcp-") for path in self.workspace.rglob("*")))

    def test_source_class_keyword_and_patch_context_are_consistent(self):
        source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_text(encoding="utf-8")
        diff = (prepare.FIXTURE_ROOT / prepare.PATCH_RELATIVE).read_text(encoding="utf-8")
        self.assertRegex(source, r"UCLASS\(\)\s+class UG1RMCPPatchProbe\s*:\s*UObject\s*\{")
        self.assertIn("@@ -5,6 +5,6 @@ class UG1RMCPPatchProbe : UObject", diff)
        self.assertEqual(source.count("return 10;"), 1)
        self.assertNotIn("return 11;", source)
        self.assertIn("-        return 10;", diff)
        self.assertIn("+        return 11;", diff)

    def test_missing_class_keyword_is_rejected_before_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary)
            (fixture / "Script").mkdir()
            (fixture / "ScriptPatches").mkdir()
            for name in ("GothicMod.json", prepare.SCRIPT_RELATIVE, prepare.PATCH_RELATIVE):
                raw = (prepare.FIXTURE_ROOT / name).read_bytes()
                if name == prepare.SCRIPT_RELATIVE:
                    raw = raw.replace(b"class UG1RMCPPatchProbe", b"UG1RMCPPatchProbe", 1)
                (fixture / name).write_bytes(raw)
            with patch.object(prepare, "FIXTURE_ROOT", fixture):
                with self.assertRaises(PackageError) as error:
                    prepare.prepare_probe(self.workspace, repository_root=self.root)
            self.assertEqual(error.exception.code, "fixture_invalid")
            self.assertEqual(list(self.workspace.iterdir()), [])

    def test_unsafe_root_and_duplicate_refuse_without_overwrite(self):
        with self.assertRaises((PackageError, WorkspaceError)):
            prepare.prepare_probe(self.root, repository_root=self.root)
        with self.assertRaises((PackageError, WorkspaceError)):
            prepare.prepare_probe(self.artifacts, repository_root=self.root)
        prepare.prepare_probe(self.workspace, repository_root=self.root)
        before = {path: path.read_bytes() for path in self.workspace.rglob("*") if path.is_file()}
        with self.assertRaises(PackageError) as error:
            prepare.prepare_probe(self.workspace, repository_root=self.root)
        self.assertEqual(error.exception.code, "already_exists")
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_patch_is_applicable_standard_git_unified_diff_to_own_fixture(self):
        script = self.root / prepare.SCRIPT_RELATIVE
        script.parent.mkdir()
        original = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
        script.write_bytes(original)
        diff = prepare.FIXTURE_ROOT / prepare.PATCH_RELATIVE
        checked = subprocess.run(["git", "apply", "--check", str(diff)], cwd=self.root, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors="replace"))
        applied = subprocess.run(["git", "apply", str(diff)], cwd=self.root, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr.decode(errors="replace"))
        self.assertEqual(script.read_bytes().replace(b"\r\n", b"\n"),
                         original.replace(b"\r\n", b"\n").replace(b"return 10;", b"return 11;", 1))
        # Git-применимость не означает совместимость с patch parser игры.
        self.assertEqual((prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes(), original)

    def test_substituted_stage_script_symlink_never_changes_external_sentinel(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside)
            victim = external / "G1RMCPPatchProbe.as"
            victim.write_bytes(b"external sentinel")
            original_write = prepare.PackageWorkspace.write_package_file
            substituted = []
            def substitute_script(writer, relative_path, file_path, content, expected_sha256=None):
                stage = self.workspace / relative_path
                if file_path == "GothicMod.json" and not substituted:
                    script = stage / "Script"
                    script.rmdir()
                    try:
                        script.symlink_to(external, target_is_directory=True)
                    except OSError:
                        script.mkdir()
                        self.skipTest("Система не разрешает изолированный symlink")
                    substituted.append(script)
                return original_write(writer, relative_path, file_path, content, expected_sha256)
            try:
                with patch.object(prepare.PackageWorkspace, "write_package_file", substitute_script):
                    with self.assertRaises(WorkspaceError) as error:
                        prepare.prepare_probe(self.workspace, repository_root=self.root)
                self.assertEqual(error.exception.code, "unsafe_path")
                self.assertEqual(victim.read_bytes(), b"external sentinel")
                self.assertFalse((self.workspace / prepare.PACKAGE_NAME).exists())
            finally:
                for link in substituted:
                    link.unlink()

    def test_foreign_filename_created_before_write_is_preserved(self):
        original_write = prepare.PackageWorkspace.write_package_file
        foreign = []
        def create_foreign(writer, relative_path, file_path, content, expected_sha256=None):
            if file_path == prepare.SCRIPT_RELATIVE:
                victim = self.workspace / relative_path / file_path
                victim.write_bytes(b"foreign sentinel")
                foreign.append(victim)
            return original_write(writer, relative_path, file_path, content, expected_sha256)
        with patch.object(prepare.PackageWorkspace, "write_package_file", create_foreign):
            with self.assertRaises(WorkspaceError) as error:
                prepare.prepare_probe(self.workspace, repository_root=self.root)
        self.assertEqual(error.exception.code, "conflict")
        self.assertEqual(foreign[0].read_bytes(), b"foreign sentinel")
        self.assertFalse((self.workspace / prepare.PACKAGE_NAME).exists())

    def test_ordinary_write_failure_removes_only_owned_stage(self):
        original_write = prepare.PackageWorkspace.write_package_file
        def fail_source(writer, relative_path, file_path, content, expected_sha256=None):
            if file_path == prepare.SCRIPT_RELATIVE:
                raise WorkspaceError("access_error", "fixture")
            return original_write(writer, relative_path, file_path, content, expected_sha256)
        with patch.object(prepare.PackageWorkspace, "write_package_file", fail_source):
            with self.assertRaises(WorkspaceError):
                prepare.prepare_probe(self.workspace, repository_root=self.root)
        self.assertEqual(list(self.workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
