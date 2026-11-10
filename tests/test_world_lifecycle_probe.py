"""Изолированные проверки наблюдения собственного PostInitialize без вызова callback."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare_world_lifecycle_probe")
live = load("probe_world_lifecycle_mcp")
base = load("probe_lifecycle_mcp")


class WorldLifecycleCalls:
    def __init__(self, *, exists=True, trigger_after=1, invalid=None, race=False):
        self.exists = exists
        self.trigger_after = trigger_after
        self.invalid = invalid
        self.race = race
        self.race_triggered = False
        self.samples = 0
        self.calls = []
        self.targets = {
            "obj-1": {"handle": "obj-1", "name": "Default__" + live.HOST,
                      "path": "/Script/Angelscript.Default__" + live.HOST, "class": live.HOST},
            "obj-2": {"handle": "obj-2", "name": live.HOST + "_0",
                      "path": "/Engine/Transient.World_0." + live.HOST + "_0", "class": live.HOST},
        }

    def count(self, target):
        if target["name"].startswith("Default__"):
            return 0
        return int(self.race_triggered or self.samples >= self.trigger_after)

    async def call(self, name, args):
        self.calls.append((name, copy.deepcopy(args)))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "owner"}
        if name == "g1r_find_objects":
            if args["class_query"] != live.HOST:
                raise AssertionError("Проверка запросила посторонний класс")
            self.samples += 1
            items = [copy.deepcopy(self.targets["obj-1"])]
            if self.exists:
                items.append(copy.deepcopy(self.targets["obj-2"]))
            return {"items": items, "truncated": False}
        handle = args.get("handle", args.get("targets", [{}])[0].get("handle"))
        target = self.targets[handle]
        if name == "g1r_object_functions":
            result = {
                "scan_complete": True, "truncated": False,
                "items": [{"name": method, "parameter_size": 4, "parameters_truncated": False,
                           "parameters": [{"name": "ReturnValue", "return": True, "reference": False,
                                           "array_dim": 1, "type": "IntProperty", "element_size": 4,
                                           "offset": 0}]}
                          for method in ("ReadMarker", live.READ_METHOD)],
            }
            if self.invalid == "signature_incomplete":
                result.pop("scan_complete")
            return result
        if name == "g1r_invoke":
            method = args["function"]
            if method not in {"ReadMarker", live.READ_METHOD} or args["arguments"]:
                raise AssertionError("Callback или мутация вызваны через MCP")
            if method == "ReadMarker":
                return {"executed": True, "result": live.MARKER}
            if self.race and handle == "obj-2":
                self.race_triggered = True
            value = self.count(target)
            if self.invalid == "counter_decreased" and handle == "obj-2":
                value = 0
            return {"executed": True, "result": value}
        if name == "g1r_snapshot":
            field = {"name": live.COUNT, "type": "IntProperty", "supported": True,
                     "value": self.count(target)}
            if self.invalid == "value_missing":
                field.pop("value")
            if self.invalid == "value_boolean":
                field["value"] = False
            if self.invalid == "value_negative":
                field["value"] = -1
            result = {
                "source": "live_native", "session_id": "owner", "consistency": "single_tick_sequential",
                "partial": False, "truncated": False,
                "objects": [{**target, "partial": False, "truncated": False, "properties": [field]}],
            }
            if self.invalid == "wrong_owner":
                result["session_id"] = "other-owner"
            if self.invalid == "source_missing":
                result.pop("source")
            if self.invalid == "object_partial_missing":
                result["objects"][0].pop("partial")
            return result
        raise AssertionError((name, args))


class WorldLifecycleProbeTests(unittest.TestCase):
    def run_calls(self, fixture, *, wait_seconds=.3):
        current = [0.0]

        async def sleep(seconds):
            current[0] += seconds

        return asyncio.run(live.exercise(fixture.call, wait_seconds=wait_seconds,
                                         clock=lambda: current[0], sleep=sleep))

    def test_prepare_copies_own_source_without_runtime_claim_or_existing_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "world-lifecycle"
            result = prepare.prepare_probe(workspace, repository_root=repository)
            source = workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE
            original = source.read_bytes()
            self.assertEqual(original, (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertEqual(result["expected"]["callback"], "PostInitialize")
            self.assertEqual(result["expected"]["marker"], 111)
            self.assertEqual(result["expected"]["verification"], "unverified_until_automatic_callback_observed")
            with self.assertRaises(prepare.PackageError) as captured:
                prepare.prepare_probe(workspace, repository_root=repository)
            self.assertEqual(captured.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), original)

    def test_live_count_proves_post_initialize_only_with_no_manual_callback(self):
        fixture = WorldLifecycleCalls()
        report = self.run_calls(fixture)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["callback"], "PostInitialize")
        self.assertEqual(report["world"]["status"], "automatic_post_initialize_confirmed")
        self.assertEqual(report["world"]["cdo_counts"], {live.COUNT: 0})
        self.assertEqual(report["world"]["instances"][0]["counts"], {live.COUNT: 1})
        self.assertFalse(report["gameplay_ready_confirmed"])
        self.assertFalse(report["callbacks_invoked_manually"])
        self.assertFalse(report["world_loaded"])
        self.assertTrue(all(name in {"g1r_native_status", "g1r_find_objects", "g1r_object_functions",
                                     "g1r_snapshot", "g1r_invoke"} for name, _ in fixture.calls))

    def test_only_cdo_or_zero_counter_returns_not_tested_without_false_success(self):
        for fixture in (WorldLifecycleCalls(exists=False), WorldLifecycleCalls(trigger_after=99)):
            with self.subTest(exists=fixture.exists):
                report = self.run_calls(fixture)
                self.assertEqual(report["status"], "not_tested_not_triggered")
                self.assertFalse(report["gameplay_ready_confirmed"])
                self.assertLessEqual(report["samples"], 5)
                self.assertFalse(report["callbacks_invoked_manually"])

    def test_delayed_callback_is_observed_with_bounded_polling(self):
        fixture = WorldLifecycleCalls(trigger_after=2)
        report = self.run_calls(fixture)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["samples"], 2)
        self.assertEqual(report["world"]["instances"][0]["counts"][live.COUNT], 1)

    def test_callback_between_snapshot_and_reader_is_rechecked(self):
        fixture = WorldLifecycleCalls(trigger_after=99, race=True)
        report = self.run_calls(fixture, wait_seconds=0)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["world"]["instances"][0]["counts"][live.COUNT], 1)
        live_snapshots = [args for name, args in fixture.calls
                          if name == "g1r_snapshot" and args["targets"][0]["handle"] == "obj-2"]
        self.assertEqual(len(live_snapshots), 2)

    def test_incomplete_metadata_and_invalid_values_are_errors_not_zero(self):
        for invalid in ("signature_incomplete", "value_missing", "value_boolean", "value_negative",
                        "wrong_owner", "source_missing", "object_partial_missing"):
            with self.subTest(invalid=invalid), self.assertRaises(base.ProbeError):
                self.run_calls(WorldLifecycleCalls(invalid=invalid), wait_seconds=0)

    def test_counter_decrease_is_error_and_never_claims_lifecycle(self):
        with self.assertRaises(base.ProbeError) as captured:
            self.run_calls(WorldLifecycleCalls(invalid="counter_decreased"), wait_seconds=0)
        self.assertEqual(captured.exception.code, "probe_counter_mismatch")

    def test_reused_engine_probe_globals_are_not_changed(self):
        original = (base.TOOL, copy.deepcopy(base.CLASSES), set(base.READ_METHODS))
        self.run_calls(WorldLifecycleCalls())
        self.assertEqual((base.TOOL, base.CLASSES, base.READ_METHODS), original)


if __name__ == "__main__":
    unittest.main()
