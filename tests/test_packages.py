"""Изолированные проверки официальных пакетов без игры и модификации файлов."""

import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1r_mcp.packages import PackageError, validate_package


class PackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        self.package = self.workspace / "Example"
        self.package.mkdir()
        self.descriptor = self.package / "GothicMod.json"

    def write_descriptor(self, **changes):
        value = {"name": "Example", "title": "Пример", "author": "Автор", "modVersion": "1.0.0"}
        value.update(changes)
        self.descriptor.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def validate(self):
        return validate_package(self.workspace, "Example")

    def test_script_package_is_valid_without_ue4ss_entrypoint(self):
        self.write_descriptor()
        script = self.package / "Script" / "Nested"
        script.mkdir(parents=True)
        (script / "Anything.as").write_text("class UExample {}", encoding="utf-8")
        result = self.validate()
        self.assertTrue(result["valid_json"])
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["script_count"], 1)
        self.assertFalse(result["compiled"])
        self.assertEqual(result["runtime_validation"], "not_performed")
        self.assertEqual(result["metadata"]["title"], "Пример")

    def test_content_only_and_workspace_root_package_are_supported(self):
        self.write_descriptor()
        (self.package / "Content").mkdir()
        result = validate_package(self.package, ".")
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["package_path"], ".")
        self.assertEqual(result["script_count"], 0)
        self.assertIn("no_scripts", {item["code"] for item in result["warnings"]})

    def test_patch_only_package_is_structural_without_claiming_patch_application(self):
        self.write_descriptor()
        directory = self.package / "ScriptPatches"
        directory.mkdir()
        # Локальные fixtures не устанавливают грамматику игрового patch parser.
        (directory / "Changes.patch").write_text("--- a/Script/Test.as\n+++ b/Script/Test.as\n@@ -1 +1 @@\n-old\n+new\n", encoding="utf-8")
        (directory / "Opaque.patch").write_bytes(b"\xff invalid diff bytes are not parsed")
        (directory / "Ignored.json").write_text("{}", encoding="utf-8")
        nested = directory / "nested"
        nested.mkdir()
        (nested / "Nested.patch").write_text("not a verified patch", encoding="utf-8")
        before = {path: path.read_bytes() for path in directory.rglob("*") if path.is_file()}
        result = self.validate()
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["script_count"], 0)
        self.assertEqual(result["patch_count"], 2)
        self.assertEqual(result["patch_validation"], "not_performed")
        self.assertEqual(result["runtime_validation"], "not_performed")
        self.assertNotIn("no_scripts", {item["code"] for item in result["warnings"]})
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertNotIn("invalid diff bytes", repr(result))

    def test_patch_file_symlink_is_rejected(self):
        self.write_descriptor()
        directory = self.package / "ScriptPatches"
        directory.mkdir()
        external = self.workspace / "Outside.patch"
        external.write_text("private", encoding="utf-8")
        link = directory / "Linked.patch"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("Система не разрешает изолированный symlink")
        with self.assertRaises(PackageError) as captured:
            self.validate()
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_development_version_is_string_but_loader_is_unverified(self):
        self.write_descriptor(modVersion="1.0.0-dev.1")
        result = self.validate()
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["metadata"]["modVersion"], "1.0.0-dev.1")
        self.assertIn("runtime_not_verified", {item["code"] for item in result["warnings"]})

    def test_missing_descriptor_and_invalid_json_are_structured_results(self):
        missing = self.validate()
        self.assertFalse(missing["valid_structure"])
        self.assertEqual(missing["errors"][0]["code"], "descriptor_missing")
        for raw in (b"{invalid", b"\xff", b'{"name":"a","name":"b"}', b'{"unknown":NaN}', b'{"unknown":Infinity}'):
            self.descriptor.write_bytes(raw)
            result = self.validate()
            self.assertFalse(result["valid_json"])
            self.assertEqual(result["errors"][0]["code"], "invalid_json")

    def test_json_top_level_and_descriptor_size_are_checked(self):
        self.descriptor.write_text("[]", encoding="utf-8")
        result = self.validate()
        self.assertTrue(result["valid_json"])
        self.assertFalse(result["valid_structure"])
        self.assertEqual(result["errors"][0]["code"], "descriptor_not_object")
        description = "x" * (256 * 1024 + 1)
        self.write_descriptor(description=description)
        result = self.validate()
        self.assertTrue(result["valid_json"])
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["metadata"]["description"], description)

    def test_mandatory_fields_and_load_order_are_checked(self):
        self.write_descriptor(name="", title=None, author=7, modVersion=" ", loadOrder="unknown")
        result = self.validate()
        self.assertFalse(result["valid_structure"])
        self.assertEqual({item["field"] for item in result["errors"]}, {"name", "title", "author", "modVersion", "loadOrder"})

    def test_dependencies_preserve_constraints_without_runtime_parse_claim(self):
        dependencies = [{"name": "G1R", "requiredVersion": ">=1.1, <2.0"}, {"name": "Other", "requiredVersion": ""}]
        self.write_descriptor(dependencies=dependencies, loadOrder="early")
        result = self.validate()
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["metadata"]["dependencies"], dependencies)
        for bad in ({"name": "G1R"}, [None], [{"name": "", "requiredVersion": "*"}], [{"name": "Other", "requiredVersion": 5}]):
            self.write_descriptor(dependencies=bad)
            self.assertFalse(self.validate()["valid_structure"])

    def test_unknown_fields_warn_without_rejecting_package(self):
        self.write_descriptor(customFutureField={"anything": True}, dependencies=[{"name": "Other", "requiredVersion": "*", "future": 1}])
        result = self.validate()
        self.assertTrue(result["valid_structure"])
        warnings = {item.get("field") for item in result["warnings"] if item["code"] == "unknown_field"}
        self.assertEqual(warnings, {"customFutureField", "dependencies[0].future"})
        self.assertNotIn("customFutureField", result["metadata"])

    def test_absolute_parent_and_alternate_stream_paths_are_rejected(self):
        for path in (str(self.package.resolve()), "../Example", "Example/../Example", "C:\\outside", "Example:private", "\\\\server\\share"):
            with self.subTest(path=path):
                with self.assertRaises(PackageError) as captured:
                    validate_package(self.workspace, path)
                self.assertEqual(captured.exception.code, "unsafe_path")

    def test_symlink_descriptor_and_script_directory_are_rejected(self):
        external = self.workspace / "external"
        external.mkdir()
        (external / "GothicMod.json").write_text("{}", encoding="utf-8")
        try:
            self.descriptor.symlink_to(external / "GothicMod.json")
        except OSError:
            self.skipTest("Система не разрешает изолированный symlink")
        with self.assertRaises(PackageError) as captured:
            self.validate()
        self.assertEqual(captured.exception.code, "unsafe_path")
        self.descriptor.unlink()
        self.write_descriptor()
        script = self.package / "Script"
        script.mkdir()
        (script / "linked").symlink_to(external, target_is_directory=True)
        with self.assertRaises(PackageError) as captured:
            self.validate()
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_known_component_file_is_structure_error(self):
        self.write_descriptor()
        (self.package / "Config").write_bytes(b"not-a-directory")
        result = self.validate()
        self.assertFalse(result["valid_structure"])
        self.assertIn("component_not_directory", {item["code"] for item in result["errors"]})

    def test_validation_does_not_modify_or_read_non_script_content(self):
        self.write_descriptor()
        script = self.package / "Script"
        script.mkdir()
        (script / "Example.as").write_bytes(b"not parsed as AngelScript")
        content = self.package / "Content"
        content.mkdir()
        (content / "private.dat").write_bytes(b"private-content")
        before = {file: file.read_bytes() for file in self.package.rglob("*") if file.is_file()}
        result = self.validate()
        self.assertTrue(result["valid_structure"])
        self.assertNotIn("private-content", repr(result))
        self.assertEqual(before, {file: file.read_bytes() for file in before})


if __name__ == "__main__":
    unittest.main()
