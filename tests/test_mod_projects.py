"""Compiled mod packages preserve exact build provenance and complete bytes."""

import hashlib
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest

from sn2_mcp.mod_projects import ModProjectError, ModProjects, verify_artifact_package
from sn2_mcp.workspace import SourceWorkspace, WorkspaceError


ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def x64_dll(marker=b"revision-A"):
    raw = bytearray(2048)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\0\0"
    struct.pack_into("<H", raw, 68, 0x8664)
    struct.pack_into("<H", raw, 70, 1)
    struct.pack_into("<H", raw, 84, 240)
    struct.pack_into("<H", raw, 86, 0x2000)
    struct.pack_into("<H", raw, 88, 0x20B)
    struct.pack_into("<I", raw, 148, 1024)
    struct.pack_into("<I", raw, 196, 16)
    # Header-mapped synthetic import table with one required module.
    struct.pack_into("<II", raw, 208, 600, 40)
    struct.pack_into("<I", raw, 612, 650)
    raw[650:663] = b"KERNEL32.dll\0"
    # Command export in a real executable section, with header-mapped tables.
    struct.pack_into("<II", raw, 200, 700, 100)
    struct.pack_into("<IIIII", raw, 720, 1, 1, 820, 824, 828)
    struct.pack_into("<I", raw, 820, 0x1000)
    struct.pack_into("<I", raw, 824, 800)
    struct.pack_into("<H", raw, 828, 0)
    raw[800:814] = b"SN2ModCommand\0"
    struct.pack_into("<IIII", raw, 336, 1024, 0x1000, 1024, 1024)
    struct.pack_into("<I", raw, 364, 0x60000020)
    return bytes(raw) + marker


class ModProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repository = self.base / "bridge"
        self.workspace = self.base / "workspace"
        self.repository.mkdir()
        self.workspace.mkdir()
        template = self.repository / "development/mod_templates/widget_opacity"
        shutil.copytree(ROOT / "development/mod_templates/widget_opacity", template)
        self.service = ModProjects(SourceWorkspace(self.workspace), self.repository)

    def build_receipt(self, *, state="succeeded", exact=True, marker=b"revision-A"):
        project = self.service.create("mods/Opacity", "Opacity")
        staged = self.repository / "build/staged"
        shutil.copytree(self.workspace / "mods/Opacity", staged / "project")
        source_metadata = (staged / "project/mod.json").read_bytes()
        artifact = self.repository / "build/Release/Opacity.dll"
        artifact.parent.mkdir(parents=True)
        raw = x64_dll(marker)
        artifact.write_bytes(raw)
        inputs = [{"path": "project/mod.json", "sha256": sha(source_metadata),
                   "bytes": len(source_metadata), "role": "mod_input", "origin": "workspace"}]
        content = [{key: item[key] for key in ("path", "sha256", "bytes", "role")} for item in inputs]
        revision = sha(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        value = {"state": state, "exact_source_compiled": exact, "staged_source_root": str(staged),
                 "recipe": {"project_path": "mods/Opacity", "input_manifest_sha256": revision,
                            "inputs": inputs},
                 "artifacts": [{"target": "sn2_mod", "path": str(artifact), "sha256": sha(raw),
                                "bytes": len(raw), "authoritative_build_output": True,
                                "pe": {"format": "PE", "architecture": "x64", "machine": 0x8664, "kind": "dll"}}]}
        receipt = self.repository / "artifacts/builds/example/receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(value), encoding="utf-8")
        return receipt, value

    def test_project_in_separate_workspace_has_real_source_and_explicit_build_recipe(self):
        result = self.service.create("mods/Opacity", "Opacity")
        self.assertTrue(result["valid"])
        self.assertTrue(result["created"])
        self.assertFalse(result["compiled"])
        self.assertEqual(result["metadata"]["artifact"], "Opacity.dll")
        self.assertEqual(result["build_recipe"]["sdk_root"], str(self.repository))
        source = (self.workspace / "mods/Opacity/src/mod.cpp").read_text(encoding="utf-8")
        self.assertIn("constexpr float Opacity = 0.5f", source)
        self.assertIn("SN2ModSpecification", source)
        self.assertNotIn("GothicMod.json", result["metadata"]["sources"])
        with self.assertRaises(ModProjectError) as raised:
            self.service.create("mods/Opacity", "Opacity")
        self.assertEqual(raised.exception.code, "already_exists")

    def test_invalid_identity_and_parent_traversal_do_not_create_project(self):
        for identity in ("bad-name", "SN2_MCP", "CON", "../Escape"):
            with self.subTest(identity=identity), self.assertRaises((ModProjectError, WorkspaceError)):
                self.service.create("mods/Bad", identity)
        with self.assertRaises(WorkspaceError):
            self.service.create("../Escape", "Escape")
        self.assertFalse((self.base / "Escape").exists())

    def test_validation_detects_missing_required_input_and_wrong_cmake_composition(self):
        self.service.create("mods/Opacity", "Opacity")
        (self.workspace / "mods/Opacity/CMakeLists.txt").write_text("project(Other)", encoding="utf-8")
        # Fixture deliberately removes only its own source; no production cleanup.
        (self.workspace / "mods/Opacity/src/mod.cpp").unlink()
        result = self.service.validate("mods/Opacity")
        self.assertFalse(result["valid"])
        self.assertIn("cmake_contract_mismatch", {item["code"] for item in result["issues"]})
        self.assertIn("access_error", {item["code"] for item in result["issues"]})

    def test_package_contains_compiled_dll_and_staged_revision_even_after_source_edit(self):
        receipt, build = self.build_receipt()
        source = self.workspace / "mods/Opacity/src/mod.cpp"
        source.write_text(source.read_text(encoding="utf-8").replace("0.5f", "0.75f"), encoding="utf-8")
        result = self.service.package(str(receipt), "artifacts/Opacity-A")
        self.assertTrue(result["valid"])
        self.assertTrue(result["compiled"])
        self.assertFalse(result["installed"])
        self.assertEqual(result["source_revision"], build["recipe"]["input_manifest_sha256"])
        self.assertEqual(Path(result["artifact"]["path"]).read_bytes(), x64_dll())
        self.assertFalse(result["activation"]["requires_installation"])
        self.assertEqual(result["metadata"]["dependencies"][0]["module"], "KERNEL32.dll")
        self.assertFalse(result["metadata"]["dependencies"][0]["availability_checked"])
        checked = self.service.verify("artifacts/Opacity-A")
        self.assertEqual(checked["manifest_sha256"], result["manifest_sha256"])

    def test_changed_output_and_failed_build_are_never_packaged_as_new_revision(self):
        receipt, build = self.build_receipt(state="failed", exact=False)
        with self.assertRaises(ModProjectError) as raised:
            self.service.package(str(receipt), "artifacts/failure")
        self.assertEqual(raised.exception.code, "build_not_succeeded")
        build["state"] = "succeeded"
        build["exact_source_compiled"] = True
        receipt.write_text(json.dumps(build), encoding="utf-8")
        Path(build["artifacts"][0]["path"]).write_bytes(x64_dll(b"changed"))
        with self.assertRaises(ModProjectError) as raised:
            self.service.package(str(receipt), "artifacts/changed")
        self.assertEqual(raised.exception.code, "artifact_changed")
        self.assertFalse((self.workspace / "artifacts/changed").exists())

    def test_verifier_detects_corruption_missing_contract_and_unlisted_file(self):
        receipt, _ = self.build_receipt()
        result = self.service.package(str(receipt), "artifacts/Opacity-A")
        package = Path(result["package_root"])
        dll = Path(result["artifact"]["path"])
        original = dll.read_bytes()
        dll.write_bytes(original + b"corruption")
        with self.assertRaises(ModProjectError) as raised:
            verify_artifact_package(package)
        self.assertEqual(raised.exception.code, "package_changed")
        dll.write_bytes(original)
        activation = package / "activation.json"
        raw_activation = activation.read_bytes()
        activation.unlink()
        with self.assertRaises(WorkspaceError):
            verify_artifact_package(package)
        activation.write_bytes(raw_activation)
        (package / "unexpected.dll").write_bytes(b"extra")
        with self.assertRaises(ModProjectError) as raised:
            verify_artifact_package(package)
        self.assertEqual(raised.exception.code, "package_inventory_mismatch")

    def test_manipulated_success_claim_cannot_replace_artifact_identity(self):
        receipt, _ = self.build_receipt()
        result = self.service.package(str(receipt), "artifacts/Opacity-A")
        package = Path(result["package_root"])
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["build"]["receipt_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ModProjectError) as raised:
            verify_artifact_package(package)
        self.assertEqual(raised.exception.code, "build_provenance_mismatch")

    def test_optional_unavailable_pdb_is_recorded_but_not_a_required_package_file(self):
        receipt, build = self.build_receipt()
        optional = {"target": "sn2_mod", "path": str(self.repository / "build/Release/Opacity.pdb"),
                    "available": False, "required": False, "role": "debug_symbols"}
        build["artifacts"].append(optional)
        receipt.write_text(json.dumps(build), encoding="utf-8")
        result = self.service.package(str(receipt), "artifacts/Opacity-A")
        self.assertEqual(result["metadata"]["unavailable_optional_artifacts"], [optional])
        self.assertFalse((Path(result["package_root"]) / "bin/Opacity.pdb").exists())
        self.assertTrue(self.service.verify("artifacts/Opacity-A")["valid"])

    def test_malformed_nested_manifest_reports_attributable_validation_error(self):
        receipt, build = self.build_receipt()
        build["recipe"] = None
        receipt.write_text(json.dumps(build), encoding="utf-8")
        with self.assertRaises(ModProjectError) as raised:
            self.service.package(str(receipt), "artifacts/invalid")
        self.assertEqual(raised.exception.code, "invalid_manifest")

    def test_malformed_packaged_artifact_is_not_an_unhandled_attribute_error(self):
        receipt, _ = self.build_receipt()
        result = self.service.package(str(receipt), "artifacts/Opacity-A")
        package = Path(result["package_root"])
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["artifact"] = None
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ModProjectError) as raised:
            verify_artifact_package(package)
        self.assertEqual(raised.exception.code, "invalid_package")

    def test_bogus_manifest_revision_does_not_claim_exact_source_compilation(self):
        receipt, build = self.build_receipt()
        build["recipe"]["input_manifest_sha256"] = "0" * 64
        receipt.write_text(json.dumps(build), encoding="utf-8")
        with self.assertRaises(ModProjectError) as raised:
            self.service.package(str(receipt), "artifacts/bogus")
        self.assertEqual(raised.exception.code, "build_provenance_mismatch")

    def test_dll_without_direct_command_export_is_rejected_before_publication(self):
        receipt, build = self.build_receipt()
        artifact = Path(build["artifacts"][0]["path"])
        raw = bytearray(artifact.read_bytes())
        struct.pack_into("<II", raw, 200, 0, 0)
        artifact.write_bytes(raw)
        build["artifacts"][0]["sha256"] = sha(raw)
        receipt.write_text(json.dumps(build), encoding="utf-8")
        with self.assertRaises(ModProjectError) as raised:
            self.service.package(str(receipt), "artifacts/no-export")
        self.assertEqual(raised.exception.code, "mod_export_missing")
        self.assertFalse((self.workspace / "artifacts/no-export").exists())

    def test_commented_out_cmake_contract_is_not_structurally_valid(self):
        self.service.create("mods/Opacity", "Opacity")
        cmake = self.workspace / "mods/Opacity/CMakeLists.txt"
        text = cmake.read_text(encoding="utf-8")
        cmake.write_text("\n".join("# " + line for line in text.splitlines()), encoding="utf-8")
        validation = self.service.validate("mods/Opacity")
        self.assertFalse(validation["valid"])
        self.assertIn("cmake_contract_mismatch", {item["code"] for item in validation["issues"]})


if __name__ == "__main__":
    unittest.main()
