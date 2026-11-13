"""Authoring fixtures verify capture/publication contracts, not Unreal compatibility."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sn2_mcp.content_authoring import ContentAuthoring, ContentAuthoringError, HELPER_NAME, _asset_file
from sn2_mcp.content_projects import _GENERATED_DIRECTORIES
from sn2_mcp.workspace import SourceWorkspace, WorkspaceError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class AuthorRunner:
    def __init__(self, test, *, fail=False, stale=False, missing=False, cleanup=True, change_dependency=False):
        self.test = test
        self.fail, self.stale, self.missing, self.cleanup = fail, stale, missing, cleanup
        self.change_dependency = change_dependency
        self.calls = []

    def __call__(self, args, cwd, timeout, stdout, stderr):
        self.calls.append(args)
        plugin = Path(next(v.split("=", 1)[1] for v in args if v.startswith("-PluginRoot=")))
        spec = Path(next(v.split("=", 1)[1] for v in args if v.startswith("-Spec=")))
        report_path = Path(next(v.split("=", 1)[1] for v in args if v.startswith("-Report=")))
        self.test.assertFalse((plugin / "Content/BP_Entry.uasset").exists())
        self.test.assertEqual(sha(spec.read_bytes()), sha(self.test.spec.read_bytes()))
        target = plugin / "Content/BP_Entry.uasset"
        if not self.missing:
            target.write_bytes(b"new editable asset")
        report = {"schema": 1, "kind": "sn2_content_authoring_report", "state": "failed" if self.fail else "succeeded",
                  "errors": [{"code": "compile_error", "message": "Unknown variable", "node": "set"}] if self.fail else [],
                  "assets": [{"path": "/TestMod/BP_Entry.BP_Entry", "kind": "blueprint", "saved": not self.fail, "generated_class": "/TestMod/BP_Entry.BP_Entry_C",
                              "compiler": {"errors": int(self.fail), "warnings": 0, "messages": []}}],
                  "outputs": [{"path": str(target), "asset": "/TestMod/BP_Entry.BP_Entry"}]}
        report_path.write_text(json.dumps(report), encoding="utf-8")
        if self.stale:
            self.test.spec.write_text(self.test.spec.read_text().replace('"1.0"', '"2.0"'), encoding="utf-8")
        if self.change_dependency:
            (cwd / "Config/DefaultEngine.ini").write_bytes(b"changed dependency")
        stdout.write_bytes(b"Compile result: error\n" if self.fail else b"Authoring succeeded\n")
        stderr.write_bytes(b"")
        return {"state": "failed" if self.fail else "succeeded", "exit_code": int(self.fail),
                "tree_exit_confirmed": self.cleanup, "output_complete": True,
                "stdout": {"path": str(stdout), "bytes": stdout.stat().st_size, "sha256": sha(stdout.read_bytes())},
                "stderr": {"path": str(stderr), "bytes": 0, "sha256": sha(b"")}}


class ContentAuthoringTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.root, self.repo = root / "workspace", root / "repo"
        self.root.mkdir(); self.repo.mkdir()
        self.modkit, self.engine = self.root / "Modkit", self.root / "Engine"
        self.project = self.root / "projects/TestMod"
        (self.project / "Content").mkdir(parents=True)
        (self.project / "TestMod.uplugin").write_text(json.dumps({"FileVersion": 3, "CanContainContent": True, "EnabledByDefault": True}), encoding="utf-8")
        (self.project / "mod.json").write_text(json.dumps({"schema": 1, "kind": "sn2_content_mod_project",
            "mod_id": "TestMod", "plugin_name": "TestMod", "entry_class": "/TestMod/BP_Entry.BP_Entry_C"}), encoding="utf-8")
        self.asset = self.project / "Content/BP_Entry.uasset"
        self.asset.write_bytes(b"prior editable asset")
        self.spec = self.project / "authoring.json"
        self.spec.write_text(json.dumps({"schema": 1, "plugin_id": "TestMod", "assets": [
            {"kind": "blueprint", "path": "/TestMod/BP_Entry.BP_Entry", "parent_class": "/Script/Engine.Actor",
             "variables": [{"name": "ProbeValue", "type": "float", "default": "0"}],
             "nodes": [{"id": "begin", "kind": "event", "function": "ReceiveBeginPlay"},
                       {"id": "set", "kind": "variable_set", "variable": "ProbeValue", "defaults": {"ProbeValue": "1.0"}}],
             "connections": [{"from": {"node": "begin", "pin": "then"}, "to": {"node": "set", "pin": "execute"}}]}]}), encoding="utf-8")
        for relative in ("Subnautica2.uproject", "Binaries/Win64/Subnautica2Editor.target", "Config/DefaultEngine.ini"):
            target = self.modkit / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('{}' if relative.endswith(('uproject', 'target')) else 'Synthetic=True', encoding="utf-8")
        for relative in ("Build/Build.version", "Binaries/Win64/UnrealEditor-Cmd.exe", "Binaries/Win64/UnrealEditor.modules", "Build/BatchFiles/Build.bat", "Binaries/ThirdParty/DotNet/8.0.300/win-x64/dotnet.exe", "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"):
            target = self.engine / relative; target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b'{}' if relative.endswith(('version', 'modules')) else b"synthetic executable")
        helper = self.repo / "development/authoring" / HELPER_NAME
        (helper / "Source/SN2MCPAuthoring").mkdir(parents=True)
        (helper / (HELPER_NAME + ".uplugin")).write_text('{}', encoding="utf-8")
        (helper / "Source/SN2MCPAuthoring/Helper.cpp").write_text('// synthetic helper', encoding="utf-8")
        self.workspace = SourceWorkspace(self.root)

    def service(self, runner):
        service = ContentAuthoring(self.repo, workspace_root=self.root, modkit_root=self.modkit,
                                   engine_root=self.engine, workspace=self.workspace, runner=runner)
        def helper(recipe, source, directory, timeout, started, receipt):
            target = service._directory(source / "Plugins" / HELPER_NAME)
            (target / (HELPER_NAME + ".uplugin")).write_bytes(b'{}')
            receipt["helper_build"] = {"state": "fixture_only"}
            return target
        service._helper = helper
        return service

    def test_capture_regenerates_selected_assets_and_publishes_exact_success(self):
        runner = AuthorRunner(self); service = self.service(runner)
        before = service.recipe("projects/TestMod", "projects/TestMod/authoring.json")
        result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["state"], "succeeded", result)
        self.assertTrue(result["compiled"]); self.assertTrue(result["published"])
        self.assertEqual(self.asset.read_bytes(), b"new editable asset")
        self.assertNotEqual(before["source_manifest_sha256"], result["result_source_manifest_sha256"])
        self.assertIn("-ddc=NoZenLocalFallback", runner.calls[0])
        self.assertTrue(any(a.startswith("-LocalDataCachePath=") for a in runner.calls[0]))
        self.assertFalse(result["installed_in_game"])
        self.assertEqual(service.status(result["authoring_id"])["state"], "succeeded")
        self.assertIn("sn2_content_authoring_report", service.read_report(result["authoring_id"], stream="structured")["text"])

    def test_source_excluded_asset_directories_rejected_before_execution(self):
        runner = AuthorRunner(self)
        service = self.service(runner)
        original = self.asset.read_bytes()
        for name in sorted(_GENERATED_DIRECTORIES):
            if not name.isidentifier():
                continue
            for prefix in (name.upper(), "Nested/" + name.upper()):
                with self.subTest(directory=prefix):
                    specification = {"schema": 1, "plugin_id": "TestMod", "assets": [
                        {"kind": "data_asset", "path": "/TestMod/" + prefix + "/DA_Test.DA_Test"}]}
                    self.spec.write_text(json.dumps(specification), encoding="utf-8")
                    for operation in (service.recipe, service.author):
                        with self.assertRaises(ContentAuthoringError) as caught:
                            operation("projects/TestMod", "projects/TestMod/authoring.json")
                        self.assertEqual(caught.exception.code, "invalid_asset_path")
                    self.assertFalse((self.project / "Content" / prefix).exists())
        self.assertEqual(runner.calls, [])
        self.assertEqual(self.asset.read_bytes(), original)
        self.assertFalse((self.root / "artifacts/content-authoring").exists())

    def test_generated_directory_name_as_asset_basename_remains_source(self):
        self.assertEqual(_asset_file("/TestMod/Nested/Saved.Saved", "TestMod"), "Content/Nested/Saved.uasset")

    def test_compiler_failure_does_not_publish_partial_output(self):
        service = self.service(AuthorRunner(self, fail=True))
        result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["state"], "failed")
        self.assertFalse(result["published"]); self.assertFalse(result["compiled"])
        self.assertEqual(self.asset.read_bytes(), b"prior editable asset")
        self.assertEqual(result["authoring_report"]["errors"][0]["node"], "set")
        self.assertIn("Unknown variable", service.read_report(result["authoring_id"], stream="structured")["text"])

    def test_status_while_command_runs_does_not_claim_confirmed_exit(self):
        fixture = AuthorRunner(self)
        def runner(*arguments):
            running = service.status()
            self.assertEqual(running["state"], "authoring")
            self.assertTrue(running["resident_worker"])
            self.assertTrue(running["cleanup"]["worker_started"])
            self.assertFalse(running["cleanup"]["tree_exit_confirmed"])
            return fixture(*arguments)
        service = self.service(runner)
        result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["state"], "succeeded", result)
        self.assertFalse(result["resident_worker"])
        self.assertTrue(result["cleanup"]["tree_exit_confirmed"])

    def test_stale_specification_keeps_original_and_compiled_capture(self):
        result = self.service(AuthorRunner(self, stale=True)).author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["failure_code"], "workspace_revision_changed")
        self.assertTrue(result["captured_assets_compiled"]); self.assertFalse(result["published"])
        self.assertEqual(self.asset.read_bytes(), b"prior editable asset")

    def test_missing_output_and_unconfirmed_process_exit_cannot_publish(self):
        for keyword in ({"missing": True}, {"cleanup": False}):
            with self.subTest(keyword=keyword):
                result = self.service(AuthorRunner(self, **keyword)).author("projects/TestMod", "projects/TestMod/authoring.json")
                self.assertEqual(result["state"], "failed"); self.assertFalse(result["published"])
                self.assertEqual(self.asset.read_bytes(), b"prior editable asset")

    def test_dependency_change_after_editor_prevents_publication(self):
        with patch("sn2_mcp.content_authoring._guard_input_files"):
            result = self.service(AuthorRunner(self, change_dependency=True)).author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["failure_code"], "captured_dependency_changed")
        self.assertFalse(result["published"])

    def test_dependency_hash_race_retains_original_error_without_starting_worker(self):
        from sn2_mcp.content_authoring import _cook_inventory
        from sn2_mcp.content_cooker import ContentCookError
        runner = AuthorRunner(self)
        service = self.service(runner)
        def inventory(*args, **kwargs):
            if args[2] == "modkit_dependency":
                raise ContentCookError("input_changed", "A cook input changed while hashing")
            return _cook_inventory(*args, **kwargs)
        with patch("sn2_mcp.content_authoring._cook_inventory", side_effect=inventory):
            result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["failure_code"], "input_changed")
        self.assertEqual(result["error"]["message"], "A cook input changed while hashing")
        self.assertFalse(result["published"])
        self.assertFalse(result["cleanup"]["worker_started"])
        self.assertTrue(result["cleanup"]["tree_exit_confirmed"])
        self.assertFalse(result["resident_worker"])
        self.assertFalse(runner.calls)
        self.assertEqual(self.asset.read_bytes(), b"prior editable asset")

    def test_report_hash_race_preserves_error_and_actual_worker_cleanup(self):
        from sn2_mcp.content_authoring import _cook_file_info
        from sn2_mcp.content_cooker import ContentCookError
        for cleanup in (True, False):
            with self.subTest(cleanup=cleanup):
                service = self.service(AuthorRunner(self, cleanup=cleanup))
                def file_info(*args, **kwargs):
                    if args[3] == "authoring_report":
                        raise ContentCookError("input_changed", "The report changed while hashing")
                    return _cook_file_info(*args, **kwargs)
                with patch("sn2_mcp.content_authoring._cook_file_info", side_effect=file_info):
                    result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["failure_code"], "input_changed" if cleanup else "cleanup_unconfirmed")
                self.assertEqual(result["finalization_error"]["code"], "input_changed")
                self.assertEqual(result["finalization_error"]["message"], "The report changed while hashing")
                self.assertEqual(result["cleanup"]["tree_exit_confirmed"], cleanup)
                self.assertEqual(result["resident_worker"], not cleanup)
                self.assertFalse(result["published"])
                self.assertEqual(self.asset.read_bytes(), b"prior editable asset")

    def test_scope_and_duplicate_assets_rejected_before_process(self):
        service = self.service(AuthorRunner(self))
        with self.assertRaises(WorkspaceError): service.recipe("projects/TestMod", "../outside.json")
        spec = json.loads(self.spec.read_text()); spec["assets"].append(spec["assets"][0])
        self.spec.write_text(json.dumps(spec), encoding="utf-8")
        with self.assertRaises(ContentAuthoringError) as error: service.recipe("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(error.exception.code, "duplicate_asset_path")

    def test_report_bytes_cannot_be_silently_replaced(self):
        service = self.service(AuthorRunner(self)); result = service.author("projects/TestMod", "projects/TestMod/authoring.json")
        Path(result["structured_report"]["source_path"]).write_text('{}', encoding="utf-8")
        with self.assertRaises(ContentAuthoringError) as error: service.read_report(result["authoring_id"], stream="structured")
        self.assertEqual(error.exception.code, "report_changed")

    def test_interrupted_runner_preserves_unconfirmed_cleanup(self):
        from sn2_mcp.build_workflow import BuildError
        def runner(*args):
            raise BuildError("interrupted", "The worker outcome is unknown", {"state": "cleanup_unconfirmed",
                "tree_exit_confirmed": False, "termination_requested": True, "output_complete": False})
        result = self.service(runner).author("projects/TestMod", "projects/TestMod/authoring.json")
        self.assertEqual(result["failure_code"], "interrupted")
        self.assertTrue(result["resident_worker"])
        self.assertFalse(result["cleanup"]["tree_exit_confirmed"])
        self.assertFalse(result["published"])

    def test_two_asset_failed_publication_restores_first_and_removes_pending(self):
        service = self.service(AuthorRunner(self))
        recipe = service.recipe("projects/TestMod", "projects/TestMod/authoring.json")
        directory = service._directory(self.root / "artifacts/content-authoring" / ("a" * 32))
        output = directory / "BP_Entry.uasset"; output.write_bytes(b"replacement")
        second = directory / "Second.uasset"; second.write_bytes(b"second")
        from sn2_mcp.content_cooker import _file_info
        outputs = [_file_info(output, directory, "Content/BP_Entry.uasset", "authored_asset"),
                   _file_info(second, directory, "Content/Sub/Second.uasset", "authored_asset")]
        receipt = {"authoring_id": "a" * 32, "published": False}
        replace = __import__('os').replace
        def fail_second(source, target):
            if str(target).endswith("Second.uasset"):
                raise OSError("synthetic publish failure")
            return replace(source, target)
        with patch("sn2_mcp.content_authoring.os.replace", side_effect=fail_second):
            with self.assertRaises(OSError): service._publish(recipe, outputs, directory, receipt)
        self.assertEqual(self.asset.read_bytes(), b"prior editable asset")
        self.assertFalse(list(self.project.rglob("*.pending")))
        self.assertFalse((self.project / "Content/Sub/Second.uasset").exists())
        self.assertTrue(receipt["publication_rollback_confirmed"])

    def test_helper_cache_requires_complete_source_and_binary_inventory(self):
        service = self.service(AuthorRunner(self))
        recipe = service.recipe("projects/TestMod", "projects/TestMod/authoring.json")
        cache = service._directory(service.helper_cache / recipe["helper_id"])
        (cache / "receipt.json").write_text(json.dumps({"state": "succeeded", "identity": recipe["helper_identity"], "files": []}), encoding="utf-8")
        self.assertIsNone(service._cached_helper(recipe["helper_id"], recipe["helper_identity"]))
        from sn2_mcp.content_cooker import ContentCookError
        (cache / "receipt.json").write_text(json.dumps({"state": "succeeded", "identity": recipe["helper_identity"], "files": [{}]}), encoding="utf-8")
        with patch("sn2_mcp.content_authoring._cook_inventory", side_effect=ContentCookError("input_changed", "The cache file changed while hashing")):
            self.assertIsNone(service._cached_helper(recipe["helper_id"], recipe["helper_identity"]))

    def test_protected_project_scope_preserved(self):
        self.workspace = SourceWorkspace(self.root, protected_roots=(self.project,))
        service = self.service(AuthorRunner(self))
        with self.assertRaises(WorkspaceError): service.recipe("projects/TestMod", "projects/TestMod/authoring.json")


if __name__ == "__main__": unittest.main()
