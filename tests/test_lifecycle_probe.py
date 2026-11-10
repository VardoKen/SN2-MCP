"""Изолированные проверки честного наблюдения собственных lifecycle callbacks."""
from __future__ import annotations

import asyncio
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


prepare = load("prepare_lifecycle_probe")
live = load("probe_lifecycle_mcp")


class LifecycleCalls:
    def __init__(self, *, tool_exists=True, trigger_after=1, world_exists=False, missing_value=False):
        self.tool_exists = tool_exists
        self.trigger_after = trigger_after
        self.world_exists = world_exists
        self.missing_value = missing_value
        self.samples = 0
        self.calls = []
        self.targets = {}
        for class_name in live.CLASSES:
            self.targets[class_name + "-cdo"] = {"handle": class_name + "-cdo", "name": "Default__" + class_name,
                "path": "/Script/Angelscript.Default__" + class_name, "class": class_name}
            self.targets[class_name + "-live"] = {"handle": class_name + "-live", "name": class_name + "_0",
                "path": "/Engine/Transient.GameEngine_0." + class_name + "_0", "class": class_name}
        for index, item in enumerate(self.targets.values(), 1):
            item["handle"] = "obj-" + str(index)

    def counts(self, target):
        fields = live.CLASSES[target["class"]]["fields"]
        return {name: (1 if name == "InitializeCount" and not target["name"].startswith("Default__")
                      and self.samples >= self.trigger_after else 0) for name in fields}

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "owner"}
        if name == "g1r_find_objects":
            class_name = args["class_query"]
            if class_name == live.TOOL:
                self.samples += 1
            items = [self.targets[class_name + "-cdo"]]
            if class_name == live.TOOL and self.tool_exists or class_name == live.WORLD and self.world_exists:
                items.append(self.targets[class_name + "-live"])
            return {"items": items, "truncated": False}
        target = next(item for item in self.targets.values() if item["handle"] == args.get("handle", args.get("targets", [{}])[0].get("handle")))
        config = live.CLASSES[target["class"]]
        if name == "g1r_object_functions":
            return {"truncated": False, "scan_complete": True,
                    "items": [{"name": method, "parameter_size": 4, "parameters_truncated": False, "parameters": [
                {"name": "ReturnValue", "return": True, "reference": False, "array_dim": 1, "type": "IntProperty", "element_size": 4, "offset": 0}]}
                for method in ("ReadMarker", *config["fields"].values())]}
        if name == "g1r_invoke":
            method = args["function"]
            if method not in live.READ_METHODS:
                raise AssertionError("Lifecycle callback вызван вручную")
            value = config["marker"] if method == "ReadMarker" else self.counts(target)[next(field for field, reader in config["fields"].items() if reader == method)]
            return {"executed": True, "result": value}
        if name == "g1r_snapshot":
            properties = [{"name": field, "type": "IntProperty", "supported": True, "value": value}
                          for field, value in self.counts(target).items()]
            if self.missing_value:
                properties[0].pop("value")
            return {"source": "live_native", "session_id": "owner", "consistency": "single_tick_sequential",
                    "partial": False, "truncated": False, "objects": [{**target, "partial": False, "truncated": False, "properties": properties}]}
        raise AssertionError(name)


class LifecycleProbeTests(unittest.TestCase):
    def run_calls(self, fixture):
        current = [0.0]

        async def sleep(seconds):
            current[0] += seconds

        return asyncio.run(live.exercise(fixture.call, wait_seconds=0.3, clock=lambda: current[0], sleep=sleep))

    def test_prepare_has_closed_tool_and_no_runtime_claim(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "lifecycle"
            result = prepare.prepare_probe(workspace, repository_root=repository)
            self.assertTrue(result["startup_parameters"]["IngameTools"])
            self.assertFalse(result["compiled"])
            self.assertEqual(result["expected"]["verification"], "unverified_until_automatic_callback_observed")
            self.assertEqual((workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE).read_bytes(), (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())

    def test_later_live_callback_proves_only_tool_world_not_tested(self):
        fixture = LifecycleCalls(trigger_after=2)
        report = self.run_calls(fixture)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["tool"]["status"], "automatic_initialize_confirmed")
        self.assertEqual(report["samples"], 2)
        self.assertEqual(report["world"]["status"], "not_tested")
        self.assertFalse(report["world"]["ready_for_play_confirmed"])
        self.assertFalse(report["callbacks_invoked_manually"])
        self.assertTrue(all(args["function"] in live.READ_METHODS for name, args in fixture.calls if name == "g1r_invoke"))

    def test_only_cdo_or_zero_live_counter_never_claims_completed(self):
        for fixture in (LifecycleCalls(tool_exists=False), LifecycleCalls(trigger_after=99)):
            with self.subTest(fixture=fixture):
                report = self.run_calls(fixture)
            self.assertEqual(report["status"], "not_triggered")
            self.assertLessEqual(report["samples"], 5)

    def test_missing_value_is_error_not_default_zero(self):
        with self.assertRaises(live.ProbeError) as captured:
            self.run_calls(LifecycleCalls(missing_value=True))
        self.assertEqual(captured.exception.code, "probe_counter_invalid")

    def test_callbacks_and_default_object_cannot_be_invoked_as_live_proof(self):
        with self.assertRaises(live.ProbeError):
            live.check_signature({"items": []}, "Initialize")
        fixture = LifecycleCalls()
        with self.assertRaises(live.ProbeError):
            live.check_target(fixture.targets[live.TOOL + "-cdo"], live.TOOL, cdo=False)
        with self.assertRaises(live.ProbeError):
            asyncio.run(live.exercise(fixture.call, wait_seconds=float("nan")))

    def test_callback_between_snapshot_and_reader_is_rechecked(self):
        fixture = LifecycleCalls(trigger_after=99)
        original_call = fixture.call

        async def race_call(name, arguments):
            if name == "g1r_invoke" and arguments["function"] == "ReadInitializeCount":
                target = next(item for item in fixture.targets.values() if item["handle"] == arguments["handle"])
                if not target["name"].startswith("Default__"):
                    fixture.trigger_after = 1
            return await original_call(name, arguments)

        report = asyncio.run(live.exercise(race_call, wait_seconds=0))
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["tool"]["instances"][0]["counts"]["InitializeCount"], 1)

    def test_missing_completeness_or_array_dimension_rejects_before_invocation(self):
        for mode in ("scan_complete", "array_dim", "object_partial"):
            fixture = LifecycleCalls()
            original_call = fixture.call

            async def incomplete(name, arguments):
                result = await original_call(name, arguments)
                if mode == "scan_complete" and name == "g1r_object_functions":
                    result.pop("scan_complete")
                if mode == "array_dim" and name == "g1r_object_functions":
                    result["items"][0]["parameters"][0].pop("array_dim")
                if mode == "object_partial" and name == "g1r_snapshot":
                    result["objects"][0].pop("partial")
                return result

            with self.subTest(mode=mode), self.assertRaises(live.ProbeError):
                asyncio.run(live.exercise(incomplete, wait_seconds=0))


if __name__ == "__main__":
    unittest.main()
