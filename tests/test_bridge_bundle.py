"""Переносимый внутренний комплект: закрытый whitelist и полные хеши."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[1] / "tools" / "prepare_bridge_bundle.py"
SPEC = importlib.util.spec_from_file_location("prepare_bridge_bundle", TOOL)
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def fake_pe(marker=b""):
    raw = bytearray(128)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\0\0"
    struct.pack_into("<H", raw, 68, 0x8664)
    return bytes(raw) + marker


class BridgeBundleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        (self.repository / "artifacts").mkdir()
        self.target = self.repository / "artifacts" / "bridge-bundle-dev7"
        self.version = "1.0.0-dev.7"
        self.write("src/g1r_mcp/__init__.py", f'__version__ = "{self.version}"\n'.encode())
        for name in bundle.SOURCE_FILES:
            if name != "__init__.py":
                self.write("src/g1r_mcp/" + name, b"# isolated source\n")
        for name in bundle.TOOLS:
            self.write("tools/" + name, b"# isolated helper\n")
        for name in bundle.HELPER_FILES:
            self.write(name, b"# isolated development helper\n")
        for name in bundle.DOCUMENTS:
            self.write(name, b"# isolated document\n")
        self.write("pyproject.toml", f'[project]\nversion = "{self.version}"\ndependencies=["mcp==2.3.0"]\n'.encode())
        self.write("uv.lock", f'[[package]]\nname="g1r-mcp"\nversion="{self.version.replace("-dev.", ".dev")}"\nsource={{editable="."}}\n[[package]]\nname="mcp"\nversion="2.3.0"\n'.encode())
        for name in bundle.FIXTURE_DIRS:
            self.write(f"tests/fixtures/{name}/GothicMod.json", b'{"name":"Probe"}\n')
            self.write(f"tests/fixtures/{name}/Script/{bundle.FIXTURE_SCRIPTS[name]}", b"class UProbe {}\n")
        self.write("tests/fixtures/patch_probe/ScriptPatches/Marker.patch", b"# isolated patch\n")
        self.write("native/src/game_access.cpp", b'"bindings_source","runtime_image_patterns"\n')
        self.write(bundle.BINARIES[0], fake_pe(self.version.encode() + b"runtime_image_patterns"))
        self.write(bundle.BINARIES[1], fake_pe())

    def write(self, path, raw):
        file = self.repository / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
        return file

    def prepare(self):
        return bundle.prepare_bundle(self.target, repository_root=self.repository)

    def test_bundle_preserves_layout_and_excludes_local_user_data(self):
        self.write(".venv/secret.txt", b"user environment")
        self.write("build/native/Release/native-tick.log", b"private log")
        self.write("artifacts/private.txt", b"user output")
        self.write("mods/UserMod/GothicMod.json", b"user mod")
        result = self.prepare()
        self.assertEqual(result["version"], self.version)
        self.assertEqual(result["binding_method"], "runtime_image_patterns")
        self.assertNotIn("supported_game_exe_sha256", result)
        self.assertFalse(result["game_launched"])
        self.assertFalse(result["installed"])
        self.assertTrue(result["verified_files"])
        self.assertTrue((self.target / "src/g1r_mcp/server.py").exists())
        self.assertTrue((self.target / bundle.BINARIES[0]).exists())
        for path in (".venv", "artifacts/private.txt", "mods/UserMod", "build/native/Release/native-tick.log"):
            self.assertFalse((self.target / path).exists())
        verified = bundle.verify_bundle(self.target)
        self.assertEqual(verified["file_count"], result["file_count"])
        manifest = json.loads((self.target / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["compatibility"]["native_version_marker"], self.version)
        self.assertEqual(manifest["compatibility"]["binding_method"], "runtime_image_patterns")
        self.assertNotIn("supported_game_exe_sha256", manifest["compatibility"])
        self.assertIn("not an installer or a public release", (self.target / "START.md").read_text(encoding="utf-8"))

        old_dll = fake_pe(self.version.encode() + b"a" * 64)
        (self.target / bundle.BINARIES[0]).write_bytes(old_dll)
        manifest["compatibility"].pop("binding_method")
        manifest["compatibility"]["supported_game_exe_sha256"] = "a" * 64
        entry = next(item for item in manifest["files"] if item["path"] == bundle.BINARIES[0])
        manifest["total_bytes"] += len(old_dll) - entry["bytes"]
        entry.update(bytes=len(old_dll), sha256=bundle._sha(old_dll))
        manifest_path = self.target / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        self.assertTrue(bundle.verify_bundle(self.target)["verified_files"])
        manifest["compatibility"]["supported_game_exe_sha256"] = "b" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "native_version_mismatch")

    def test_existing_outside_and_symlink_targets_refused(self):
        self.prepare()
        with self.assertRaises(bundle.BundleError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "already_exists")
        with self.assertRaises(bundle.BundleError):
            bundle.prepare_bundle(self.repository / "outside", repository_root=self.repository)
        foreign = self.repository / "foreign"
        foreign.mkdir()
        link = self.repository / "artifacts" / "redirect"
        try:
            link.symlink_to(foreign, target_is_directory=True)
        except OSError:
            self.skipTest("Система не разрешает isolated symlink")
        with self.assertRaises(bundle.WorkspaceError):
            bundle.prepare_bundle(link / "child", repository_root=self.repository)
        self.assertEqual(list(foreign.iterdir()), [])

    def test_version_binary_and_source_changes_refused(self):
        self.write("pyproject.toml", b'[project]\nversion="1.0.0-dev.6"\n')
        with self.assertRaises(bundle.BundleError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "version_mismatch")
        self.write("pyproject.toml", f'[project]\nversion="{self.version}"\ndependencies=["mcp==2.3.0"]\n'.encode())
        self.write(bundle.BINARIES[0], fake_pe(b"old version"))
        with self.assertRaises(bundle.BundleError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "native_version_mismatch")
        self.write(bundle.BINARIES[0], fake_pe(self.version.encode() + b"runtime_image_patterns"))
        self.write(bundle.BINARIES[1], b"not a PE")
        with self.assertRaises(bundle.BundleError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "invalid_binary")
        native_version = "1.0.0-dev.6"
        self.write(bundle.BINARIES[1], fake_pe())
        self.write(bundle.BINARIES[0], fake_pe(native_version.encode() + b"runtime_image_patterns"))
        result = bundle.prepare_bundle(self.target, repository_root=self.repository, native_version=native_version)
        self.assertEqual(result["version"], self.version)
        self.assertEqual(result["native_version"], native_version)
        self.assertEqual(bundle.verify_bundle(self.target)["native_version"], native_version)
        manifest_path = self.target / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["compatibility"]["native_version_marker"] = self.version
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "native_version_mismatch")

    def test_verification_rejects_changed_added_and_escape_manifest(self):
        self.prepare()
        source = self.target / "src/g1r_mcp/server.py"
        original = source.read_bytes()
        source.write_bytes(b"changed")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "integrity_mismatch")
        source.write_bytes(original)
        foreign = self.target / "Foreign.py"
        foreign.write_bytes(b"user source")
        with self.assertRaises(bundle.BundleError):
            bundle.verify_bundle(self.target)
        foreign.unlink()
        mf = self.target / "manifest.json"
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        manifest["files"][0]["path"] = "../outside"
        mf.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "invalid_manifest")

    def test_failed_copy_leaves_no_own_stage_or_partial_files(self):
        original = Path.open

        class Failure:
            def __init__(self, stream): self.stream = stream
            def __enter__(self): return self
            def __exit__(self, *args): self.stream.close()
            def fileno(self): return self.stream.fileno()
            def write(self, raw):
                self.stream.write(raw[:3]); self.stream.flush()
                raise OSError("isolated write failure")

        def failing_open(path, mode="r", *args, **kwargs):
            stream = original(path, mode, *args, **kwargs)
            if mode == "xb" and path.name == "README.md" and ".g1r-bridge-bundle-" in str(path):
                return Failure(stream)
            return stream

        with patch.object(Path, "open", failing_open):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(list((self.repository / "artifacts").iterdir()), [])

    def test_foreign_file_during_stage_not_published_or_removed(self):
        original = bundle._file_set
        injected = []

        def inject(root):
            if ".g1r-bridge-bundle-" in root.name and not injected:
                file = root / "Foreign.py"
                file.write_bytes(b"Foreign user data")
                injected.append(file)
            return original(root)

        with patch.object(bundle, "_file_set", inject):
            with self.assertRaises(bundle.BundleError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "copy_changed")
        self.assertFalse(self.target.exists())
        self.assertEqual(injected[0].read_bytes(), b"Foreign user data")

    def test_verification_requires_complete_runtime_and_version_lock(self):
        self.prepare()
        mf = self.target / "manifest.json"
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        manifest["version"] = "9.9.9-dev.1"
        mf.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "invalid_manifest")
        manifest["version"] = self.version
        removed = next(entry for entry in manifest["files"] if entry["path"] == "src/g1r_mcp/native_client.py")
        manifest["files"].remove(removed)
        manifest["file_count"] -= 1
        manifest["total_bytes"] -= removed["bytes"]
        (self.target / removed["path"]).unlink()
        mf.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.verify_bundle(self.target)
        self.assertEqual(captured.exception.code, "incomplete_bundle")
        self.write("uv.lock", b'[[package]]\nname="g1r-mcp"\nversion="wrong"\n')
        new_target = self.repository / "artifacts" / "bad-lock"
        with self.assertRaises(bundle.BundleError) as captured:
            bundle.prepare_bundle(new_target, repository_root=self.repository)
        self.assertEqual(captured.exception.code, "lock_mismatch")


class BridgeBundleDependencyTests(unittest.TestCase):
    DEPENDENCIES = (
        "src/g1r_mcp/process_observer.py",
        "development/helpers/G1RMCPDevelopmentHelpers/GothicMod.json",
        "development/helpers/G1RMCPDevelopmentHelpers/Script/G1RMCPDevelopmentHelpers.as",
    )

    def test_selected_dependencies_copy_exact_bytes_without_bundle_generation(self):
        repository = TOOL.parents[1]
        selected = bundle._enumerate(repository)
        self.assertTrue(set(self.DEPENDENCIES).issubset(selected))
        helper_prefix = "development/helpers/G1RMCPDevelopmentHelpers/"
        self.assertEqual({path for path in selected if path.startswith(helper_prefix)}, set(self.DEPENDENCIES[1:]))
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)
            for relative in self.DEPENDENCIES:
                raw = bundle._read(repository / relative, repository)
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(raw)
                copied = bundle._read(target, destination)
                self.assertEqual(copied, raw)
                self.assertEqual(bundle._sha(copied), bundle._sha(raw))
            self.assertEqual(bundle._file_set(destination), set(self.DEPENDENCIES))

    def test_verification_rejects_each_missing_dependency(self):
        repository = TOOL.parents[1]
        selected = set(bundle._enumerate(repository)) | {"START.md"}
        # Состав baseline моделируется в памяти: инструкции и полный комплект не создаются.
        contents = {path: b"baseline" for path in selected}
        contents.update({path: bundle._read(repository / path, repository) for path in self.DEPENDENCIES})

        def verify_with(omitted):
            files = {path: raw for path, raw in contents.items() if path != omitted}
            manifest = {"schema_version": 1, "kind": "internal_bridge_folder",
                        "files": [{"path": path, "bytes": len(raw), "sha256": bundle._sha(raw)}
                                  for path, raw in sorted(files.items())],
                        "file_count": len(files), "total_bytes": sum(len(raw) for raw in files.values())}
            directories = {"."}
            for path in files:
                directories.update(parent.as_posix() for parent in Path(path).parents if parent.as_posix() != ".")
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)

                def read(path, boundary):
                    relative = path.relative_to(root).as_posix()
                    return json.dumps(manifest).encode("utf-8") if relative == "manifest.json" else files[relative]

                with patch.object(bundle, "_read", side_effect=read), \
                     patch.object(bundle, "_file_set", return_value=set(files) | {"manifest.json"}), \
                     patch.object(bundle, "_directory_set", return_value=directories), \
                     patch.object(bundle, "_version", side_effect=RuntimeError("required dependency gate passed")):
                    return bundle.verify_bundle(root)

        with self.assertRaisesRegex(RuntimeError, "required dependency gate passed"):
            verify_with(None)
        for missing in self.DEPENDENCIES:
            with self.subTest(missing=missing), self.assertRaises(bundle.BundleError) as captured:
                verify_with(missing)
            self.assertEqual(captured.exception.code, "incomplete_bundle")


if __name__ == "__main__":
    unittest.main()
