"""Test source correlation, native uncertainty and working-revision preservation."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sn2_mcp.content_projects import validate_content_project
from sn2_mcp.native_client import NativeError
from sn2_mcp.runtime_authoring import RuntimeAuthoring, RuntimeAuthoringError


def specification():
    return {"schema": 1, "plugin_id": "UserMod", "assets": [{
        "kind": "blueprint", "path": "/UserMod/BP_User.BP_User",
        "parent_class": "/Script/Engine.Actor", "variables": [], "nodes": [],
        "connections": [], "functions": [],
    }]}


class NativeFixture:
    def __init__(self):
        self.calls = []
        self.error = None
        self.on_request = None
        self.response = {"source": "runtime_blueprint_compilation", "compiled": True,
                         "runtime_published": True, "persisted": False,
                         "class": {"handle": "class-17"},
                         "functions": [{"handle": "function-18", "script_verified": True}],
                         "effects": {"executed": True, "may_have_executed": False}}

    def __call__(self, operation, params, timeout):
        self.calls.append((operation, params, timeout))
        if self.on_request is not None:
            self.on_request()
        if self.error is not None:
            raise self.error
        if operation == "runtime_compile_blueprint":
            return {**self.response, "asset_path": params["asset"]["path"],
                    "class": {**self.response.get("class", {}), "path": params["asset"]["path"] + "_C"}}
        directory = Path(params["output_directory"])
        package = params["package_name"]
        leaf = package.rsplit("/", 1)[-1]
        uasset, uexp = directory / (leaf + ".uasset"), directory / (leaf + ".uexp")
        uasset.write_bytes(b"raw package header")
        uexp.write_bytes(b"raw export data")
        response = {"source": "runtime_package_serialization", "serialized": True, "persisted": True,
                    "package_name": package, "uasset_path": str(uasset), "uexp_path": str(uexp),
                    "uasset_bytes": uasset.stat().st_size, "uexp_bytes": uexp.stat().st_size,
                    "fresh_load_verified": False, "containerized": getattr(self, "containerized", False)}
        if response["containerized"]:
            outputs = []
            for suffix in ("pak", "utoc", "ucas"):
                path = directory / (params["plugin_id"] + "." + suffix)
                path.write_bytes((suffix + " synthetic container").encode())
                outputs.append({"path": str(path), "bytes": path.stat().st_size, "role": suffix})
            response["outputs"] = outputs
        return response


class RuntimeAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "projects/UserMod"
        (self.project / "Content").mkdir(parents=True)
        (self.project / "UserMod.uplugin").write_text(json.dumps({
            "FileVersion": 3, "CanContainContent": True, "EnabledByDefault": True,
        }), encoding="utf-8")
        (self.project / "mod.json").write_text(json.dumps({
            "schema": 1, "kind": "sn2_content_mod_project", "mod_id": "UserMod",
            "plugin_name": "UserMod", "entry_class": "/UserMod/BP_User.BP_User_C",
        }), encoding="utf-8")
        self.spec = self.project / "authoring.json"
        self.spec.write_bytes(b"\xef\xbb\xbf" + json.dumps(specification(), indent=2).encode("utf-8") + b"\r\n")
        self.native = NativeFixture()
        self.service = RuntimeAuthoring(self.root, self.native)
        self.project_path = "projects/UserMod"
        self.spec_path = "projects/UserMod/authoring.json"

    def compile(self):
        return self.service.compile(self.project_path, self.spec_path, timeout_seconds=3.5)

    def test_compile_captures_exact_input_and_only_calls_native_backend(self):
        raw = self.spec.read_bytes()
        receipt = self.compile()
        expected = {**specification()["assets"][0], "path": receipt["runtime_asset_path"]}
        self.assertEqual(self.native.calls, [("runtime_compile_blueprint", {"asset": expected}, 3.5)])
        self.assertEqual(receipt["specification_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual((Path(receipt["receipt_path"]).parent / "specification.json").read_bytes(), raw)
        self.assertTrue(receipt["compiled"])
        self.assertTrue(receipt["runtime_published"])
        self.assertFalse(receipt["disk_assets_published"])
        self.assertFalse(receipt["persisted"])
        self.assertFalse(receipt["packaged"])
        self.assertFalse(receipt["whole_project_compiled"])
        self.assertTrue(receipt["source_current"])
        self.assertFalse((self.project / "Content/BP_User.uasset").exists())
        self.assertEqual(receipt["native_response"]["class"]["path"], receipt["runtime_asset_path"] + "_C")
        self.assertNotEqual(receipt["runtime_asset_path"], receipt["selected_asset_path"])
        self.assertTrue((Path(receipt["staged_source_root"]) / "UserMod.uplugin").is_file())

    def test_specification_change_after_project_inventory_rejects_before_native_call(self):
        original = self.spec.read_bytes()
        for changed in (original[:-2] + b" \n", original + b" "):
            with self.subTest(size=len(changed)):
                self.spec.write_bytes(original)

                def inventory_then_change(*args, **kwargs):
                    project = validate_content_project(*args, **kwargs)
                    self.spec.write_bytes(changed)
                    return project

                with patch("sn2_mcp.runtime_authoring.validate_content_project", inventory_then_change):
                    with self.assertRaises(RuntimeAuthoringError) as caught:
                        self.compile()
                self.assertEqual(caught.exception.code, "specification_changed")
                self.assertFalse(caught.exception.details["executed"])
                self.assertFalse(caught.exception.details["may_have_executed"])
                self.assertEqual(self.native.calls, [])
                self.assertEqual(self.service.status(working=True)["state"], "not_compiled")
        external = self.root / "external-authoring.json"
        external.write_bytes(original)
        self.spec.write_bytes(original)
        recipe = self.service.recipe(self.project_path, "external-authoring.json")
        self.assertEqual(recipe["specification_sha256"], hashlib.sha256(original).hexdigest())
        self.assertEqual(self.native.calls, [])

    def test_compiler_rejection_preserves_working_revision_and_diagnostics(self):
        working = self.compile()
        original_source = self.spec.read_bytes()
        failure = NativeError("graph_pin_unavailable", "Pin not found", False, "request-B", {
            "executed": False, "may_have_executed": False,
            "function": "Calculate", "node": "result", "pin": "Missing",
        })
        self.native.error = failure
        with self.assertRaises(NativeError) as caught:
            self.compile()
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.service.status(working=True)["authoring_id"], working["authoring_id"])
        self.assertEqual(self.service.status()["state"], "failed")
        self.assertEqual(self.service.report(self.service.status()["authoring_id"])["error"]["details"], failure.details)
        self.assertEqual(self.spec.read_bytes(), original_source)
        self.assertEqual(len(self.native.calls), 2)

    def test_uncertain_failure_is_not_retried_or_published(self):
        failure = NativeError("response_lost", "Request outcome unknown", True, "request-A", {
            "executed": True, "may_have_executed": True, "do_not_retry": True,
        })
        self.native.error = failure
        with self.assertRaises(NativeError) as caught:
            self.compile()
        self.assertIs(caught.exception, failure)
        receipt = self.service.status()
        self.assertEqual(receipt["state"], "outcome_unknown")
        self.assertFalse(receipt["compiled"])
        self.assertEqual(receipt["error"]["details"], failure.details)
        self.assertEqual(len(self.native.calls), 1)
        self.assertEqual(self.service.status(working=True)["state"], "not_compiled")

    def test_return_without_explicit_compilation_proof_does_not_replace_working(self):
        working = self.compile()
        for response in ({"source": "runtime_blueprint_compilation", "effects": {"executed": True}},
                         {"source": "synthetic_reply", "compiled": True},
                         {"source": "runtime_blueprint_compilation", "compiled": False}):
            with self.subTest(response=response):
                self.native.response = response
                receipt = self.compile()
                self.assertEqual(receipt["state"], "returned_unverified")
                self.assertFalse(receipt["compiled"])
                self.assertEqual({key: receipt["native_response"][key] for key in response}, response)
                self.assertEqual(self.service.status(working=True)["authoring_id"], working["authoring_id"])

    def test_source_change_during_call_is_separate_from_captured_compilation(self):
        captured = self.spec.read_bytes()
        self.native.on_request = lambda: self.spec.write_text(json.dumps(specification()) + "\n", encoding="utf-8")
        receipt = self.compile()
        self.assertTrue(receipt["compiled"])
        self.assertFalse(receipt["source_current"])
        self.assertEqual(receipt["specification_sha256"], hashlib.sha256(captured).hexdigest())

    def test_multi_asset_input_requires_explicit_selected_target(self):
        spec = specification()
        spec["assets"].append({**spec["assets"][0], "path": "/UserMod/BP_Other.BP_Other"})
        self.spec.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaises(RuntimeAuthoringError) as caught:
            self.compile()
        self.assertEqual(caught.exception.code, "runtime_asset_selection_required")
        self.assertEqual(self.native.calls, [])
        receipt = self.service.compile(self.project_path, self.spec_path, "/UserMod/BP_Other.BP_Other")
        self.assertEqual(receipt["selected_asset_path"], "/UserMod/BP_Other.BP_Other")
        self.assertEqual(receipt["declared_asset_count"], 2)
        self.assertFalse(receipt["whole_project_compiled"])

    def test_two_revisions_have_distinct_live_candidates_and_canonical_save_target(self):
        first = self.compile()
        second = self.compile()
        self.assertNotEqual(first["runtime_asset_path"], second["runtime_asset_path"])
        self.assertEqual(first["selected_asset_path"], second["selected_asset_path"])
        saved = self.service.save(second["authoring_id"])
        operation, params, timeout = self.native.calls[-1]
        self.assertEqual(operation, "runtime_save_package")
        self.assertEqual(params["package_name"], "/UserMod/BP_User")
        self.assertEqual(params["class_handle"], second["native_response"]["class"]["handle"])
        self.assertEqual(saved["state"], "serialized_raw_package")
        self.assertTrue(saved["persisted"])
        self.assertFalse(saved["containerized"])
        self.assertFalse(saved["packaged"])
        self.assertEqual(saved["outputs"], [])
        self.assertEqual({item["role"] for item in saved["intermediate_outputs"]}, {"uasset", "uexp"})

    def test_complete_runtime_triplet_integrates_with_actual_publisher_and_verifier(self):
        from sn2_mcp.content_projects import ContentProjects
        from sn2_mcp.workspace import SourceWorkspace
        self.native.containerized = True
        compiled = self.compile()
        saved = self.service.save(compiled["authoring_id"], timeout_seconds=8)
        self.assertEqual(saved["state"], "succeeded", saved)
        self.assertTrue(saved["containerized"])
        self.assertFalse(saved["fresh_load_verified"])
        self.assertEqual({item["role"] for item in saved["outputs"]}, {"plugin_descriptor", "pak", "utoc", "ucas"})
        self.assertTrue(all(Path(item["path"]).is_absolute() for item in saved["outputs"]))
        self.assertFalse((self.project / "Content/BP_User.uasset").exists())
        publisher = ContentProjects(SourceWorkspace(self.root), self.root)
        result = publisher.runtime_package(saved["receipt_path"], "packages/UserMod-B")
        self.assertIn("manifest_sha256", result)
        verified = publisher.verify("packages/UserMod-B")
        self.assertTrue(verified["valid"])
        manifest = json.loads((self.root / "packages/UserMod-B/manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["build"]["method"], "runtime_blueprint")
        self.assertEqual(verified["source_revision"], compiled["source_manifest_sha256"])

    def test_runtime_save_uncertainty_retains_raw_native_error_and_does_not_retry(self):
        compiled = self.compile()
        failure = NativeError("response_lost", "The save outcome is unknown", True, "save-17", {"do_not_retry": True})
        self.native.error = failure
        with self.assertRaises(NativeError) as raised:
            self.service.save(compiled["authoring_id"])
        self.assertIs(raised.exception, failure)
        saved = self.service.build_status()
        self.assertEqual(saved["state"], "outcome_unknown")
        self.assertEqual(saved["error"]["details"], failure.details)
        self.assertFalse(saved["containerized"])
        self.assertEqual(len(self.native.calls), 2)
        self.assertEqual(self.service.status(working=True)["authoring_id"], compiled["authoring_id"])

    def test_malformed_container_report_finalizes_known_save_without_verified_package(self):
        compiled = self.compile()
        fixture = self.native
        self.native.containerized = True
        def malformed(operation, params, timeout):
            response = fixture(operation, params, timeout)
            response["outputs"][0] = None
            return response
        self.service.request = malformed
        saved = self.service.save(compiled["authoring_id"])
        self.assertEqual(saved["state"], "output_unverified")
        self.assertTrue(saved["persisted"])
        self.assertFalse(saved["containerized"])
        self.assertEqual(saved["error"]["code"], "incomplete_runtime_containers")
        self.assertEqual(self.service.build_status(saved["build_id"])["finished_at"], saved["finished_at"])

    def test_save_exception_retains_original_when_failure_receipt_cannot_be_written(self):
        compiled = self.compile()
        error = RuntimeError("The native save outcome is unknown")
        def request(*args):
            raise error
        self.service.request = request
        original = self.service._save_build
        def save(directory, receipt):
            if receipt["state"] == "outcome_unknown":
                raise OSError("The receipt volume is unavailable")
            return original(directory, receipt)
        with patch.object(self.service, "_save_build", side_effect=save):
            with self.assertRaises(RuntimeError) as raised:
                self.service.save(compiled["authoring_id"])
        self.assertIs(raised.exception, error)
        self.assertIn("uncertain save receipt", error.__notes__[0])

    def test_external_specification_changes_have_distinct_compiler_identity_and_cannot_be_tampered(self):
        external = self.root / "external.json"
        external.write_bytes(self.spec.read_bytes())
        first = self.service.compile(self.project_path, "external.json")
        changed = specification()
        changed["assets"][0]["variables"] = [{"name": "Count", "type": "int", "default": "1"}]
        external.write_text(json.dumps(changed), encoding="utf-8")
        second = self.service.compile(self.project_path, "external.json")
        self.assertEqual(first["source_manifest_sha256"], second["source_manifest_sha256"])
        self.assertNotEqual(first["specification_sha256"], second["specification_sha256"])
        evidence = second["compiler_specification"]
        Path(evidence["source_path"]).write_text('{}', encoding="utf-8")
        with self.assertRaises(RuntimeAuthoringError) as raised:
            self.service.save(second["authoring_id"])
        self.assertEqual(raised.exception.code, "captured_specification_changed")
        self.assertEqual(len(self.native.calls), 2)

    def test_external_graph_revisions_package_exact_spec_and_verify_without_authoring_capture(self):
        from sn2_mcp.content_projects import ContentProjects
        from sn2_mcp.workspace import SourceWorkspace
        self.native.containerized = True
        publisher = ContentProjects(SourceWorkspace(self.root), self.root)
        external = self.root / "external-authoring.json"
        captures = []
        manifests = []
        packaged_specs = []
        for revision, default in (("A", "1"), ("B", "2")):
            graph = specification()
            graph["assets"][0]["variables"] = [{"name": "Count", "type": "int", "default": default}]
            raw = b"\xef\xbb\xbf" + json.dumps(graph, indent=2).replace("\n", "\r\n").encode("utf-8") + b"\r\n"
            external.write_bytes(raw)
            compiled = self.service.compile(self.project_path, "external-authoring.json")
            saved = self.service.save(compiled["authoring_id"])
            self.assertEqual(saved["state"], "succeeded", saved)
            destination = "packages/UserMod-" + revision
            publisher.runtime_package(saved["receipt_path"], destination)
            package_root = self.root / destination
            manifest = json.loads((package_root / "manifest.json").read_text(encoding="utf-8"))
            packaged_spec = (package_root / "compiler-specification.json").read_bytes()
            self.assertEqual(packaged_spec, raw)
            self.assertEqual(manifest["build"]["specification_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(manifest["build"]["specification_sha256"], compiled["specification_sha256"])
            verified = publisher.verify(destination)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["source_revision"], compiled["source_manifest_sha256"])
            captures.append(compiled)
            manifests.append(manifest)
            packaged_specs.append(packaged_spec)
        self.assertEqual(captures[0]["source_manifest_sha256"], captures[1]["source_manifest_sha256"])
        self.assertNotEqual(captures[0]["specification_sha256"], captures[1]["specification_sha256"])
        self.assertNotEqual(manifests[0]["build"]["specification_sha256"], manifests[1]["build"]["specification_sha256"])
        self.assertNotEqual(packaged_specs[0], packaged_specs[1])
        authoring_capture = self.root / "artifacts/runtime-authoring"
        authoring_capture.rename(self.root / "unavailable-runtime-authoring")
        self.assertFalse(authoring_capture.exists())
        for revision, compiled in zip(("A", "B"), captures):
            self.assertFalse(Path(compiled["compiler_specification"]["source_path"]).exists())
            verified = publisher.verify("packages/UserMod-" + revision)
            self.assertTrue(verified["valid"])
            self.assertEqual(verified["source_revision"], compiled["source_manifest_sha256"])

    def test_recipe_and_invalid_input_have_no_native_effect(self):
        recipe = self.service.recipe(self.project_path, self.spec_path)
        self.assertFalse(recipe["backend_readiness_checked"])
        self.assertEqual(self.native.calls, [])
        spec = specification()
        spec["assets"][0]["path"] = "/OtherMod/BP_User.BP_User"
        self.spec.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaises(RuntimeAuthoringError) as caught:
            self.compile()
        self.assertEqual(caught.exception.code, "invalid_asset_path")
        self.assertFalse(caught.exception.details["executed"])
        self.assertEqual(self.native.calls, [])


if __name__ == "__main__":
    unittest.main()
