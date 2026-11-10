"""Совместимость подготовленного lifecycle-пакета и явного reload/watch контракта."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("world_lifecycle_package_prepare", ROOT / "tools" / "prepare_world_lifecycle_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class WorldLifecyclePackageTests(unittest.TestCase):
    def test_generated_package_preserves_counter_contract_and_declares_explicit_travel(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "world-lifecycle"
            report = prepare.prepare_probe(workspace, repository_root=repository)
            source = workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE
            manifest = json.loads((workspace / prepare.PACKAGE_NAME / "GothicMod.json").read_text(encoding="utf-8"))
            self.assertEqual(report["expected"], {
                "verification": "unverified_until_automatic_callback_observed",
                "class": "G1RMCPWorldPostInitializeProbe", "marker": 111,
                "count": "PostInitializeCount", "read_method": "ReadPostInitializeCount",
                "cdo_count": 0, "callback": "PostInitialize",
            })
            self.assertEqual(source.read_bytes(), (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())
            self.assertEqual(report["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
            self.assertEqual(manifest["name"], prepare.PACKAGE_NAME)
            self.assertEqual(manifest["modVersion"], "1.0.0")
            self.assertFalse(report["game_launched"])
            self.assertFalse(report["compiled"])
            self.assertEqual(report["runtime_validation"], "not_performed")
            self.assertEqual(report["reload_and_watch"], {
                "enabled_by_default": False,
                "probe_option": "--reload-and-watch",
                "methods": {"GetCurrentMapName": "FString", "GetCurrentLoadingScreenType": "ELoadingScreenType",
                            "ReloadCurrentMap": "void"},
                "watched_property": "PostInitializeCount",
                "loading_screen_type": "ELoadingScreenType::BlackScreen",
                "destination": "current_map",
            })


if __name__ == "__main__":
    unittest.main()
