"""Проверки AS wrapper: finite default, точный out, эффект и одна очистка."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("hit_result_wrapper_probe_test", TOOLS / "probe_hit_result_wrapper_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class WrapperFixture:
    def __init__(self, *, hit=False, bad_cleanup=False, has_world=True):
        self.hit, self.bad_cleanup, self.has_world = hit, bad_cleanup, has_world
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.instance = {"handle": "obj-live", "name": probe.HOST + "_0", "class": probe.HOST,
                         "path": "/Game/Menu.Menu:PersistentLevel." + probe.HOST + "_0"}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.count, self.resets, self.calls = 0, 0, []

    @staticmethod
    def metadata():
        return {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector", "properties_size": 24, "min_alignment": 8,
                "components": [{"name": axis, "type": "DoubleProperty", "element_size": 8, "offset": i * 8, "array_dim": 1}
                               for i, axis in enumerate(("X", "Y", "Z"))]}

    def functions(self, name):
        fields, offset = [], 0
        if name == "TraceImpactPoint":
            for key in ("Start", "End"):
                fields.append({"name": key, "type": "StructProperty", "element_size": 24, "offset": offset, "array_dim": 1,
                    "return": False, "out": False, "const": False, "reference": False, "flags": 0x80,
                    "direction": "input", "passing": "value", "struct": self.metadata()})
                offset += 24
            fields.append({"name": "ActorsToIgnore", "type": "ArrayProperty", "element_size": 16, "offset": offset,
                "array_dim": 1, "return": False, "out": True, "const": True, "reference": True, "flags": 0x08000182,
                "direction": "input", "passing": "const_reference", "argument_constraints": dict(probe.CONSTRAINTS),
                "array": {"inner_type": "ObjectProperty", "inner_element_size": 8, "inner_array_dim": 1, "inner_offset": 0,
                    "property_class": {"path": "/Script/Engine.Actor", "name": "Actor"}}})
            offset += 16
            fields.append({"name": "ImpactPoint", "type": "StructProperty", "element_size": 24, "offset": offset, "array_dim": 1,
                "return": False, "out": True, "const": False, "reference": False, "flags": 0x180,
                "direction": "output", "argument_marker": {"out": True}, "struct": self.metadata()})
            offset += 24
        size = offset
        returned = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                    "ReadDefaultImpactPoint": ("StructProperty", 24), "TraceImpactPoint": ("BoolProperty", 1),
                    "HasWorld": ("BoolProperty", 1)}.get(name)
        if returned:
            field = {"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset, "array_dim": 1,
                     "return": True, "out": True, "const": False, "reference": False, "flags": 0x580}
            if field["type"] == "StructProperty": field.update(struct=self.metadata(), direction="return")
            fields.append(field)
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name]}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.target, self.instance], "truncated": False}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError(name)
        method = arguments["function"]
        if arguments["handle"] != self.instance["handle"]: raise AssertionError("CDO must never be invoked")
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            return {"code": expected_error, "may_have_executed": False, "effect": {"executed": False, "may_have_executed": False,
                    "cleanup_attempted": False, "cleanup_confirmed": True, "array_input_cleanup_confirmed": True,
                    "cleanup_state": "released", "object_array_input_quarantined": False}}
        if method == "ReadMarker": return {"executed": True, "return_type": "IntProperty", "result": probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "HasWorld": return {"executed": True, "return_type": "BoolProperty", "result": self.has_world}
        if method == "ResetProbe": self.count = 0; self.resets += 1; return {"executed": True, "return_type": "", "result": None}
        self.count += 1
        if method == "ReadDefaultImpactPoint":
            return {"executed": True, "return_type": "StructProperty", "result": {"X": 4, "Y": -2, "Z": 0.5}, "result_metadata": self.metadata()}
        return {"executed": True, "return_type": "BoolProperty", "result": self.hit,
                "out_parameters": {"ImpactPoint": {"X": 1, "Y": -2, "Z": 3} if self.hit else dict(probe.ZERO)},
                "out_parameter_metadata": {"ImpactPoint": self.metadata()}, "cleanup_confirmed": not self.bad_cleanup,
                "array_input_cleanup_confirmed": not self.bad_cleanup}


class HitResultWrapperProbeTests(unittest.TestCase):
    def test_finite_nonzero_default_and_false_trace_explicit_zero_branch(self):
        fixture = WrapperFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["default_impact_point"], {"X": 4, "Y": -2, "Z": 0.5})
        self.assertFalse(report["trace"]["hit"])
        self.assertEqual(report["trace"]["impact_point"], probe.ZERO)
        self.assertEqual(report["completed_mutating_calls"], 2)
        self.assertEqual(len(report["rejections"]), 7)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(report["world_context"], "available_confirmed_by_own_HasWorld")

    def test_missing_world_never_invokes_trace_or_trace_negatives(self):
        fixture = WrapperFixture(has_world=False)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["world_context"], "world_context_unavailable")
        self.assertEqual(report["trace"], {"tested": False, "reason": "world_context_unavailable"})
        self.assertEqual(report["completed_mutating_calls"], 1)
        self.assertEqual(report["rejections"], [])
        self.assertEqual(fixture.resets, 1)
        self.assertFalse(any(name == "g1r_invoke" and args["function"] == "TraceImpactPoint" for name, args, _ in fixture.calls))

    def test_true_trace_finite_impact_does_not_assume_false_or_zero(self):
        report = asyncio.run(probe.exercise(WrapperFixture(hit=True).call))
        self.assertTrue(report["trace"]["hit"])
        self.assertEqual(report["trace"]["impact_point"], {"X": 1, "Y": -2, "Z": 3})

    def test_cdo_without_live_instance_and_template_paths_reject_before_invocation(self):
        for altered in (None, {"handle": "obj-live", "name": probe.HOST + "_0", "class": probe.HOST,
                                "path": "/Script/Angelscript." + probe.HOST + "_0"}):
            fixture = WrapperFixture()
            async def call(name, arguments, *, expected_error=None):
                result = await fixture.call(name, arguments, expected_error=expected_error)
                if name == "g1r_find_objects":
                    result["items"] = [fixture.target] + ([] if altered is None else [altered])
                return result
            with self.subTest(altered=altered), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(call))
            self.assertFalse(any(name == "g1r_invoke" for name, _, _ in fixture.calls))

    def test_signature_actor_inner_class_and_out_contract_are_strict(self):
        for change in ("class", "outconst", "outmarker", "overlap", "rawhit", "booldim", "booloffset"):
            listing = WrapperFixture().functions("TraceImpactPoint")
            fields = listing["items"][0]["parameters"]
            if change == "class": fields[2]["array"]["property_class"]["path"] = "/Script/CoreUObject.Object"
            elif change == "outconst": fields[3].update(const=True, flags=0x182)
            elif change == "outmarker": fields[3]["argument_marker"] = {"out": False}
            elif change == "overlap": fields[4]["offset"] = 64
            elif change == "rawhit": fields[3]["struct"]["struct_path"] = "/Script/Engine.HitResult"
            elif change == "booldim": fields[2]["array"]["inner_array_dim"] = True
            else: fields[2]["array"]["inner_offset"] = False
            with self.subTest(change=change), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "TraceImpactPoint")

    def test_unknown_refusal_stops_before_counter_and_reset(self):
        async def scenario():
            fixture = WrapperFixture()
            index = None
            async def call(name, arguments, *, expected_error=None):
                nonlocal index
                result = await fixture.call(name, arguments, expected_error=expected_error)
                if expected_error:
                    index = len(fixture.calls) - 1
                    return {"code": expected_error, "may_have_executed": True, "effect": {"executed": True}}
                return result
            with self.assertRaises(probe.ProbeError): await probe.exercise(call)
            self.assertEqual(fixture.calls[index + 1:], [])
            self.assertEqual(fixture.resets, 0)
        asyncio.run(scenario())

    def test_unknown_trace_or_cleanup_skips_reset_without_retry(self):
        fixture = WrapperFixture(bad_cleanup=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        async def scenario(code):
            fixture = WrapperFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "TraceImpactPoint":
                    await fixture.call(name, arguments); raise probe.ProbeError(code, "Результат неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError): await probe.exercise(call)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "TraceImpactPoint" for name, args, _ in fixture.calls), 1)
        for code in ("game_timeout", "game_result_error", "game_call_fault"):
            with self.subTest(code=code): asyncio.run(scenario(code))

    def test_repeated_cancel_waits_one_reset_without_repeating_default(self):
        async def scenario():
            fixture = WrapperFixture(); started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadDefaultImpactPoint":
                    await fixture.call(name, arguments); raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe": started.set(); await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call)); await asyncio.wait_for(started.wait(), 2)
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadDefaultImpactPoint" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
