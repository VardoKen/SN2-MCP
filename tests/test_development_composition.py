"""Составной development-сеанс: реальные файлы и manifest, подменён только процессный исполнитель."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from g1r_mcp.development import CommandResult, DevelopmentError, DevelopmentService
from g1r_mcp.workspace import WorkspaceError


class DevelopmentCompositionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="g1r-composition-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = self.root / "repository"
        (self.repository / "tools").mkdir(parents=True)
        for name in ("game_process_state.ps1", "start_g1r_development.ps1", "stop_g1r_development.ps1"):
            (self.repository / "tools" / name).write_text("# isolated executor only\n", encoding="utf-8")
        self.workspace = self.root / "workspace"
        self.modkit = self.root / "modkit"
        self.game = self.root / "game"
        for directory in (self.workspace, self.modkit, self.game):
            directory.mkdir()
        self.main_path = "user sources/WorkingMod"
        self.helper_path = "bridge helpers/G1R_DevelopmentHelpers"
        self.main = self.make_package(self.main_path, "WorkingMod", b"\xef\xbb\xbfclass UWork {\r\n}\r\n")
        self.helper = self.make_package(self.helper_path, "G1R_DevelopmentHelpers",
                                        "class UHelper { /* Ёж 🐉 */ }\n".encode("utf-8"))
        self.calls = []
        self.processes = []
        self.receipt_transform = None
        self.service = DevelopmentService(repository_root=self.repository, workspace_root=self.workspace,
                                          modkit_root=self.modkit, game_root=self.game,
                                          executor=self.execute, powershell="isolated-powershell")
        self.addCleanup(self.service.close)
        self.identity = {"pid": 123, "creation_time": "2026-10-30T01:02:03.1234567Z",
                         "executable": str(self.service.inner_executable)}

    def make_package(self, relative_path, name, script=b"class UAdditional {}\n"):
        package = self.workspace / relative_path
        (package / "Script").mkdir(parents=True)
        (package / "Script" / "Entry.as").write_bytes(script)
        (package / "Content" / "nested").mkdir(parents=True)
        (package / "Content" / "nested" / "asset.bin").write_bytes(bytes(range(256)) + b"\x00\xff")
        (package / "Content" / "empty.bin").write_bytes(b"")
        (package / "GothicMod.json").write_text(json.dumps({
            "name": name, "title": "Изолированный пакет", "author": "Тест", "modVersion": "1.0.0"
        }, ensure_ascii=False), encoding="utf-8")
        return package

    def execute(self, arguments, timeout):
        self.calls.append((list(arguments), timeout))
        script = Path(arguments[arguments.index("-File") + 1]).name
        if script == "game_process_state.ps1":
            result = {"processes": self.processes}
        elif script == "start_g1r_development.ps1":
            names = arguments[arguments.index("-ModsCsv") + 1].split(",")
            result = {"state": "launch_requested", "package_name": None, "mods": names,
                      "mods_directory": arguments[arguments.index("-ModsDirectory") + 1]}
            if self.receipt_transform is not None:
                result = self.receipt_transform(result)
        elif script == "stop_g1r_development.ps1":
            result = {"state": "stopped", "process": self.identity}
        else:
            self.fail("Неожиданный процессный инструмент: " + script)
        return CommandResult(0, json.dumps(result, ensure_ascii=False))

    def snapshot(self, directory):
        return {item.relative_to(directory).as_posix(): item.read_bytes() if item.is_file() else None
                for item in directory.rglob("*")}

    def recipe(self, companions=None, *, enable_ingame_tools=False):
        return self.service.recipe(self.main_path,
                                   companion_packages=[self.helper_path] if companions is None else companions,
                                   enable_ingame_tools=enable_ingame_tools)

    def start(self, recipe=None, companions=None, *, enable_ingame_tools=False):
        selected = [self.helper_path] if companions is None else companions
        recipe = recipe or self.recipe(selected, enable_ingame_tools=enable_ingame_tools)
        return self.service.start(self.main_path, recipe["manifest"]["manifest_sha256"],
                                  companion_packages=selected, enable_ingame_tools=enable_ingame_tools)

    def launch_calls(self):
        return [arguments for arguments, _ in self.calls
                if Path(arguments[arguments.index("-File") + 1]).name == "start_g1r_development.ps1"]

    def test_recipe_is_read_only_and_aggregate_hash_covers_each_package_and_launch_options(self):
        before = self.snapshot(self.workspace)
        recipe = self.recipe()
        manifest = recipe["manifest"]
        self.assertEqual(self.snapshot(self.workspace), before)
        self.assertFalse((self.workspace / "artifacts").exists())
        self.assertEqual(manifest["composition"], "explicit_companion_packages")
        self.assertEqual(manifest["selected_mods"], ["WorkingMod", "G1R_DevelopmentHelpers"])
        self.assertEqual([item["role"] for item in manifest["packages"]], ["main", "companion"])
        self.assertEqual([item["package_path"] for item in manifest["packages"]],
                         [self.main_path, self.helper_path])
        self.assertEqual(manifest["total_bytes"], sum(item["total_bytes"] for item in manifest["packages"]))
        self.assertIsNone(recipe["launch"]["mods_directory"])
        self.assertEqual(recipe["launch"]["staging_created_by"], "development_start")
        self.assertEqual(recipe["launch"]["edit_target"], "staged_copy_only")
        self.assertFalse(recipe["compiled"])
        self.assertFalse(recipe["launch"]["source_packages_modified"])
        self.assertEqual(len(self.calls), 1)
        enabled = self.recipe(enable_ingame_tools=True)
        self.assertNotEqual(enabled["manifest"]["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(enabled["manifest"]["packages"][1]["files"], manifest["packages"][1]["files"])
        (self.helper / "Content" / "empty.bin").write_bytes(b"helper changed")
        changed = self.recipe()
        self.assertNotEqual(changed["manifest"]["manifest_sha256"], manifest["manifest_sha256"])
        self.assertEqual(changed["manifest"]["packages"][0], manifest["packages"][0])
        self.assertNotEqual(changed["manifest"]["packages"][1]["manifest_sha256"],
                            manifest["packages"][1]["manifest_sha256"])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_start_copies_packages_from_different_parents_exactly_and_rehashes_each_copy(self):
        recipe = self.recipe()
        original_main = self.snapshot(self.main)
        original_helper = self.snapshot(self.helper)
        result = self.start(recipe)
        stage = result["development_staging"]
        relative = Path(stage["relative_directory"])
        self.assertEqual(relative.parts[:2], ("artifacts", "development-sessions"))
        self.assertEqual(len(relative.parts), 3)
        self.assertRegex(relative.name, r"^[0-9a-f]{32}$")
        stage_root = self.workspace / relative
        packages = stage_root / "packages"
        self.assertEqual(Path(stage["mods_directory"]), packages)
        self.assertEqual({item.name for item in packages.iterdir()}, {"WorkingMod", "G1R_DevelopmentHelpers"})
        for source, copied, original in zip(recipe["manifest"]["packages"], stage["packages"],
                                            (original_main, original_helper)):
            destination = self.workspace / copied["relative_package"]
            self.assertEqual(self.snapshot(destination), original)
            current = self.service._manifest(copied["relative_package"])
            self.assertEqual(current["manifest_sha256"], copied["manifest_sha256_at_launch"])
            self.assertEqual(current["files"], source["files"])
            self.assertEqual(copied["source_manifest_sha256"], source["manifest_sha256"])
            self.assertEqual(copied["source_package_path"], source["package_path"])
            for entry in source["files"]:
                raw = (destination / entry["path"]).read_bytes()
                self.assertEqual(len(raw), entry["bytes"])
                self.assertEqual(hashlib.sha256(raw).hexdigest(), entry["sha256"])
        self.assertEqual(json.loads((stage_root / "session.json").read_text(encoding="utf-8")), stage)
        arguments = self.launch_calls()[0]
        self.assertEqual(arguments[arguments.index("-ModsCsv") + 1], "WorkingMod,G1R_DevelopmentHelpers")
        self.assertEqual(arguments[arguments.index("-ModsDirectory") + 1], str(packages))
        self.assertNotIn("-Mods", arguments)
        self.assertNotIn("-ModsJson", arguments)
        self.assertEqual(self.snapshot(self.main), original_main)
        self.assertEqual(self.snapshot(self.helper), original_helper)
        self.assertEqual(result["state"], "launch_requested")
        self.assertFalse(result["compiled"])
        self.assertFalse(result["game_ready"])
        self.assertFalse(result["source_packages_modified"])
        self.assertEqual([item["relative_package"] for item in result["active_staging_packages"]],
                         [item["relative_package"] for item in stage["packages"]])

    def test_changed_helper_rejects_stale_aggregate_hash_before_process_or_stage_creation(self):
        recipe = self.recipe()
        (self.helper / "Script" / "Entry.as").write_bytes(b"class UChangedHelper {}\n")
        before = len(self.calls)
        with self.assertRaises(DevelopmentError) as captured:
            self.start(recipe)
        self.assertEqual(captured.exception.code, "package_changed")
        self.assertEqual(len(self.calls), before)
        self.assertEqual(self.launch_calls(), [])
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_sources_changed_after_copy_are_rehashed_before_launch(self):
        recipe = self.recipe()
        original_stage = self.service._stage_composition

        def copy_then_change_source(manifest):
            stage = original_stage(manifest)
            (self.helper / "Script" / "Entry.as").write_bytes(b"class UChangedAfterCopy {}\n")
            return stage

        with patch.object(self.service, "_stage_composition", side_effect=copy_then_change_source):
            with self.assertRaises(DevelopmentError) as captured:
                self.start(recipe)
        self.assertEqual(captured.exception.code, "package_changed")
        self.assertEqual(self.launch_calls(), [])
        self.assertEqual(len(list((self.workspace / "artifacts" / "development-sessions").iterdir())), 1)

    def test_staged_bytes_changed_after_copy_are_rehashed_before_launch(self):
        recipe = self.recipe()
        originals = (self.snapshot(self.main), self.snapshot(self.helper))
        original_stage = self.service._stage_composition

        def copy_then_change_stage(manifest):
            stage = original_stage(manifest)
            path = self.workspace / stage["packages"][1]["relative_package"] / "Content" / "empty.bin"
            path.write_bytes(b"unexpected staged change")
            return stage

        with patch.object(self.service, "_stage_composition", side_effect=copy_then_change_stage):
            with self.assertRaises(DevelopmentError) as captured:
                self.start(recipe)
        self.assertEqual(captured.exception.code, "staging_changed")
        self.assertEqual(self.launch_calls(), [])
        self.assertEqual((self.snapshot(self.main), self.snapshot(self.helper)), originals)

    def test_duplicate_paths_and_casefold_names_reject_before_any_executor_call(self):
        self.make_package("other/WORKINGMOD", "WORKINGMOD")
        for companions in ([self.main_path], [self.helper_path, self.helper_path], ["other/WORKINGMOD"]):
            with self.subTest(companions=companions):
                before = len(self.calls)
                with self.assertRaises(DevelopmentError) as captured:
                    self.recipe(companions)
                self.assertEqual(captured.exception.code, "duplicate_package")
                self.assertEqual(len(self.calls), before)
        self.assertFalse((self.workspace / "artifacts").exists())

    def test_five_packages_are_ordered_and_staged(self):
        extras = ["extra/Third", "elsewhere/Fourth", "last/Fifth"]
        for relative in extras:
            self.make_package(relative, Path(relative).name)
        companions = [self.helper_path, *extras]
        recipe = self.recipe(companions)
        self.assertEqual(recipe["manifest"]["selected_mods"],
                         ["WorkingMod", "G1R_DevelopmentHelpers", "Third", "Fourth", "Fifth"])
        result = self.start(recipe, companions)
        self.assertEqual(len(result["active_staging_packages"]), 5)
        arguments = self.launch_calls()[0]
        self.assertEqual(arguments[arguments.index("-ModsCsv") + 1],
                         "WorkingMod,G1R_DevelopmentHelpers,Third,Fourth,Fifth")

    def test_wrong_receipt_names_or_root_leave_launch_outcome_unknown(self):
        transforms = {
            "reordered": lambda result: {**result, "mods": list(reversed(result["mods"]))},
            "missing": lambda result: {**result, "mods": ["WorkingMod"]},
            "extra": lambda result: {**result, "mods": [*result["mods"], "Unexpected"]},
            "string": lambda result: {**result, "mods": ",".join(result["mods"])},
            "null": lambda result: {**result, "mods": None},
            "wrong_root": lambda result: {**result, "mods_directory": str(self.main.parent)},
            "root_missing": lambda result: {**result, "mods_directory": None},
        }
        recipe = self.recipe()
        for label, transform in transforms.items():
            with self.subTest(receipt=label):
                self.receipt_transform = transform
                before = len(self.launch_calls())
                with self.assertRaises(DevelopmentError) as captured:
                    self.start(recipe)
                self.assertEqual(captured.exception.code, "tool_protocol_error")
                self.assertTrue(captured.exception.outcome_unknown)
                self.assertEqual(len(self.launch_calls()), before + 1)
        self.assertEqual(len(list((self.workspace / "artifacts" / "development-sessions").iterdir())),
                         len(transforms))

    def test_stage_supports_sha_checked_editing_and_stop_retains_it_without_touching_sources(self):
        originals = (self.snapshot(self.main), self.snapshot(self.helper))
        result = self.start()
        stage = result["development_staging"]
        active = result["active_staging_packages"][0]
        read = self.service.workspace.read_package_file_exact(active["relative_package"], "Script/Entry.as")
        replacement = read["text"].replace("class UWork", "class UEditedWork")
        written = self.service.workspace.write_package_file(active["relative_package"], "Script/Entry.as",
                                                             replacement, expected_sha256=read["sha256"])
        self.assertTrue(written["written"])
        self.assertFalse(written["created"])
        self.assertEqual(written["sha256"], hashlib.sha256(replacement.encode("utf-8")).hexdigest())
        with self.assertRaises(WorkspaceError) as captured:
            self.service.workspace.write_package_file(active["relative_package"], "Script/Entry.as",
                                                       "class UStaleWrite {}\n", expected_sha256=read["sha256"])
        self.assertEqual(captured.exception.code, "conflict")
        current = self.service.workspace.read_package_file_exact(active["relative_package"], "Script/Entry.as")
        self.assertEqual(current["text"], replacement)
        self.assertNotEqual(current["sha256"], read["sha256"])
        self.assertEqual((self.snapshot(self.main), self.snapshot(self.helper)), originals)
        before_stop = self.snapshot(self.workspace / stage["relative_directory"])
        self.processes = [self.identity]
        stopped = self.service.stop(self.identity)
        self.assertEqual(stopped["state"], "stop_result")
        self.assertFalse(stopped["outcome_unknown"])
        self.assertEqual(self.snapshot(self.workspace / stage["relative_directory"]), before_stop)
        self.assertEqual(stage["retention"], "retain_for_review")
        self.assertEqual((self.snapshot(self.main), self.snapshot(self.helper)), originals)

    def test_repeated_starts_get_distinct_stages_and_preserve_earlier_edits(self):
        recipe = self.recipe()
        first = self.start(recipe)["development_staging"]
        first_file = self.workspace / first["packages"][0]["relative_package"] / "Script" / "Entry.as"
        first_file.write_bytes(b"class UEarlierSessionEdit {}\n")
        before = self.snapshot(self.workspace / first["relative_directory"])
        second = self.start(recipe)["development_staging"]
        self.assertNotEqual(first["relative_directory"], second["relative_directory"])
        self.assertEqual(self.snapshot(self.workspace / first["relative_directory"]), before)
        self.assertEqual((self.workspace / second["packages"][0]["relative_package"] / "Script" / "Entry.as").read_bytes(),
                         (self.main / "Script" / "Entry.as").read_bytes())


if __name__ == "__main__":
    unittest.main()
