"""Полная изолированная копия base Script без обращения к игре."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / "tools" / "prepare_base_patch_probe.py"
SPEC = importlib.util.spec_from_file_location("prepare_base_patch_probe", TOOL)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


class BasePatchProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        (self.repository / "artifacts").mkdir()
        self.modkit = self.repository / "Modkit"
        self.script = self.modkit / "Script"
        (self.script / "Subdir").mkdir(parents=True)
        (self.script / "Base.as").write_text("// Base\nclass UBase {}\n", encoding="utf-8")
        (self.script / "Subdir" / "README.md").write_text("Описание", encoding="utf-8")
        self.target = self.repository / "artifacts" / "base-patch"
        self.cache = self.repository / "Game" / "Script" / "Binds.Cache"
        self.cache.parent.mkdir(parents=True)
        self.cache.write_bytes(b"isolated current game bindings")
        self.cache_hash = hashlib.sha256(self.cache.read_bytes()).hexdigest()

    def prepare(self):
        return probe.prepare_probe(self.target, modkit_root=self.modkit, repository_root=self.repository,
                                   binds_cache=self.cache, expected_binds_sha256=self.cache_hash, probe_target="own_marker")

    def test_complete_copy_has_patch_only_package_and_unchanged_source(self):
        before = {path.relative_to(self.script).as_posix(): path.read_bytes() for path in self.script.rglob("*") if path.is_file()}
        result = self.prepare()
        for relative, raw in before.items():
            self.assertEqual((self.target / "Script" / relative).read_bytes(), raw)
            self.assertEqual((self.script / relative).read_bytes(), raw)
        self.assertEqual(result["source_file_count"], 2)
        self.assertEqual((self.target / "Script" / "Binds.Cache").read_bytes(), self.cache.read_bytes())
        self.assertEqual(result["binds_cache"]["sha256"], self.cache_hash)
        package = self.target / "Mods" / probe.PACKAGE_NAME
        self.assertFalse((package / "Script").exists())
        validation = probe.validate_package(self.target / "Mods", probe.PACKAGE_NAME)
        self.assertTrue(validation["valid_structure"])
        self.assertEqual(validation["script_count"], 0)
        self.assertEqual(validation["patch_count"], 1)
        self.assertIn("return 10;", (self.target / "Script" / probe.MARKER_NAME).read_text(encoding="utf-8"))
        self.assertIn("+        return 11;", (package / "ScriptPatches" / "Marker.patch").read_text(encoding="utf-8"))
        self.assertFalse(result["compiled"])
        self.assertFalse(result["patch_applied"])
        manifest = json.loads((self.target / "source-manifest.json").read_text(encoding="utf-8"))
        for item in manifest["source_files"]:
            self.assertEqual(item["sha256"], hashlib.sha256((self.target / "Script" / item["path"]).read_bytes()).hexdigest())
        self.assertEqual(result["script_directory"], str(self.target / "Script"))

    def test_existing_target_and_outside_target_refused(self):
        self.prepare()
        with self.assertRaises(probe.PackageError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "already_exists")
        with self.assertRaises(probe.PackageError) as captured:
            probe.prepare_probe(self.repository / "outside", modkit_root=self.modkit, repository_root=self.repository,
                                binds_cache=self.cache, expected_binds_sha256=self.cache_hash)
        self.assertEqual(captured.exception.code, "unsafe_path")
        with self.assertRaises(probe.WorkspaceError):
            probe.prepare_probe(self.repository / "artifacts" / "missing-parent" / "probe",
                                modkit_root=self.modkit, repository_root=self.repository,
                                binds_cache=self.cache, expected_binds_sha256=self.cache_hash)
        self.assertFalse((self.repository / "artifacts" / "missing-parent").exists())

    def test_source_change_during_copy_is_rejected_and_stage_removed(self):
        original = probe._read_bytes
        changed = False

        def read_then_change(file, boundary, limit):
            nonlocal changed
            result = original(file, boundary, limit)
            if Path(file) == self.script / "Base.as" and not changed:
                changed = True
                (self.script / "Base.as").write_text("changed", encoding="utf-8")
            return result

        with patch.object(probe, "_read_bytes", read_then_change):
            with self.assertRaises(probe.PackageError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "source_changed")
        self.assertFalse(self.target.exists())
        self.assertEqual(list((self.repository / "artifacts").iterdir()), [])

    def test_bounds_and_symlink_refused(self):
        external = self.repository / "outside.as"
        external.write_text("user source", encoding="utf-8")
        try:
            (self.script / "External.as").symlink_to(external)
        except OSError:
            self.skipTest("Система не разрешает isolated symlink")
        with self.assertRaises(probe.WorkspaceError):
            self.prepare()
        self.assertEqual(external.read_text(encoding="utf-8"), "user source")

    def test_foreign_plain_script_added_to_copy_is_refused_and_preserved(self):
        original = probe.PackageWorkspace.write_package_file
        injected = []

        def write_then_add(writer, package, file_path, content, expected_sha256=None):
            result = original(writer, package, file_path, content, expected_sha256)
            if file_path == "GothicMod.json":
                file = writer.root.parent / "Script" / "Foreign.as"
                file.write_bytes(b"Foreign user source")
                injected.append(file)
            return result

        with patch.object(probe.PackageWorkspace, "write_package_file", write_then_add):
            with self.assertRaises(probe.PackageError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "copy_changed")
        self.assertFalse(self.target.exists())
        self.assertEqual(injected[0].read_bytes(), b"Foreign user source")

    def test_partial_copy_failure_removes_only_created_partial_file(self):
        original = Path.open

        class FailingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                return self

            def __exit__(self, *arguments):
                self.stream.close()

            def fileno(self):
                return self.stream.fileno()

            def write(self, raw):
                self.stream.write(raw[:3])
                self.stream.flush()
                raise OSError("Isolated partial write failure")

        def failing_open(path, mode="r", *arguments, **options):
            stream = original(path, mode, *arguments, **options)
            if mode == "xb" and ".g1r-base-patch-" in str(path) and path.name == "Base.as":
                return FailingWriter(stream)
            return stream

        with patch.object(Path, "open", failing_open):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(list((self.repository / "artifacts").iterdir()), [])

    def test_source_walk_error_and_directory_limit_refuse_preparation(self):

        def denied_walk(path, *, followlinks, onerror):
            onerror(PermissionError("isolated blocked subdirectory"))
            return iter(())

        with patch.object(probe.os, "walk", denied_walk):
            with self.assertRaises(probe.PackageError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "access_error")

    def test_bind_cache_hash_required_and_changed_source_refused(self):
        with self.assertRaises(probe.PackageError) as captured:
            probe.prepare_probe(self.target, modkit_root=self.modkit, repository_root=self.repository,
                                binds_cache=self.cache, expected_binds_sha256="0" * 64)
        self.assertEqual(captured.exception.code, "cache_changed")

    def test_existing_attack_patch_changes_only_unique_original_body(self):
        source = self.modkit / probe.ATTACK_SOURCE_PATH
        source.parent.mkdir(parents=True)
        original = """class UCM_Attack : UObject
{
    UFUNCTION(BlueprintCallable)
    bool ShouldInvalidateAttack()
    {
        return false;
    }
    bool ShouldInvalidateAttack() override
    {
        return Super::ShouldInvalidateAttack();
    }
}
"""
        source.write_text(original, encoding="utf-8")
        patch_raw, expected = probe._patch_probe(self.modkit, "existing_attack")
        self.assertEqual(source.read_text(encoding="utf-8"), original)
        self.assertIn(b"-        return false;", patch_raw)
        self.assertIn(b"+        return true;", patch_raw)
        self.assertTrue(patch_raw.startswith(("diff --git a/" + probe.ATTACK_SOURCE_PATH).encode("utf-8")))
        self.assertIn(b"\nindex ", patch_raw)
        self.assertEqual(expected["patch_source_format"], "git_diff")
        self.assertEqual(expected["object_name"], "Default__CM_Attack")
        self.assertIs(expected["source_value"], False)
        self.assertIs(expected["patched_value"], True)
        self.assertEqual(expected["signature"]["return_type"], "BoolProperty")
        self.assertFalse(self.target.exists())
        self.cache.write_bytes(b"changed game bindings")
        with self.assertRaises(probe.PackageError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "cache_changed")


if __name__ == "__main__":
    unittest.main()
