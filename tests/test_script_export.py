"""ZIP официального пакета: точные байты и отсутствие установки."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from sn2_mcp import package_export as export


class ScriptExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.workspace = self.root / "workspace"
        (self.workspace / "artifacts").mkdir(parents=True)
        self.modkit = self.root / "Modkit"
        self.game = self.root / "Game"
        self.modkit.mkdir(); self.game.mkdir()
        self.package = self.workspace / "WorkingFolder"
        (self.package / "Script" / "Nested").mkdir(parents=True)
        (self.package / "ScriptPatches").mkdir()
        self.source = b"\xef\xbb\xbf// Original\r\nclass UProbe {}\r\n"
        (self.package / "Script" / "Nested" / "Probe.as").write_bytes(self.source)
        (self.package / "ScriptPatches" / "Example.patch").write_bytes(b"--- a/Script/X.as\n+++ b/Script/X.as\n")
        self.descriptor = json.dumps({"name": "ActualMod", "title": "Actual Mod", "author": "Test", "modVersion": "0.3.1"}).encode("utf-8")
        (self.package / "GothicMod.json").write_bytes(self.descriptor)
        self.target = self.workspace / "artifacts" / "export"

    def prepare(self, target=None):
        return export.export_package(self.workspace, "WorkingFolder", target or self.target,
                                     modkit_root=self.modkit, game_root=self.game)

    def test_exact_source_zip_root_and_determinism(self):
        result = self.prepare()
        self.assertFalse(result["compiled"])
        self.assertFalse(result["installed"])
        with zipfile.ZipFile(self.target / "ActualMod.zip") as archive:
            self.assertEqual(archive.read("ActualMod/Script/Nested/Probe.as"), self.source)
            self.assertEqual(archive.read("ActualMod/GothicMod.json"), self.descriptor)
            self.assertTrue(all(entry.date_time == (1980, 1, 1, 0, 0, 0) for entry in archive.infolist()))
        verified = export.verify_export(self.target)
        self.assertEqual(verified["package_name"], "ActualMod")
        self.assertEqual(verified["file_count"], 3)
        second = self.workspace / "artifacts" / "second"
        self.prepare(second)
        self.assertEqual((self.target / "ActualMod.zip").read_bytes(), (second / "ActualMod.zip").read_bytes())
        self.assertEqual((self.package / "Script/Nested/Probe.as").read_bytes(), self.source)

    def test_large_utf8_source_preserves_bytes_hashes_and_determinism(self):
        source = b"\xef\xbb\xbf" + "// Готика 😀\r\n".encode("utf-8") * 110_000 + b"class UProbe {}\r\n"
        self.assertGreater(len(source), 2 * 1024 * 1024)
        path = self.package / "Script/Nested/Probe.as"
        path.write_bytes(source)
        result = self.prepare()
        verified = export.verify_export(self.target)
        self.assertTrue(verified["verified_files"])
        manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["files"] if item["path"] == "Script/Nested/Probe.as")
        self.assertEqual(entry["bytes"], len(source))
        self.assertEqual(entry["sha256"], hashlib.sha256(source).hexdigest())
        with zipfile.ZipFile(self.target / "ActualMod.zip") as archive:
            self.assertEqual(archive.read("ActualMod/Script/Nested/Probe.as"), source)
        second = self.workspace / "artifacts/large-second"
        second_result = self.prepare(second)
        self.assertEqual(second_result["archive_sha256"], result["archive_sha256"])
        self.assertEqual((second / "ActualMod.zip").read_bytes(), (self.target / "ActualMod.zip").read_bytes())
        self.assertEqual(path.read_bytes(), source)

    def test_zip64_representation_uses_existing_archive_support(self):
        with patch.object(zipfile, "ZIP64_LIMIT", 16):
            result = self.prepare()
        self.assertTrue(export.verify_export(self.target)["verified_files"])
        with zipfile.ZipFile(self.target / "ActualMod.zip") as archive:
            self.assertEqual(archive.read("ActualMod/Script/Nested/Probe.as"), self.source)
            self.assertTrue(any(entry.extract_version == 45 for entry in archive.infolist()))
        self.assertFalse(result["compiled"])

    def test_official_components_and_non_source_files_preserve_bytes(self):
        components = {
            "Config/Nested/settings.json": b'\x00\xff{"setting":true}\r\n',
            "Content/Nested/resource.pak": b"\x00\x80\xff\x01binary resource",
            "Story/Nested/dialogue.bin": b"\xfe\xff\x00story\r\n",
            "ScriptPatches/Nested/Example.patch": b"\x00\xffnested patch bytes",
            "Script/Binds.Cache": b"\x00\xffuser cache\x80",
        }
        for relative, raw in components.items():
            path = self.package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        result = self.prepare()
        verified = export.verify_export(self.target)
        self.assertEqual(verified["file_count"], 3 + len(components))
        self.assertFalse(result["compiled"])
        self.assertFalse(result["installed"])
        with zipfile.ZipFile(self.target / "ActualMod.zip") as archive:
            for relative, raw in components.items():
                with self.subTest(path=relative):
                    self.assertEqual(archive.read("ActualMod/" + relative), raw)
                    self.assertEqual((self.package / relative).read_bytes(), raw)

    def test_protected_roots_existing_target_and_redirects_refused(self):
        self.prepare()
        with self.assertRaises(export.ExportError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "already_exists")
        with self.assertRaises(export.ExportError):
            self.prepare(self.workspace / "outside")
        protected = self.game / "WorkingFolder"
        protected.mkdir()
        (protected / "GothicMod.json").write_bytes(self.descriptor)
        with self.assertRaises(export.WorkspaceError):
            export.export_package(self.game, "WorkingFolder", self.game / "artifacts/export",
                                  modkit_root=self.modkit, game_root=self.game)
        outside = self.root / "outside.as"
        outside.write_bytes(b"user outside")
        linked = self.package / "Script" / "Linked.as"
        try:
            linked.symlink_to(outside)
        except OSError:
            self.skipTest("Система не разрешает isolated symlink")
        with self.assertRaises((export.WorkspaceError, export.PackageError)):
            self.prepare(self.workspace / "artifacts" / "redirected")
        self.assertEqual(outside.read_bytes(), b"user outside")

    def test_source_changed_between_reads_refused(self):
        original = export._source_files
        calls = 0

        def mutate(*arguments):
            nonlocal calls
            result = original(*arguments)
            calls += 1
            if calls == 1:
                (self.package / "Script" / "Foreign.as").write_bytes(b"foreign source")
            return result

        with patch.object(export, "_source_files", mutate):
            with self.assertRaises(export.ExportError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "source_changed")
        self.assertFalse(self.target.exists())

    def test_verify_hash_paths_and_foreign_files(self):
        self.prepare()
        archive = self.target / "ActualMod.zip"
        original = archive.read_bytes()
        archive.write_bytes(original + b"tampered")
        with self.assertRaises(export.ExportError) as captured:
            export.verify_export(self.target)
        self.assertEqual(captured.exception.code, "integrity_mismatch")
        archive.write_bytes(original)
        foreign = self.target / "Foreign.txt"
        foreign.write_bytes(b"foreign output")
        with self.assertRaises(export.ExportError):
            export.verify_export(self.target)
        foreign.unlink()
        mf = self.target / "manifest.json"
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../Escape.as"
        mf.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(export.WorkspaceError):
            export.verify_export(self.target)

    def test_partial_write_failure_cleans_own_output(self):
        original = Path.open

        class Failure:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def write(self, raw):
                self.stream.write(raw[:10]); self.stream.flush()
                raise OSError("isolated write failure")

        def fail(path, mode="r", *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            if mode == "xb" and path.name == "manifest.json" and ".g1r-script-export-" in str(path):
                return Failure(stream)
            return stream

        with patch.object(Path, "open", fail):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(list((self.workspace / "artifacts").iterdir()), [])

    def test_foreign_file_added_to_stage_preserved_not_published(self):
        original = export.verify_export
        injected = []

        def add(root):
            file = root / "Foreign.txt"
            file.write_bytes(b"foreign user output")
            injected.append(file)
            return original(root)

        with patch.object(export, "verify_export", add):
            with self.assertRaises(export.ExportError):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(injected[0].read_bytes(), b"foreign user output")

    def test_archive_verifier_refuses_duplicate_link_and_false_runtime_claims(self):
        self.prepare()
        root_manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        root_manifest["compiled"] = True
        (self.target / "manifest.json").write_text(json.dumps(root_manifest), encoding="utf-8")
        with self.assertRaises(export.ExportError) as captured:
            export.verify_export(self.target)
        self.assertEqual(captured.exception.code, "invalid_manifest")
        root_manifest["compiled"] = False
        archive_path = self.target / "ActualMod.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
            entry = zipfile.ZipInfo("ActualMod/GothicMod.json")
            entry.create_system = 3
            entry.external_attr = (0o120777 << 16)
            archive.writestr(entry, self.descriptor)
            archive.writestr("ActualMod/Script/Nested/Probe.as", self.source)
            archive.writestr("ActualMod/ScriptPatches/Example.patch", b"--- a/Script/X.as\n+++ b/Script/X.as\n")
        archive_raw = archive_path.read_bytes()
        root_manifest["archive"]["bytes"] = len(archive_raw)
        root_manifest["archive"]["sha256"] = hashlib.sha256(archive_raw).hexdigest()
        (self.target / "manifest.json").write_text(json.dumps(root_manifest), encoding="utf-8")
        with self.assertRaises(export.ExportError) as captured:
            export.verify_export(self.target)
        self.assertEqual(captured.exception.code, "unsafe_archive")

    def test_descriptor_contract_rejects_duplicates_nonfinite_and_bad_dependencies(self):
        for descriptor in (b'{"name":"A","name":"B"}',
                           b'{"name":"A","title":"B","author":"C","modVersion":"1","loadOrder":false}',
                           b'{"name":"A","title":"B","author":"C","modVersion":"1","dependencies":[{}]}',
                           b'{"name":"A","title":"B","author":"C","modVersion":"1","unknown":NaN}'):
            with self.subTest(descriptor=descriptor), self.assertRaises(export.ExportError):
                export._descriptor_metadata(descriptor)


if __name__ == "__main__":
    unittest.main()
