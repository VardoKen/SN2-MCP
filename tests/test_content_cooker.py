"""Contained content-cook evidence checks with neutral synthetic files and runner."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.content_cooker import ContentCooker, ContentCookError, _manifest
from sn2_mcp.build_workflow import run_build_command
from sn2_mcp.workspace import SourceWorkspace, WorkspaceError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class FixtureRunner:
    def __init__(self, fixture, *, failure=False, missing=False, mutate=False,
                 cleanup=True, output_error=False, dependency_change=False, dependency_addition=False,
                 source_addition=False):
        self.fixture = fixture
        self.failure, self.missing, self.mutate = failure, missing, mutate
        self.cleanup, self.output_error, self.dependency_change = cleanup, output_error, dependency_change
        self.dependency_addition, self.source_addition = dependency_addition, source_addition
        self.calls = []

    def __call__(self, arguments, cwd, timeout, stdout, stderr):
        self.calls.append((arguments, cwd, timeout))
        stage = Path(next(item.split("=", 1)[1] for item in arguments if item.startswith("-stagingdirectory=")))
        paks = stage / "Subnautica2/Plugins/TestMod/Content/Paks/Windows"
        if not self.failure:
            paks.mkdir(parents=True)
            for suffix in ("pak", "utoc") if self.missing else ("pak", "utoc", "ucas"):
                (paks / ("TestMod-Windows." + suffix)).write_bytes(("synthetic-" + suffix).encode())
        if self.mutate:
            (self.fixture.project / "Content/Marker.uasset").write_bytes(b"edited source")
        if self.dependency_change:
            with self.fixture.engine_file.open("wb") as stream:
                stream.write(b"changed engine executable")
        if self.dependency_addition:
            (self.fixture.engine / "Binaries/Win64/Added.dll").write_bytes(b"new unrecorded dependency")
        if self.source_addition:
            (Path(cwd) / "Plugins/TestMod/Content/Added.uasset").write_bytes(b"new unrecorded asset")
        output = ("LogCook: Error: /TestMod/Marker: Blueprint failed\n" if self.failure or self.output_error
                  else "LogCook: Display: Cook complete\n")
        stdout.write_bytes(output.encode("utf-8"))
        stderr.write_bytes(b"")
        return {"state": "failed" if self.failure else "succeeded", "exit_code": 1 if self.failure else 0,
                "tree_exit_confirmed": self.cleanup, "output_complete": True,
                "stdout": {"path": str(stdout), "bytes": stdout.stat().st_size, "sha256": sha(stdout.read_bytes())},
                "stderr": {"path": str(stderr), "bytes": 0, "sha256": sha(b"")}, "arguments": arguments}


class ContentCookerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "authoring"
        self.workspace.mkdir()
        self.repository = self.root / "repository"
        self.repository.mkdir()
        self.modkit = self.workspace / "Modkit"
        self.modkit.mkdir()
        self.engine = self.workspace / "Engine/Engine"
        self.game = self.root / "game"
        self.game.mkdir()
        self.project = self.workspace / "projects/TestMod"
        (self.project / "Content").mkdir(parents=True)
        (self.project / "TestMod.uplugin").write_text(json.dumps({"FileVersion": 3, "CanContainContent": True}), encoding="utf-8")
        (self.project / "mod.json").write_text(json.dumps({"kind": "sn2_content_mod_project", "plugin_name": "TestMod"}), encoding="utf-8")
        (self.project / "Content/Marker.uasset").write_bytes(b"synthetic editable asset input")
        self.validator = patch("sn2_mcp.content_cooker._validate_project", side_effect=self._validation)
        self.validator.start()
        self.addCleanup(self.validator.stop)
        for relative, raw in {
            "Subnautica2.uproject": b'{"FileVersion":3}',
            "Config/DefaultGame.ini": b"UsePakFile=True\nbUseIoStore=True\n",
            "Releases/SN2Base/Windows/AssetRegistry.bin": b"synthetic base registry",
            "Binaries/Win64/Subnautica2Editor.target": json.dumps({"Version": {"MajorVersion": 5, "MinorVersion": 6, "PatchVersion": 1}}).encode(),
            "Plugins/Alpakit/Source/Alpakit.Automation/PackagePlugin.cs": b"source provenance",
            "Automation/Subnautica2-131439.usmap": b"mapping evidence",
            "GameInstallDirectory.txt": b"example original path\n",
        }.items():
            target = self.modkit / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        from sn2_mcp.content_cooker import _FMOD_FILES
        for name in _FMOD_FILES:
            target = self.modkit / "Plugins/FMODStudio/Binaries/Win64" / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic native dependency")
        for relative, raw in {
            "Build/Build.version": json.dumps({"MajorVersion": 5, "MinorVersion": 6, "PatchVersion": 1}).encode(),
            "Build/BatchFiles/RunUAT.bat": b"@echo synthetic UAT contract",
            "Binaries/Win64/UnrealEditor-Cmd.exe": b"synthetic commandlet",
        }.items():
            target = self.engine / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        self.engine_file = self.engine / "Binaries/Win64/UnrealEditor-Cmd.exe"
        (self.game / "Content/Paks").mkdir(parents=True)
        (self.game / "Content/Paks/Game.utoc").write_bytes(b"synthetic reference container")

    def _validation(self, root, project_path):
        selected = Path(root) / project_path
        files = []
        for path in sorted(selected.rglob("*")):
            if path.is_file():
                relative = path.relative_to(selected).as_posix()
                role = ("plugin_descriptor" if relative.endswith(".uplugin") else "project_manifest"
                        if relative == "mod.json" else "authored_asset")
                raw = path.read_bytes()
                files.append({"path": relative, "sha256": sha(raw), "bytes": len(raw), "role": role})
        return {"valid": True, "plugin_id": "TestMod", "project_root": str(selected),
                "metadata": {"plugin_name": "TestMod"}, "files": files,
                "authored_assets_present": any(item["role"] == "authored_asset" for item in files),
                "project_source_manifest_sha256": _manifest(files)}

    def cooker(self, runner=None):
        return ContentCooker(self.repository, workspace_root=self.workspace, modkit_root=self.modkit,
                             engine_root=self.engine.parent, game_root=self.game, runner=runner or FixtureRunner(self))

    def test_missing_engine_has_persisted_blocked_receipt_without_runner_or_game_writes(self):
        self.engine_file.rename(self.engine_file.with_suffix(".unavailable"))
        runner = FixtureRunner(self)
        cooker = self.cooker(runner)
        receipt = cooker.build("projects/TestMod")
        self.assertEqual(receipt["state"], "blocked")
        self.assertIn("editor_commandlet", receipt["blocked_reasons"])
        self.assertFalse(receipt["compiled"])
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertEqual(runner.calls, [])
        self.assertEqual(cooker.status(receipt["build_id"])["state"], "blocked")
        self.assertFalse((self.game / "Mods").exists())

    def test_recipe_is_fixed_DLC_contract_and_engine_label_only_warns(self):
        version = self.engine / "Build/Build.version"
        version.write_text('{"MajorVersion":5,"MinorVersion":6,"PatchVersion":2}', encoding="utf-8")
        recipe = self.cooker().recipe("projects/TestMod")
        self.assertTrue(recipe["environment"]["ready_to_attempt_cook"])
        self.assertTrue(recipe["environment"]["warnings"])
        self.assertEqual(recipe["base_release"], "SN2Base")
        self.assertIn("-CookAgainstFixedBase", recipe["cook_options"])
        self.assertIn("-AllowUncookedAssetReferences", recipe["cook_options"])

    def test_absent_fmod_setup_files_are_conditional_warnings_not_unverified_cook_blockers(self):
        for path in (self.modkit / "Plugins/FMODStudio/Binaries/Win64").iterdir():
            path.rename(path.with_name(path.name + ".unavailable"))
        environment = self.cooker().inspect("projects/TestMod")
        self.assertTrue(environment["ready_to_attempt_cook"])
        self.assertFalse(any(item.startswith("fmod/") for item in environment["missing"]))
        self.assertFalse(environment["compiled"])
        self.assertEqual(environment["cooker_compatibility"], "not_runtime_verified")
        by_name = {item["name"]: item for item in environment["fmod"]}
        self.assertEqual(by_name["fmodL.dll"]["requirement_role"], "declared_editor_logging_runtime_dependency")
        self.assertEqual(by_name["fmodL_vc.lib"]["requirement_role"], "conditional_native_rebuild_import_library")
        self.assertEqual(by_name["resonanceaudio.dll"]["requirement_role"], "bank_specific_dependency_not_verified")
        self.assertTrue(all(not item["available"] for item in by_name.values()))
        self.assertTrue(any("FMOD file is absent" in warning for warning in environment["warnings"]))

    def test_cooker_project_checks_preserve_source_workspace_protected_roots(self):
        self.validator.stop()
        protected = SourceWorkspace(self.workspace, protected_roots=(self.project,))
        cooker = ContentCooker(self.repository, workspace_root=self.workspace,
            modkit_root=self.modkit, engine_root=self.engine.parent, game_root=self.game,
            workspace=protected, runner=FixtureRunner(self))
        with self.assertRaises(WorkspaceError):
            cooker.recipe("projects/TestMod")
        with self.assertRaises(WorkspaceError):
            cooker.build("projects/TestMod")
        self.assertFalse((self.workspace / "artifacts/content-builds").exists())

    def test_cooker_rejects_mismatched_source_workspace_root(self):
        with self.assertRaises(ContentCookError) as raised:
            ContentCooker(self.repository, workspace_root=self.workspace, workspace=SourceWorkspace(self.repository))
        self.assertEqual(raised.exception.code, "workspace_mismatch")

    def test_install_root_resolves_only_actual_inner_game_content_directory(self):
        inner = self.game / "Subnautica2"
        inner.mkdir()
        (self.game / "Content").rename(inner / "Content")
        cooker = self.cooker()
        inspection = cooker.inspect("projects/TestMod")
        self.assertEqual(inspection["game_reference_root"], str(inner))
        receipt = cooker.build("projects/TestMod")
        self.assertEqual(receipt["state"], "succeeded")
        self.assertEqual((Path(receipt["staged_modkit_root"]) / "GameInstallDirectory.txt").read_text(), str(inner) + "\n")

    def test_ambiguous_inner_and_outer_game_content_roots_require_exact_selection(self):
        (self.game / "Subnautica2/Content/Paks").mkdir(parents=True)
        with self.assertRaises(ContentCookError) as error:
            self.cooker().inspect()
        self.assertEqual(error.exception.code, "ambiguous_game_reference")

    def test_success_captures_exact_inputs_outputs_raw_reports_and_reuses_containment_runner(self):
        runner = FixtureRunner(self)
        cooker = self.cooker(runner)
        receipt = cooker.build("projects/TestMod", timeout_seconds=5000)
        self.assertEqual(receipt["state"], "succeeded", receipt.get("error"))
        self.assertTrue(receipt["exact_source_compiled"])
        self.assertTrue(receipt["workspace_current_revision_compiled"])
        self.assertTrue(receipt["dependencies_stable"])
        self.assertEqual({item["role"] for item in receipt["outputs"]}, {"plugin_descriptor", "pak", "utoc", "ucas"})
        for item in receipt["inputs"]:
            self.assertEqual(sha(Path(item["source_path"]).read_bytes()), item["sha256"])
        for item in receipt["outputs"]:
            self.assertTrue(Path(item["path"]).is_relative_to(Path(receipt["output_root"])))
            self.assertEqual(sha(Path(item["path"]).read_bytes()), item["sha256"])
        arguments, cwd, timeout = runner.calls[0]
        self.assertIn("BuildCookRun", arguments)
        self.assertIn("-dlcname=TestMod", arguments)
        self.assertIn("-iostore", arguments)
        self.assertNotIn("PackagePlugin", arguments)
        self.assertTrue(Path(cwd).is_relative_to(self.workspace))
        self.assertGreater(timeout, 4000)
        captured = Path(receipt["staged_modkit_root"])
        self.assertEqual((captured / "GameInstallDirectory.txt").read_text(), str(self.game) + "\n")
        self.assertEqual((self.modkit / "GameInstallDirectory.txt").read_bytes(), b"example original path\n")
        self.assertTrue(cooker.read_report(receipt["build_id"])["receipt_matches"])
        self.assertEqual(cooker.status()["build_id"], receipt["build_id"])
        self.assertFalse(receipt["installed_in_game"])

    def test_failed_cook_keeps_full_diagnostic_and_produces_no_selected_package(self):
        receipt = self.cooker(FixtureRunner(self, failure=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "cooker_failed")
        self.assertEqual(receipt["outputs"], [])
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertIn("/TestMod/Marker", receipt["reports"][0]["text"])
        self.assertTrue(receipt["reports"][0]["error_reported"])

    def test_reported_errors_with_zero_exit_do_not_claim_success(self):
        receipt = self.cooker(FixtureRunner(self, output_error=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "cooker_errors")
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertEqual(receipt["outputs"], [])

    def test_missing_container_pair_does_not_claim_package(self):
        receipt = self.cooker(FixtureRunner(self, missing=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "incomplete_cook_output")
        self.assertFalse(receipt["compiled"])
        self.assertEqual(receipt["outputs"], [])

    def test_current_workspace_change_does_not_change_captured_revision(self):
        receipt = self.cooker(FixtureRunner(self, mutate=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "succeeded")
        self.assertTrue(receipt["exact_source_compiled"])
        self.assertFalse(receipt["workspace_current_revision_compiled"])
        self.assertEqual((Path(receipt["staged_source_root"]) / "Content/Marker.uasset").read_bytes(), b"synthetic editable asset input")

    def test_worker_exit_unconfirmed_blocks_success_and_records_cleanup(self):
        receipt = self.cooker(FixtureRunner(self, cleanup=False)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "cleanup_unconfirmed")
        self.assertFalse(receipt["cleanup"]["tree_exit_confirmed"])
        self.assertEqual(receipt["outputs"], [])

    def test_added_dependency_is_detected_by_complete_post_cook_inventory(self):
        receipt = self.cooker(FixtureRunner(self, dependency_addition=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "inputs_changed")
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertFalse(receipt["dependencies_stable"])
        self.assertTrue(all(not item["authoritative_cook_output"] for item in receipt["outputs"]))

    def test_added_captured_source_is_detected_by_complete_source_inventory(self):
        receipt = self.cooker(FixtureRunner(self, source_addition=True)).build("projects/TestMod")
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(receipt["failure_code"], "inputs_changed")
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertFalse(receipt["staged_source_stable"])

    def test_missing_authored_assets_blocks_useful_cook(self):
        (self.project / "Content/Marker.uasset").rename(self.project / "Marker.reference")
        original = self._validation
        with patch("sn2_mcp.content_cooker._validate_project", side_effect=lambda *args: {**original(*args), "authored_assets_present": False}):
            receipt = self.cooker().build("projects/TestMod")
        self.assertEqual(receipt["state"], "blocked")
        self.assertIn("authored_content_assets", receipt["blocked_reasons"])

    def test_changed_report_is_reported_by_read_and_never_silently_truncated(self):
        cooker = self.cooker()
        receipt = cooker.build("projects/TestMod")
        raw = Path(receipt["stages"]["cook"]["stdout"]["path"])
        raw.write_bytes(b"changed report\n")
        result = cooker.read_report(receipt["build_id"])
        self.assertFalse(result["receipt_matches"])
        self.assertEqual(result["text"], "changed report\n")
        self.assertFalse(result["truncated"])

    def test_invalid_path_and_deadline_do_not_start_worker(self):
        runner = FixtureRunner(self)
        cooker = self.cooker(runner)
        for timeout in (0, -1, True, float("inf")):
            with self.assertRaises(Exception):
                cooker.build("projects/TestMod", timeout_seconds=timeout)
        for selected in ("../TestMod", "C:/TestMod", "projects\\TestMod"):
            with self.assertRaises(Exception):
                cooker.recipe(selected)
        with self.assertRaises(ContentCookError):
            cooker.status("../receipt")
        self.assertEqual(runner.calls, [])

    def test_cook_receipt_integrates_with_real_content_package_source_contract(self):
        from sn2_mcp.content_projects import ContentProjects
        from sn2_mcp.workspace import SourceWorkspace
        template_root = Path(__file__).resolve().parents[1] / "development/mod_templates/content_plugin"
        shutil.copytree(template_root, self.repository / "development/mod_templates/content_plugin")
        projects = ContentProjects(SourceWorkspace(self.workspace), self.repository)
        projects.create("projects/Integrated", "Integrated")
        (self.workspace / "projects/Integrated/Content/Marker.uasset").write_bytes(b"synthetic authoring input; not actual UE asset")

        def runner(arguments, cwd, timeout, stdout, stderr):
            stage = Path(next(item.split("=", 1)[1] for item in arguments if item.startswith("-stagingdirectory=")))
            paks = stage / "Subnautica2/Plugins/Integrated/Content/Paks/Windows"
            paks.mkdir(parents=True)
            for suffix in ("pak", "utoc", "ucas"):
                (paks / ("Integrated-Windows." + suffix)).write_bytes(("synthetic-" + suffix).encode())
            stdout.write_bytes(b"LogCook: Display: synthetic fixture completed\n")
            stderr.write_bytes(b"")
            return {"state": "succeeded", "exit_code": 0, "tree_exit_confirmed": True, "output_complete": True,
                    "stdout": {"path": str(stdout), "bytes": stdout.stat().st_size, "sha256": sha(stdout.read_bytes())},
                    "stderr": {"path": str(stderr), "bytes": 0, "sha256": sha(b"")}}

        self.validator.stop()
        receipt = self.cooker(runner).build("projects/Integrated")
        self.assertEqual(receipt["state"], "succeeded", receipt.get("error"))
        package = projects.package(receipt["receipt_path"], "artifacts/Integrated-A")
        self.assertTrue(package["valid"])
        verification = projects.verify("artifacts/Integrated-A")
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["container_validation"], "identified_cooker_outputs_not_runtime_mount_verification")
        self.assertEqual(package["source_revision"], receipt["source_manifest_sha256"])

    @unittest.skipUnless(os.name == "nt", "Windows contained batch execution")
    def test_inherited_runner_executes_batch_path_with_spaces_and_confirms_tree_exit(self):
        directory = self.root / "neutral tools with spaces"
        directory.mkdir()
        batch = directory / "synthetic RunUAT.bat"
        batch.write_text("@echo off\r\necho neutral-fixture %~1\r\nexit /b 0\r\n", encoding="ascii")
        stdout, stderr = self.root / "batch.stdout.log", self.root / "batch.stderr.log"
        command = run_build_command([str(batch), "argument with spaces"], directory, 30, stdout, stderr)
        self.assertEqual(command["state"], "succeeded")
        self.assertEqual(command["exit_code"], 0)
        self.assertTrue(command["tree_exit_confirmed"])
        self.assertTrue(command["output_complete"])
        self.assertIn(b"neutral-fixture argument with spaces", stdout.read_bytes())


if __name__ == "__main__":
    unittest.main()
