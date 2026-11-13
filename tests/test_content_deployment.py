"""Isolated content deployment checks; no installed game is changed."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sn2_mcp.content_deployment import ContentDeployment
from sn2_mcp.content_projects import ContentProjectError
from sn2_mcp.development import DevelopmentError


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


class RuntimeFixture:
    def __init__(self, repository, game):
        self.repository = repository
        self.game = game
        self.snapshot = {"processes": [], "running": False, "source": "native_process_handle_snapshot"}
        self.calls = 0
        self.on_snapshot = None

    def process_state(self):
        self.calls += 1
        if self.on_snapshot:
            self.on_snapshot(self.calls)
        return copy.deepcopy(self.snapshot)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.central = self.root / "central"
        self.repository = self.central / "Subnautica 2 MCP"
        self.repository.mkdir(parents=True)
        self.game = self.root / "game"
        (self.game / "Subnautica2").mkdir(parents=True)
        self.package = self.root / "package"
        self.package.mkdir()
        self.plugin = "ContentProbe"
        self.runtime = RuntimeFixture(self.repository, self.game)
        self.report = self.make_package(b"version A")
        self.service = ContentDeployment(self.runtime, verifier=self.verify, central_root=self.central)
        self.destination = self.game / "Subnautica2" / "Mods" / self.plugin
        self.verify_calls = 0

    def make_package(self, payload):
        descriptor = (self.plugin + "/" + self.plugin + ".uplugin")
        contents = {descriptor: b'{"FileVersion":3,"CanContainContent":true}',
                    self.plugin + "/Content/Paks/Windows/probe.pak": payload + b" pak",
                    self.plugin + "/Content/Paks/Windows/probe.utoc": payload + b" utoc",
                    self.plugin + "/Content/Paks/Windows/probe.ucas": payload + b" ucas",
                    "README.md": b"Package provenance\n"}
        entries = []
        for name, raw in contents.items():
            path = self.package / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            entries.append({"path": name, "sha256": sha(raw), "bytes": len(raw), "role": path.suffix.lstrip(".")})
        metadata = {"kind": "sn2_content_mod_package", "schema": 1, "files": entries, "plugin_id": self.plugin}
        raw = (json.dumps(metadata) + "\n").encode()
        (self.package / "manifest.json").write_bytes(raw)
        return {"valid": True, "compiled": True, "package_root": str(self.package),
                "plugin_id": self.plugin, "files": entries, "metadata": metadata,
                "manifest_sha256": sha(raw),
                "activation": {"plugin_name": self.plugin, "mount_root": "/" + self.plugin + "/",
                               "entry_class": "/ContentProbe/BP_Probe.BP_Probe_C",
                               "replacement_boundary": "game_restart"}}

    def verify(self, package_path):
        self.verify_calls += 1
        if package_path != "packages/Probe":
            raise DevelopmentError("invalid_package", "Unexpected package fixture")
        return copy.deepcopy(self.report)

    def install(self, **kwargs):
        return self.service.install("packages/Probe", self.report["manifest_sha256"], **kwargs)

    def assert_error(self, code, function, *args, **kwargs):
        with self.assertRaises(DevelopmentError) as captured:
            function(*args, **kwargs)
        self.assertEqual(captured.exception.code, code)
        return captured.exception

    def test_install_uses_inner_mods_and_separates_runtime_states(self):
        receipt = self.install()
        self.assertEqual(receipt["state"], "installed")
        self.assertTrue(receipt["installed"])
        self.assertFalse(receipt["outcome_unknown"])
        self.assertTrue((self.destination / (self.plugin + ".uplugin")).is_file())
        self.assertFalse((self.destination / "README.md").exists())
        self.assertFalse((self.game / "Mods").exists())
        self.assertEqual(self.verify_calls, 2)
        self.assertGreaterEqual(self.runtime.calls, 3)
        for key in ("plugin_discovery", "plugin_mount", "asset_registration", "blueprint_activity"):
            self.assertEqual(receipt[key], "not_performed")
        status = self.service.status(self.plugin)
        self.assertTrue(status["identified_installation"])
        self.assertEqual(status["installed_inventory_sha256"], receipt["installed_inventory_sha256"])
        self.assertEqual(self.service.status()["plugins"][0]["plugin_id"], self.plugin)

    def test_unknown_destination_is_preserved(self):
        self.destination.mkdir(parents=True)
        marker = self.destination / "unknown.txt"
        marker.write_bytes(b"preserve")
        self.assert_error("unknown_installation", self.install)
        self.assertEqual(marker.read_bytes(), b"preserve")
        self.assertFalse(self.service.records.exists())

    def test_manifest_hash_and_payload_changes_are_rejected(self):
        self.assert_error("package_changed", self.service.install, "packages/Probe", "0" * 64)
        payload = self.package / self.plugin / "Content/Paks/Windows/probe.ucas"
        payload.write_bytes(b"corruption")
        error = self.assert_error("package_changed", self.install)
        self.assertFalse(error.outcome_unknown)
        self.assertFalse(self.destination.exists())
        self.assertEqual(error.receipt["state"], "failed_without_installation")
        self.assertTrue(Path(error.receipt["moves"][-1]["destination"]).is_dir())
        self.assertFalse(any(self.game.joinpath("Subnautica2").glob(".sn2-mcp-content-stage-*")))

    def test_dll_payload_and_traversal_are_rejected(self):
        self.report["files"].append({"path": self.plugin + "/Binaries/Win64/mod.dll",
                                     "sha256": sha(b"dll"), "bytes": 3, "role": "dll"})
        self.assert_error("unsupported_payload", self.install)
        self.report["files"].pop()
        self.report["files"].append({"path": self.plugin + "/../escape.pak",
                                     "sha256": sha(b"x"), "bytes": 1, "role": "pak"})
        self.assert_error("unsafe_path", self.install)
        self.assertFalse(self.destination.exists())

    def test_running_or_unconfirmed_game_state_blocks_files(self):
        self.runtime.snapshot = {"processes": [{"pid": 123, "creation_time": "134127648000000000",
                                                 "executable": str(self.game / "Subnautica2.exe")}],
                                 "running": True, "source": "native_process_handle_snapshot"}
        self.assert_error("game_running", self.install)
        self.runtime.snapshot["running"] = False
        self.assert_error("unconfirmed_process_state", self.install)
        self.runtime.snapshot = {"processes": [], "running": False, "source": "windows_process_snapshot"}
        self.assert_error("unconfirmed_process_state", self.install)
        self.assertFalse(self.destination.exists())

    def test_changed_installed_inventory_or_wrong_expected_hash_blocks_replacement(self):
        receipt = self.install()
        self.assert_error("expected_installation_required", self.install)
        self.assert_error("installation_changed", self.install, expected_installed_inventory_sha256="0" * 64)
        (self.destination / "extra").mkdir()
        self.assert_error("installation_changed", self.install,
                          expected_installed_inventory_sha256=receipt["installed_inventory_sha256"])
        self.assertFalse(self.service.status(self.plugin)["identified_installation"])

    def test_replacement_moves_previous_revision_to_trash(self):
        old = self.install()
        self.report = self.make_package(b"version B")
        new = self.install(expected_installed_inventory_sha256=old["installed_inventory_sha256"])
        self.assertEqual(new["operation"], "replace")
        self.assertEqual(new["previous_operation_id"], old["operation_id"])
        trash = Path(new["moves"][0]["destination"])
        self.assertTrue(trash.is_relative_to(self.central / ".trash"))
        self.assertEqual((trash / "Content/Paks/Windows/probe.pak").read_bytes(), b"version A pak")
        self.assertEqual((self.destination / "Content/Paks/Windows/probe.pak").read_bytes(), b"version B pak")
        self.assertNotEqual(new["installed_inventory_sha256"], old["installed_inventory_sha256"])
        self.assertTrue(self.service.status(self.plugin)["identified_installation"])

    def test_uninstall_requires_current_hash_and_preserves_other_mods(self):
        installed = self.install()
        unrelated = self.destination.parent / "Unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_bytes(b"keep")
        self.assert_error("installation_changed", self.service.uninstall, self.plugin, "0" * 64)
        removed = self.service.uninstall(self.plugin, installed["installed_inventory_sha256"])
        self.assertEqual(removed["state"], "removed")
        self.assertFalse(removed["outcome_unknown"])
        self.assertFalse(self.destination.exists())
        self.assertEqual((unrelated / "keep.txt").read_bytes(), b"keep")
        self.assertTrue(Path(removed["moves"][0]["destination"]).is_dir())
        self.assertFalse(self.service.status(self.plugin)["installed_files"])
        self.assert_error("installation_changed", self.service.uninstall, self.plugin,
                          installed["installed_inventory_sha256"])

    def test_game_start_after_staging_preserves_previous_installation(self):
        old = self.install()
        self.report = self.make_package(b"version B")
        first = self.runtime.calls

        def start(calls):
            if calls == first + 2:
                self.runtime.snapshot.update({"running": True, "processes": [{"pid": 9}]})

        self.runtime.on_snapshot = start
        error = self.assert_error("game_running", self.install,
                                  expected_installed_inventory_sha256=old["installed_inventory_sha256"])
        self.assertFalse(error.outcome_unknown)
        self.assertEqual((self.destination / "Content/Paks/Windows/probe.pak").read_bytes(), b"version A pak")
        self.assertEqual(self.service.status(self.plugin)["receipt"]["operation_id"], old["operation_id"])

    def test_game_start_after_publish_retains_unresolved_receipt(self):
        def start(calls):
            if calls == 4:
                self.runtime.snapshot.update({"running": True, "processes": [{"pid": 9}]})

        self.runtime.on_snapshot = start
        error = self.assert_error("game_running", self.install)
        self.assertTrue(error.outcome_unknown)
        self.assertTrue(self.destination.exists())
        self.assertEqual(error.receipt["state"], "deployment_unresolved")
        self.assertTrue(error.receipt["failure_receipt"]["process_snapshot"]["running"])
        self.runtime.snapshot.update({"running": False, "processes": []})
        self.runtime.on_snapshot = None
        repeated = self.assert_error("deployment_unresolved", self.install)
        self.assertTrue(repeated.outcome_unknown)

    def test_partial_cross_volume_move_preserves_receipt_and_never_retries(self):
        old = self.install()

        def partial(source, destination):
            Path(destination).mkdir(parents=True)
            (Path(destination) / "partial.txt").write_bytes(b"incomplete copy")
            raise OSError("synthetic interrupted copy")

        with patch("sn2_mcp.content_deployment.shutil.move", side_effect=partial):
            error = self.assert_error("access_error", self.service.uninstall, self.plugin,
                                      old["installed_inventory_sha256"])
        self.assertTrue(error.outcome_unknown)
        self.assertTrue(self.destination.exists())
        self.assertEqual(error.receipt["moves"][0]["state"], "requested")
        self.assertTrue(Path(error.receipt["moves"][0]["destination"]).exists())
        self.assert_error("deployment_unresolved", self.service.uninstall, self.plugin,
                          old["installed_inventory_sha256"])

    def test_failed_staging_cleanup_records_unknown_state_durably(self):
        payload = self.package / self.plugin / "Content/Paks/Windows/probe.ucas"
        payload.write_bytes(b"corruption")

        def partial(source, destination):
            Path(destination).mkdir(parents=True)
            (Path(destination) / "partial.txt").write_bytes(b"incomplete copy")
            raise OSError("synthetic interrupted cleanup")

        with patch("sn2_mcp.content_deployment.shutil.move", side_effect=partial):
            error = self.assert_error("package_changed", self.install)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(error.receipt["state"], "deployment_unresolved")
        recorded = self.service.status(self.plugin)["receipt"]
        self.assertTrue(recorded["outcome_unknown"])
        self.assertEqual(recorded["receipt_persistence_or_cleanup"], "incomplete")
        self.report = self.make_package(b"repair")
        self.assert_error("deployment_unresolved", self.install)

    def test_failure_handler_does_not_overwrite_a_new_current_operation(self):
        previous = self.install()
        current = copy.deepcopy(previous)
        current["operation_id"] = "synthetic-newer-operation"
        self.service._write(self.service.records / "current" / (self.plugin + ".json"), current)
        error = self.assert_error("synthetic_failure", self.service._failed,
                                  DevelopmentError("synthetic_failure", "Original operation failed"),
                                  previous, None, False, None)
        self.assertTrue(error.outcome_unknown)
        self.assertEqual(self.service.status(self.plugin)["receipt"]["operation_id"], current["operation_id"])

    def test_game_start_after_final_hash_is_checked_before_rename(self):
        original = self.service._inventory
        stage_reads = 0

        def inventory(directory, boundary):
            nonlocal stage_reads
            result = original(directory, boundary)
            if directory.name.startswith(".sn2-mcp-content-stage-"):
                stage_reads += 1
                if stage_reads == 2:
                    self.runtime.snapshot.update({"running": True, "processes": [{"pid": 9}]})
            return result

        with patch.object(self.service, "_inventory", side_effect=inventory):
            error = self.assert_error("game_running", self.install)
        self.assertFalse(self.destination.exists())
        self.assertTrue(Path(error.receipt["moves"][-1]["destination"]).is_dir())

    def test_interrupted_publish_has_durable_unknown_outcome(self):
        rename = Path.rename

        def interrupt(path, target):
            if path.name.startswith(".sn2-mcp-content-stage-"):
                raise KeyboardInterrupt("synthetic interruption at publication")
            return rename(path, target)

        with patch.object(Path, "rename", interrupt), self.assertRaises(KeyboardInterrupt):
            self.install()
        receipt = self.service.status(self.plugin)["receipt"]
        self.assertEqual(receipt["state"], "publish_requested")
        self.assertTrue(receipt["outcome_unknown"])
        self.assertTrue(Path(receipt["stage"]).is_dir())
        self.assert_error("deployment_unresolved", self.install)

    def test_package_change_after_staging_is_rejected(self):
        original = self.verify

        def change(package_path):
            report = original(package_path)
            if self.verify_calls == 2:
                report["activation"]["entry_class"] = "/ContentProbe/BP_Other.BP_Other_C"
            return report

        self.service.verifier = change
        error = self.assert_error("package_changed", self.install)
        self.assertFalse(error.outcome_unknown)
        self.assertFalse(self.destination.exists())

    def test_second_verifier_exception_keeps_failure_receipt_and_cleans_stage(self):
        original = self.verify

        def change(package_path):
            result = original(package_path)
            if self.verify_calls == 2:
                raise ContentProjectError("package_inventory_mismatch", "A package input changed")
            return result

        self.service.verifier = change
        error = self.assert_error("package_inventory_mismatch", self.install)
        self.assertFalse(error.outcome_unknown)
        self.assertFalse(self.destination.exists())
        self.assertEqual(error.receipt["state"], "failed_without_installation")
        self.assertFalse(Path(error.receipt["stage"]).exists())

    def test_actual_content_package_verifier_composes_with_deployment(self):
        # This fixture represents recorded cooker output identities. It neither
        # runs a cooker nor establishes executable Blueprint or mount validity.
        from tests.test_content_projects import ContentProjectTests
        fixture = ContentProjectTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        receipt_path, _ = fixture.cook_receipt()
        report = fixture.service.package(str(receipt_path), "artifacts/TestMod-A")
        self.service.verifier = fixture.service.verify
        installed = self.service.install("artifacts/TestMod-A", report["manifest_sha256"])
        self.assertEqual(installed["plugin_id"], "TestMod")
        self.assertTrue(self.service.status("TestMod")["identified_installation"])
        self.assertEqual(len([entry for entry in installed["installed_inventory"]
                              if entry["type"] == "file"]), 4)

    def test_directory_junction_or_symlink_is_rejected(self):
        external = self.root / "external"
        external.mkdir()
        try:
            self.destination.parent.mkdir(parents=True)
            self.destination.symlink_to(external, target_is_directory=True)
        except OSError:
            self.skipTest("The environment cannot create directory symbolic links")
        # Unknown destination fails before ownership inspection can authorize writes.
        self.assert_error("unknown_installation", self.install)
        self.assertEqual(list(external.iterdir()), [])
        self.assert_error("unsafe_path", self.service.status, self.plugin)


if __name__ == "__main__":
    unittest.main()
