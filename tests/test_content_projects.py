"""Content project and package contracts; fixtures are not cooker acceptance."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from sn2_mcp.content_projects import (
    COOK_RECEIPT_KIND, RUNTIME_BUILD_RECEIPT_KIND, ContentProjectError, ContentProjects,
    source_manifest_sha256, verify_content_package,
)
from sn2_mcp.workspace import SourceWorkspace, WorkspaceError


ROOT = Path(__file__).resolve().parents[1]


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class ContentProjectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.repository = self.base / "bridge"
        self.workspace = self.base / "authoring"
        self.repository.mkdir()
        self.workspace.mkdir()
        shutil.copytree(ROOT / "development/mod_templates/content_plugin",
                        self.repository / "development/mod_templates/content_plugin")
        self.service = ContentProjects(SourceWorkspace(self.workspace), self.repository)

    def _write_receipt(self, path, receipt):
        path.write_text(json.dumps(receipt), encoding="utf-8")

    def cook_receipt(self):
        """Prepare structural provenance fixtures without emulating compilation."""
        self.service.create("mods/TestMod", "TestMod", "Testing content")
        project = self.workspace / "mods/TestMod"
        asset = project / "Content/Blueprints/BP_Entry.uasset"
        asset.parent.mkdir()
        asset.write_bytes(b"authored asset fixture, not a compiled Blueprint")
        metadata_path = project / "mod.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["entry_class"] = "/TestMod/Blueprints/BP_Entry.BP_Entry_C"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        validated = self.service.validate("mods/TestMod")
        self.assertTrue(validated["valid"])
        capture = self.repository / "build/source/Subnautica2/Plugins/TestMod"
        shutil.copytree(project, capture)
        inputs = [{**item, "source_path": str(capture / item["path"]),
                   "origin_path": str(project / item["path"])} for item in validated["files"]]
        dependencies = [{"path": "modkit/Subnautica2.uproject", "sha256": sha(b"fixture"),
                         "bytes": len(b"fixture"), "role": "modkit_project"}]
        revision = source_manifest_sha256(inputs)
        dep_revision = source_manifest_sha256(dependencies)
        output_root = self.repository / "build/output/TestMod"
        containers = output_root / "Content/Paks/Windows"
        containers.mkdir(parents=True)
        (output_root / "TestMod.uplugin").write_bytes((capture / "TestMod.uplugin").read_bytes())
        outputs = []
        for suffix in ("pak", "utoc", "ucas"):
            (containers / ("TestMod-Windows." + suffix)).write_bytes(("cooker output fixture " + suffix).encode())
        for path in sorted(output_root.rglob("*")):
            if path.is_file():
                raw = path.read_bytes()
                outputs.append({"path": str(path), "relative_path": path.relative_to(output_root).as_posix(),
                                "sha256": sha(raw), "bytes": len(raw),
                                "role": "plugin_descriptor" if path.suffix == ".uplugin" else path.suffix[1:],
                                "authoritative_cook_output": True})
        receipt = {"kind": COOK_RECEIPT_KIND, "state": "succeeded", "plugin_id": "TestMod",
                   "project_path": "mods/TestMod", "exact_source_compiled": True,
                   "workspace_current_revision_compiled": True,
                   "source_manifest_sha256": revision, "inputs": inputs,
                   "shared_dependencies": dependencies, "dependency_manifest_sha256": dep_revision,
                   "staged_source_root": str(capture), "output_root": str(output_root), "outputs": outputs,
                   "build_identity": {"build_id": "fixture-build", "source_manifest_sha256": revision,
                                      "dependency_manifest_sha256": dep_revision, "engine_version": "5.6.1",
                                      "modkit_head": "fixture", "target_platform": "Win64",
                                      "configuration": "Shipping", "base_release": "SN2Base"}}
        receipt_path = self.repository / "build/receipt.json"
        self._write_receipt(receipt_path, receipt)
        return receipt_path, receipt

    def runtime_receipt(self):
        """Publisher fixtures identify source and containers, not game execution."""
        path, cooked = self.cook_receipt()
        captured = Path(cooked["staged_source_root"])
        (captured / "Content/Blueprints/BP_Entry.uasset").unlink()
        spec = captured / "Authoring/specification.json"
        spec.parent.mkdir()
        spec.write_text('{"schema":1,"plugin_id":"TestMod","assets":[{"path":"/TestMod/Blueprints/BP_Entry.BP_Entry"}]}', encoding="utf-8")
        inputs = []
        for item in cooked["inputs"]:
            if item["role"] != "authored_asset":
                inputs.append(item)
        raw = spec.read_bytes()
        inputs.append({"path": "Authoring/specification.json", "sha256": sha(raw), "bytes": len(raw),
                       "role": "authoring_source", "source_path": str(spec)})
        inputs.sort(key=lambda item: item["path"])
        revision = source_manifest_sha256(inputs)
        outputs = [{**item, "authoritative_runtime_output": True} for item in cooked["outputs"]]
        for item in outputs:
            item.pop("authoritative_cook_output")
        receipt = {"kind": RUNTIME_BUILD_RECEIPT_KIND, "schema": 1, "method": "runtime_blueprint",
                   "state": "succeeded", "compiled": True, "persisted": True, "containerized": True,
                   "exact_source_compiled": True,
                   "plugin_id": "TestMod", "project_path": "mods/TestMod", "inputs": inputs,
                   "source_manifest_sha256": revision, "staged_source_root": str(captured),
                   "specification_sha256": sha(raw),
                   "compiler_specification": {"source_path": str(spec), "sha256": sha(raw), "bytes": len(raw)},
                   "output_root": cooked["output_root"], "outputs": outputs,
                   "generated_class_paths": ["/TestMod/Blueprints/BP_Entry.BP_Entry_C"],
                   "compiled_asset_paths": ["/TestMod/Blueprints/BP_Entry.BP_Entry"],
                   "build_identity": {"build_id": "runtime-fixture", "source_manifest_sha256": revision,
                                      "specification_sha256": sha(raw),
                                      "target_platform": "Win64", "configuration": "Shipping"},
                   "runtime_provenance": {"fixture": True, "game_execution_verified": False}}
        self._write_receipt(path, receipt)
        return path, receipt

    def test_runtime_package_accepts_json_authored_source_and_retains_actual_producer(self):
        path, receipt = self.runtime_receipt()
        result = self.service.runtime_package(str(path), "artifacts/Runtime-A")
        self.assertTrue(result["valid"])
        self.assertEqual(result["metadata"]["build"]["method"], "runtime_blueprint")
        self.assertEqual(result["source_revision"], receipt["source_manifest_sha256"])
        self.assertEqual(result["container_validation"], "identified_runtime_outputs_not_fresh_load_verification")
        self.assertFalse(result["installed"])
        self.assertEqual(result["runtime_validation"], "not_performed")
        package = Path(result["package_root"])
        self.assertTrue((package / "runtime-build-receipt.json").is_file())
        self.assertFalse((package / "cook-receipt.json").exists())
        self.assertEqual(json.loads((package / "runtime-build-receipt.json").read_text()), receipt)
        self.assertEqual((package / "compiler-specification.json").read_bytes(),
                         Path(receipt["compiler_specification"]["source_path"]).read_bytes())
        self.assertEqual(result["specification_sha256"], receipt["specification_sha256"])
        self.assertTrue(self.service.verify("artifacts/Runtime-A")["valid"])

    def test_raw_runtime_pair_is_rejected_and_prior_package_survives(self):
        path, receipt = self.runtime_receipt()
        prior = self.service.runtime_package(str(path), "artifacts/Runtime-A")
        manifest = (Path(prior["package_root"]) / "manifest.json").read_bytes()
        receipt.update(state="serialized_raw_package", containerized=False)
        self._write_receipt(path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-B")
        self.assertEqual(raised.exception.code, "containerization_not_completed")
        self.assertFalse((self.workspace / "artifacts/Runtime-B").exists())
        self.assertEqual((Path(prior["package_root"]) / "manifest.json").read_bytes(), manifest)

    def test_runtime_source_or_payload_changes_do_not_publish(self):
        path, receipt = self.runtime_receipt()
        output = next(item for item in receipt["outputs"] if item["role"] == "utoc")
        Path(output["path"]).write_bytes(b"changed native output")
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-changed")
        self.assertEqual(raised.exception.code, "runtime_output_changed")
        self.assertFalse((self.workspace / "artifacts/Runtime-changed").exists())

    def test_runtime_build_requires_exact_source_and_matching_compiled_assets(self):
        path, receipt = self.runtime_receipt()
        for proof in (False, None):
            with self.subTest(exact_source_compiled=proof):
                receipt["exact_source_compiled"] = proof
                self._write_receipt(path, receipt)
                with self.assertRaises(ContentProjectError) as raised:
                    self.service.runtime_package(str(path), "artifacts/Runtime-unverified")
                self.assertEqual(raised.exception.code, "runtime_build_not_succeeded")
                self.assertFalse((self.workspace / "artifacts/Runtime-unverified").exists())
        receipt["exact_source_compiled"] = True
        receipt["compiled_asset_paths"] = ["/TestMod/Other.Other"]
        self._write_receipt(path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-mismatched-assets")
        self.assertEqual(raised.exception.code, "runtime_build_provenance_mismatch")
        self.assertFalse((self.workspace / "artifacts/Runtime-mismatched-assets").exists())

    def test_runtime_build_requires_absolute_identified_output_paths(self):
        path, receipt = self.runtime_receipt()
        output = receipt["outputs"][0]
        output["path"] = output["relative_path"]
        self._write_receipt(path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-relative-output")
        self.assertEqual(raised.exception.code, "invalid_runtime_build_receipt")
        self.assertFalse((self.workspace / "artifacts/Runtime-relative-output").exists())

    def test_runtime_entry_requires_compiled_class_and_complete_triplet(self):
        path, receipt = self.runtime_receipt()
        receipt["generated_class_paths"] = ["/TestMod/Other.Other_C"]
        receipt["compiled_asset_paths"] = ["/TestMod/Other.Other"]
        self._write_receipt(path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-wrong-entry")
        self.assertEqual(raised.exception.code, "source_provenance_mismatch")
        receipt["generated_class_paths"] = ["/TestMod/Blueprints/BP_Entry.BP_Entry_C"]
        receipt["compiled_asset_paths"] = ["/TestMod/Blueprints/BP_Entry.BP_Entry"]
        missing = next(item for item in receipt["outputs"] if item["role"] == "ucas")
        receipt["outputs"].remove(missing)
        Path(missing["path"]).unlink()
        self._write_receipt(path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-incomplete")
        self.assertEqual(raised.exception.code, "containerization_not_completed")

    def test_runtime_verifier_rejects_changed_producer_identity(self):
        path, _ = self.runtime_receipt()
        result = self.service.runtime_package(str(path), "artifacts/Runtime-A")
        manifest_path = Path(result["package_root"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["build"]["identity"]["build_id"] = "other-runtime-build"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ContentProjectError) as raised:
            verify_content_package(Path(result["package_root"]))
        self.assertEqual(raised.exception.code, "runtime_build_provenance_mismatch")

    def test_external_compiler_specification_is_preserved_and_identifies_graph_revision(self):
        path, receipt = self.runtime_receipt()
        captured = Path(receipt["staged_source_root"])
        original_specification = Path(receipt["compiler_specification"]["source_path"])
        raw = original_specification.read_bytes()
        original_specification.unlink()
        receipt["inputs"] = [item for item in receipt["inputs"] if item["path"] != "Authoring/specification.json"]
        project_revision = source_manifest_sha256(receipt["inputs"])
        receipt["source_manifest_sha256"] = project_revision
        receipt["build_identity"]["source_manifest_sha256"] = project_revision
        external = self.repository / "build/external-specification.json"
        external.write_bytes(raw)
        receipt["compiler_specification"]["source_path"] = str(external)
        self._write_receipt(path, receipt)
        first = self.service.runtime_package(str(path), "artifacts/Runtime-external-A")
        changed = json.loads(raw)
        changed["assets"][0]["variables"] = [{"name": "Count", "type": "int", "default": 7}]
        changed_raw = json.dumps(changed).encode("utf-8")
        external.write_bytes(changed_raw)
        receipt["compiler_specification"].update(sha256=sha(changed_raw), bytes=len(changed_raw))
        receipt["specification_sha256"] = sha(changed_raw)
        receipt["build_identity"]["specification_sha256"] = sha(changed_raw)
        self._write_receipt(path, receipt)
        second = self.service.runtime_package(str(path), "artifacts/Runtime-external-B")
        self.assertEqual(first["source_revision"], second["source_revision"])
        self.assertNotEqual(first["specification_sha256"], second["specification_sha256"])
        self.assertEqual((Path(first["package_root"]) / "compiler-specification.json").read_bytes(), raw)
        self.assertEqual((Path(second["package_root"]) / "compiler-specification.json").read_bytes(), changed_raw)
        shutil.rmtree(captured)
        external.unlink()
        self.assertTrue(self.service.verify("artifacts/Runtime-external-A")["valid"])
        self.assertTrue(self.service.verify("artifacts/Runtime-external-B")["valid"])

    def test_compiler_specification_changes_and_reidentified_package_bytes_are_rejected(self):
        path, receipt = self.runtime_receipt()
        specification = Path(receipt["compiler_specification"]["source_path"])
        external = self.repository / "build/captured-specification.json"
        external.write_bytes(specification.read_bytes())
        receipt["compiler_specification"]["source_path"] = str(external)
        self._write_receipt(path, receipt)
        result = self.service.runtime_package(str(path), "artifacts/Runtime-specification-A")
        external.write_bytes(b'{"schema":1,"plugin_id":"TestMod","assets":[]}')
        with self.assertRaises(ContentProjectError) as raised:
            self.service.runtime_package(str(path), "artifacts/Runtime-specification-changed")
        self.assertEqual(raised.exception.code, "compiler_specification_changed")
        self.assertFalse((self.workspace / "artifacts/Runtime-specification-changed").exists())
        package = Path(result["package_root"])
        modified_raw = external.read_bytes()
        (package / "compiler-specification.json").write_bytes(modified_raw)
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        entry = next(item for item in manifest["files"] if item["path"] == "compiler-specification.json")
        entry.update(sha256=sha(modified_raw), bytes=len(modified_raw))
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ContentProjectError) as raised:
            self.service.verify("artifacts/Runtime-specification-A")
        self.assertEqual(raised.exception.code, "compiler_specification_changed")

    def test_new_project_is_editable_content_plugin_and_claims_no_authored_or_compiled_assets(self):
        result = self.service.create("mods/MyContent", "MyContent", 'A title with "quotes"')
        self.assertTrue(result["valid"])
        self.assertTrue(result["created"])
        self.assertTrue(result["authoring_required"])
        self.assertFalse(result["authored_assets_present"])
        self.assertFalse(result["compiled"])
        self.assertEqual(result["descriptor"]["FriendlyName"], 'A title with "quotes"')
        self.assertTrue(result["descriptor"]["CanContainContent"])
        self.assertNotIn("Modules", result["descriptor"])
        self.assertTrue((self.workspace / "mods/MyContent/Content").is_dir())
        self.assertEqual(result["project_source_manifest_sha256"], source_manifest_sha256(result["files"]))
        with self.assertRaises(ContentProjectError) as raised:
            self.service.create("mods/MyContent", "MyContent")
        self.assertEqual(raised.exception.code, "already_exists")

    def test_invalid_path_identity_or_class_cannot_create_project(self):
        for identity in ("../Escape", "9Mod", "bad-name", "CON"):
            with self.subTest(identity=identity), self.assertRaises((WorkspaceError, ContentProjectError)):
                self.service.create("mods/Invalid", identity)
        for entry in ("/Game/BP.BP_C", "/TestMod/BP.BP", "/TestMod/BP.BP_C:Child", "Blueprint'/TestMod/BP.BP_C'", "/TestMod/../BP.BP_C"):
            with self.subTest(entry=entry), self.assertRaises(ContentProjectError):
                self.service.create("mods/Invalid", "TestMod", entry_class=entry)
        with self.assertRaises(WorkspaceError):
            self.service.create("../Escape", "Escape")
        self.assertFalse((self.base / "Escape").exists())

    def test_declared_entry_requires_real_authored_asset_and_modules_are_rejected(self):
        result = self.service.create("mods/Blueprint", "Blueprint", entry_class="/Blueprint/BP_Entry.BP_Entry_C")
        self.assertFalse(result["valid"])
        self.assertFalse(result["entry_asset_present"])
        self.assertIn("entry_asset_missing", {item["code"] for item in result["issues"]})
        project = self.workspace / "mods/Blueprint"
        (project / "Content/BP_Entry.uasset").write_bytes(b"actual source fixture")
        descriptor_path = project / "Blueprint.uplugin"
        descriptor = json.loads(descriptor_path.read_text(encoding="utf-8"))
        descriptor["Modules"] = []
        descriptor_path.write_text(json.dumps(descriptor), encoding="utf-8")
        result = self.service.validate("mods/Blueprint")
        self.assertTrue(result["entry_asset_present"])
        self.assertIn("native_modules_unsupported", {item["code"] for item in result["issues"]})

    def test_entry_class_retains_valid_unicode_package_and_asset_names(self):
        self.service.create("mods/Unicode", "Unicode", entry_class="/Unicode/Objects/BP_珊瑚.BP_珊瑚_C")
        asset = self.workspace / "mods/Unicode/Content/Objects/BP_珊瑚.uasset"
        asset.parent.mkdir()
        asset.write_bytes(b"authored Unicode fixture")
        result = self.service.validate("mods/Unicode")
        self.assertTrue(result["valid"])
        self.assertTrue(result["entry_asset_present"])
        self.assertEqual(result["entry_class"], "/Unicode/Objects/BP_珊瑚.BP_珊瑚_C")

    def test_full_source_inventory_tracks_assets_and_excludes_editor_generated_files(self):
        self.service.create("mods/Inventory", "Inventory")
        project = self.workspace / "mods/Inventory"
        (project / "Content/Data.uasset").write_bytes(b"data")
        (project / "Config").mkdir()
        (project / "Config/DefaultInventory.ini").write_bytes(b"setting=true")
        (project / "Saved").mkdir()
        (project / "Saved/cache.bin").write_bytes(b"generated")
        result = self.service.validate("mods/Inventory")
        self.assertTrue(result["valid"])
        files = {item["path"]: item for item in result["files"]}
        self.assertEqual(files["Content/Data.uasset"]["sha256"], sha(b"data"))
        self.assertEqual(files["Config/DefaultInventory.ini"]["role"], "authoring_source")
        self.assertNotIn("Saved/cache.bin", files)

    def test_package_binds_complete_captured_revision_and_does_not_install(self):
        receipt_path, receipt = self.cook_receipt()
        (self.workspace / "mods/TestMod/Content/Blueprints/BP_Entry.uasset").write_bytes(b"new editable revision")
        result = self.service.package(str(receipt_path), "artifacts/TestMod-A")
        self.assertTrue(result["valid"])
        self.assertTrue(result["compiled"])
        self.assertFalse(result["installed"])
        self.assertEqual(result["source_revision"], receipt["source_manifest_sha256"])
        self.assertEqual({item["role"] for item in result["containers"]}, {"pak", "utoc", "ucas"})
        self.assertEqual(result["activation"]["entry_class"], "/TestMod/Blueprints/BP_Entry.BP_Entry_C")
        self.assertFalse(result["activation"]["entry_class_load_verified"])
        self.assertEqual(self.service.verify("artifacts/TestMod-A")["manifest_sha256"], result["manifest_sha256"])
        self.assertFalse(any(Path(item["path"]).suffix == ".dll" for item in result["files"]))

    def test_failed_missing_or_changed_cook_is_not_packaged(self):
        receipt_path, receipt = self.cook_receipt()
        with self.assertRaises(WorkspaceError):
            self.service.package("missing-receipt.json", "artifacts/missing")
        receipt["state"] = "failed"
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/failed")
        self.assertEqual(raised.exception.code, "cook_not_succeeded")
        receipt["state"] = "succeeded"
        self._write_receipt(receipt_path, receipt)
        output = next(item for item in receipt["outputs"] if item["role"] == "pak")
        Path(output["path"]).write_bytes(b"changed after cook")
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/changed")
        self.assertEqual(raised.exception.code, "cook_output_changed")
        self.assertFalse((self.workspace / "artifacts/changed").exists())

    def test_changed_captured_source_or_revision_is_rejected(self):
        receipt_path, receipt = self.cook_receipt()
        receipt["source_manifest_sha256"] = "0" * 64
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/bogus")
        self.assertEqual(raised.exception.code, "source_provenance_mismatch")
        receipt["source_manifest_sha256"] = source_manifest_sha256(receipt["inputs"])
        self._write_receipt(receipt_path, receipt)
        Path(receipt["inputs"][0]["source_path"]).write_bytes(b"changed capture")
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/changed-source")
        self.assertEqual(raised.exception.code, "captured_source_changed")

    def test_incomplete_container_set_and_unidentified_output_are_rejected(self):
        receipt_path, receipt = self.cook_receipt()
        output = next(item for item in receipt["outputs"] if item["role"] == "ucas")
        receipt["outputs"].remove(output)
        Path(output["path"]).unlink()
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/incomplete")
        self.assertEqual(raised.exception.code, "missing_cook_output")
        receipt["outputs"][0]["authoritative_cook_output"] = False
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/unidentified")
        self.assertEqual(raised.exception.code, "missing_cook_output")

    def test_verifier_rechecks_corruption_unlisted_files_and_receipt_identity(self):
        receipt_path, _ = self.cook_receipt()
        result = self.service.package(str(receipt_path), "artifacts/TestMod-A")
        package = Path(result["package_root"])
        container = package / result["containers"][0]["path"]
        raw = container.read_bytes()
        container.write_bytes(raw + b"corrupted")
        with self.assertRaises(ContentProjectError) as raised:
            verify_content_package(package)
        self.assertEqual(raised.exception.code, "package_changed")
        container.write_bytes(raw)
        extra = package / "extra.dll"
        extra.write_bytes(b"unlisted")
        with self.assertRaises(ContentProjectError) as raised:
            verify_content_package(package)
        self.assertEqual(raised.exception.code, "package_inventory_mismatch")
        extra.unlink()
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["build"]["receipt_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ContentProjectError) as raised:
            verify_content_package(package)
        self.assertEqual(raised.exception.code, "cook_provenance_mismatch")

    def test_dependency_mismatch_is_rejected(self):
        receipt_path, receipt = self.cook_receipt()
        receipt["shared_dependencies"][0]["sha256"] = "0" * 64
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/dependencies")
        self.assertEqual(raised.exception.code, "dependency_provenance_mismatch")

    def test_changed_payload_layout_is_rejected_even_with_consistent_hashes(self):
        receipt_path, receipt = self.cook_receipt()
        output = next(item for item in receipt["outputs"] if item["role"] == "pak")
        misplaced = Path(receipt["output_root"]) / "TestMod-Windows.pak"
        Path(output["path"]).rename(misplaced)
        output["path"] = str(misplaced)
        output["relative_path"] = misplaced.name
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/layout")
        self.assertEqual(raised.exception.code, "invalid_container_layout")

    def test_source_role_cannot_turn_arbitrary_file_into_authored_asset(self):
        receipt_path, receipt = self.cook_receipt()
        item = next(item for item in receipt["inputs"] if item["path"] == "README.md")
        item["role"] = "authored_asset"
        revision = source_manifest_sha256(receipt["inputs"])
        receipt["source_manifest_sha256"] = revision
        receipt["build_identity"]["source_manifest_sha256"] = revision
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), "artifacts/roles")
        self.assertEqual(raised.exception.code, "source_provenance_mismatch")

    def test_verifier_requires_source_correlated_entry_class(self):
        receipt_path, _ = self.cook_receipt()
        result = self.service.package(str(receipt_path), "artifacts/TestMod-A")
        package = Path(result["package_root"])
        activation_path = package / "activation.json"
        activation = json.loads(activation_path.read_text(encoding="utf-8"))
        activation["entry_class"] = None
        activation_raw = json.dumps(activation).encode()
        activation_path.write_bytes(activation_raw)
        manifest_path = package / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["activation"] = activation
        item = next(item for item in manifest["files"] if item["path"] == "activation.json")
        item.update({"sha256": sha(activation_raw), "bytes": len(activation_raw)})
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ContentProjectError) as raised:
            verify_content_package(package)
        self.assertEqual(raised.exception.code, "activation_contract_mismatch")

    def test_output_outside_recorded_root_is_rejected(self):
        receipt_path, receipt = self.cook_receipt()
        unrelated = self.base / "unrelated.pak"
        unrelated.write_bytes(b"unrelated")
        output = next(item for item in receipt["outputs"] if item["role"] == "pak")
        output.update({"path": str(unrelated), "sha256": sha(b"unrelated"), "bytes": len(b"unrelated")})
        self._write_receipt(receipt_path, receipt)
        with self.assertRaises(WorkspaceError):
            self.service.package(str(receipt_path), "artifacts/outside")
        self.assertEqual(unrelated.read_bytes(), b"unrelated")

    def test_duplicate_manifest_keys_are_rejected_without_creating_output(self):
        self.service.create("mods/Bad", "Bad")
        (self.workspace / "mods/Bad/mod.json").write_bytes(b'{"schema":1,"schema":2}')
        with self.assertRaises(ContentProjectError) as raised:
            self.service.validate("mods/Bad")
        self.assertEqual(raised.exception.code, "invalid_manifest")

    def test_malformed_receipt_roots_report_predictable_error(self):
        receipt_path, receipt = self.cook_receipt()
        for invalid in (None, [], "", "relative/path"):
            with self.subTest(invalid=invalid):
                receipt["staged_source_root"] = invalid
                self._write_receipt(receipt_path, receipt)
                with self.assertRaises(ContentProjectError) as raised:
                    self.service.package(str(receipt_path), "artifacts/malformed")
                self.assertEqual(raised.exception.code, "invalid_cook_receipt")

    def test_package_destination_does_not_write_repository_internals(self):
        receipt_path, _ = self.cook_receipt()
        with self.assertRaises(ContentProjectError) as raised:
            self.service.package(str(receipt_path), ".git/TestMod-A")
        self.assertEqual(raised.exception.code, "unsafe_path")
        self.assertFalse((self.workspace / ".git").exists())

    def test_validation_preserves_protected_nested_workspace_boundary(self):
        self.service.create("mods/Protected", "Protected")
        protected = self.workspace / "mods/Protected/Content/External"
        protected.mkdir()
        (protected / "Private.uasset").write_bytes(b"protected data")
        service = ContentProjects(SourceWorkspace(self.workspace, protected_roots=(protected,)), self.repository)
        with self.assertRaises(WorkspaceError) as raised:
            service.validate("mods/Protected")
        self.assertEqual(raised.exception.code, "unsafe_path")

    def test_binary_sources_and_containers_use_streaming_instead_of_whole_file_reads(self):
        receipt_path, _ = self.cook_receipt()
        import sn2_mcp.content_projects as content_projects
        original = content_projects._read

        def manifest_read(path, boundary):
            self.assertNotIn(path.suffix.casefold(), {".uasset", ".pak", ".utoc", ".ucas"})
            return original(path, boundary)

        with patch.object(content_projects, "_read", side_effect=manifest_read):
            self.assertTrue(self.service.validate("mods/TestMod")["valid"])
            result = self.service.package(str(receipt_path), "artifacts/TestMod-streaming")
            self.assertTrue(result["valid"])
            self.assertTrue(self.service.verify("artifacts/TestMod-streaming")["valid"])

    def test_failed_preparation_uses_cross_volume_recovery(self):
        real_rename = Path.rename
        real_move = shutil.move
        moves = []

        def rename_only_within_workspace(source, target):
            if Path(target).is_relative_to(self.repository.parent / ".trash"):
                raise OSError(18, "Cross-device link")
            return real_rename(source, target)

        def record_move(source, target):
            moves.append((source, target))
            return real_move(source, target)

        with patch("sn2_mcp.content_projects.verify_content_package", side_effect=ValueError("invalid generated package")), \
             patch("sn2_mcp.content_projects.shutil.move", side_effect=record_move), \
             patch.object(Path, "rename", rename_only_within_workspace):
            with self.assertRaises(ContentProjectError) as raised:
                self.service._publish("artifacts/broken", {"manifest.json": b"{}"}, package=True)
        self.assertEqual(raised.exception.code, "publish_failed")
        self.assertEqual(len(moves), 1)
        recovered = Path(raised.exception.receipt["trash_path"])
        self.assertEqual((recovered / "manifest.json").read_bytes(), b"{}")
        self.assertFalse((self.workspace / "artifacts/broken").exists())

    def test_streaming_read_access_error_is_predictable(self):
        self.service.create("mods/ReadFailure", "ReadFailure")
        import sn2_mcp.content_projects as content_projects
        with patch.object(content_projects, "_stream_identity_read", side_effect=PermissionError("synthetic access failure")):
            with self.assertRaises(WorkspaceError) as raised:
                self.service.validate("mods/ReadFailure")
        self.assertEqual(raised.exception.code, "access_error")


if __name__ == "__main__":
    unittest.main()
