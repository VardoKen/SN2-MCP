"""Transform input проба проверяет canonical metadata, effects, getters и один reset."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import math
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("transform_contract_probe_test", TOOLS / "probe_transform_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class TransformFixture:
    def __init__(self, *, bad_marker=False, bad_translation=False, bad_scale=False, bad_rotation=False,
                 missing_effect=False, mutate_refusal=False, bad_reset=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.count, self.resets, self.calls = 0, 0, []
        self.stored = copy.deepcopy(probe.IDENTITY)
        self.bad_marker, self.bad_translation, self.bad_scale, self.bad_rotation = bad_marker, bad_translation, bad_scale, bad_rotation
        self.missing_effect, self.mutate_refusal, self.bad_reset = missing_effect, mutate_refusal, bad_reset

    @staticmethod
    def vector_metadata():
        return {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector", "properties_size": 24,
                "min_alignment": 8, "components": [{"name": axis, "type": "DoubleProperty", "element_size": 8,
                    "offset": i * 8, "array_dim": 1} for i, axis in enumerate(("X", "Y", "Z"))]}

    @classmethod
    def transform_metadata(cls):
        quat = {"struct_path": "/Script/CoreUObject.Quat", "struct_name": "Quat", "properties_size": 32,
                "min_alignment": 16, "components": [{"name": axis, "type": "DoubleProperty", "element_size": 8,
                    "offset": i * 8, "array_dim": 1} for i, axis in enumerate(("X", "Y", "Z", "W"))]}
        return {"struct_path": "/Script/CoreUObject.Transform", "struct_name": "Transform", "properties_size": 96,
                "min_alignment": 16, "members": [{"name": name, "type": "StructProperty", "element_size": width,
                    "offset": offset, "array_dim": 1, "struct": quat if name == "Rotation" else cls.vector_metadata()}
                    for name, width, offset in (("Rotation", 32, 0), ("Translation", 24, 32), ("Scale3D", 24, 64))]}

    def functions(self, name):
        fields, offset = [], 0
        if name == "AcceptTransform":
            fields.append({"name": "Input", "type": "StructProperty", "element_size": 96, "offset": 0,
                           "array_dim": 1, "return": False, "out": False, "reference": False, "const": False,
                           "flags": 0x80, "struct": self.transform_metadata(), "direction": "input", "passing": "value"})
            offset = 96
        size = offset
        returned = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4), "AcceptTransform": ("BoolProperty", 1),
                    **{getter: ("StructProperty", 24) for getter in probe.GETTERS}}.get(name)
        if returned:
            field = {"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset,
                     "array_dim": 1, "return": True, "out": True, "reference": False, "const": False, "flags": 0x580}
            if field["type"] == "StructProperty": field.update(struct=self.vector_metadata(), direction="return")
            fields.append(field)
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    @staticmethod
    def valid_transform(value):
        if not isinstance(value, dict) or set(value) != {"Rotation", "Translation", "Scale3D"}: return False
        for name, axes in (("Rotation", {"X", "Y", "Z", "W"}), ("Translation", {"X", "Y", "Z"}), ("Scale3D", {"X", "Y", "Z"})):
            part = value[name]
            if (not isinstance(part, dict) or set(part) != axes
                    or any(type(component) not in (int, float) or not math.isfinite(component) for component in part.values())):
                return False
        quat = value["Rotation"]
        return (all(abs(component) <= 1 + 1e-6 for component in quat.values())
                and abs(sum(component * component for component in quat.values()) - 1) <= 1e-6)

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError((name, arguments))
        method = arguments["function"]
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            supplied = arguments["arguments"]
            actual = ("missing_argument" if "Input" not in supplied else "invalid_argument" if not self.valid_transform(supplied["Input"])
                      else "unknown_argument" if set(supplied) != {"Input"} else None)
            if actual != expected_error: raise AssertionError(("wrong expected error", actual, expected_error))
            if self.mutate_refusal: self.count += 1
            return {"code": actual, "may_have_executed": False,
                    "effect": {} if self.missing_effect else {"executed": False, "may_have_executed": False}}
        if method == "ReadMarker":
            return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.count, self.stored = 0, copy.deepcopy(probe.IDENTITY)
            self.resets += 1
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        if method == "AcceptTransform":
            self.count += 1
            self.stored = copy.deepcopy(arguments["arguments"]["Input"])
            return {"executed": True, "return_type": "BoolProperty", "result": True}
        if method in probe.GETTERS:
            if method == "GetTranslation":
                result = dict(self.stored["Translation"])
                if self.bad_translation and self.count: result["X"] = True
            elif method == "GetScale3D":
                result = dict(self.stored["Scale3D"])
                if self.bad_scale and self.count: result["Z"] = 0
            else:
                rotation = self.stored["Rotation"]
                result = {"X": 0.0, "Y": math.degrees(2 * math.atan2(rotation["Z"], rotation["W"])), "Z": 0.0}
                if self.bad_rotation and self.count: result["Y"] += 0.1
            return {"executed": True, "return_type": "StructProperty", "result": result, "result_metadata": self.vector_metadata()}
        raise AssertionError((name, arguments))


class TransformProbeTests(unittest.TestCase):
    def test_complete_canonical_series_exact_getters_refusals_fullreflection_single_reset(self):
        fixture = TransformFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(len(report["accepted"]), 2)
        self.assertEqual(report["completed_mutating_calls"], 2)
        self.assertEqual(len(report["rejections"]), 17)
        self.assertEqual(report["local_nonfinite_json_rejections"], 9)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.stored, probe.IDENTITY)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertIn("semantic equivalence", report["rotation_verification"])
        self.assertEqual(report["euler_tolerance_degrees"], 1e-4)
        self.assertEqual(report["transform_return"], "not_tested")
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_canonical_nested_metadata_is_strict(self):
        for alteration in ("rootpath", "rootsize", "rootalignment", "missingmember", "duplicatemember", "offset", "width", "array",
                           "quatpath", "quatsize", "quatwidth", "quatoffset", "quatname", "vectorpath", "vectorwidth", "nestedalignment"):
            metadata = TransformFixture.transform_metadata()
            member, quat = metadata["members"][0], metadata["members"][0]["struct"]
            if alteration == "rootpath": metadata["struct_path"] = "/Script/CoreUObject.Other"
            elif alteration == "rootsize": metadata["properties_size"] = 80
            elif alteration == "rootalignment": metadata["min_alignment"] = 3
            elif alteration == "missingmember": metadata["members"].pop()
            elif alteration == "duplicatemember": metadata["members"][1]["name"] = "Rotation"
            elif alteration == "offset": member["offset"] = 16
            elif alteration == "width": member["element_size"] = 24
            elif alteration == "array": member["array_dim"] = True
            elif alteration == "quatpath": quat["struct_path"] = "/Script/CoreUObject.Rotator"
            elif alteration == "quatsize": quat["properties_size"] = 16
            elif alteration == "quatwidth": quat["components"][0]["type"] = "FloatProperty"
            elif alteration == "quatoffset": quat["components"][3]["offset"] = 16
            elif alteration == "quatname": quat["components"][0]["name"] = "Q"
            elif alteration == "vectorpath": metadata["members"][1]["struct"]["struct_path"] = "/Script/CoreUObject.Other"
            elif alteration == "vectorwidth": metadata["members"][2]["struct"]["properties_size"] = 12
            else: metadata["min_alignment"] = 8
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_transform_metadata(metadata)

    def test_input_flags_directions_spans_and_types_reject_before_mutation(self):
        for alteration in ("out", "const", "ref", "missingflags", "size", "array", "passing", "direction", "marker", "name", "overlap", "count", "alignment"):
            fixture = TransformFixture()
            listing = fixture.functions("AcceptTransform")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "out": field.update(out=True, flags=0x180)
            elif alteration == "const": field.update(const=True, flags=0x82)
            elif alteration == "ref": field.update(reference=True, flags=0x08000080)
            elif alteration == "missingflags": field.pop("flags")
            elif alteration == "size": field["element_size"] = 80
            elif alteration == "array": field["array_dim"] = True
            elif alteration == "passing": field["passing"] = "const_reference"
            elif alteration == "direction": field["direction"] = "output"
            elif alteration == "marker": field["argument_marker"] = {"out": True}
            elif alteration == "name": field["name"] = "Other"
            elif alteration == "overlap": item["parameters"][1]["offset"] = 80
            elif alteration == "alignment": field["offset"] = 1; item["parameter_size"] = 112; item["parameters"][1]["offset"] = 112
            else: item["parameter_count"] = 1
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "AcceptTransform")

    def test_bad_marker_or_bad_getter_values_are_not_accepted(self):
        fixture = TransformFixture(bad_marker=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        for kwargs in ({"bad_translation": True}, {"bad_scale": True}, {"bad_rotation": True}, {"missing_effect": True}, {"mutate_refusal": True}):
            fixture = TransformFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)

    def test_quaternion_shape_norm_and_errors_are_independent_of_expected_code(self):
        fixture = TransformFixture()
        self.assertTrue(fixture.valid_transform(probe.IDENTITY))
        for quat in ({"X": 0, "Y": 0, "Z": 0, "W": 0}, {"X": 0, "Y": 0, "Z": 0, "W": 2},
                     {"X": 0, "Y": 0, "Z": 0, "W": 1.000002}, {"X": True, "Y": 0, "Z": 0, "W": 1}):
            value = {**probe.IDENTITY, "Rotation": quat}
            self.assertFalse(fixture.valid_transform(value))
            arguments = {"function": "AcceptTransform", "arguments": {"Input": value}, "expected_signature_token": fixture.tokens["AcceptTransform"]}
            with self.assertRaises(AssertionError): asyncio.run(fixture.call("g1r_invoke", arguments, expected_error="missing_argument"))

    def test_euler_tolerance_and_exact_component_type_checks(self):
        probe.check_euler({"X": 0.00005, "Y": 90.00005, "Z": -0.00005}, {"X": 0, "Y": 90, "Z": 0})
        for value in ({"X": True, "Y": 90, "Z": 0}, {"X": 0, "Y": 90.001, "Z": 0},
                      {"X": 0, "Y": float("nan"), "Z": 0}, {"X": 0, "Y": 90, "Z": 0, "W": 1}):
            with self.subTest(value=value), self.assertRaises(probe.ProbeError): probe.check_euler(value, {"X": 0, "Y": 90, "Z": 0})

    def test_primary_failure_retains_metadata_and_cleanup_failure(self):
        fixture = TransformFixture(bad_rotation=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_transform_rotation")
        self.assertTrue(any("reflected_functions" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned Transform CDO cleanup is not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_result_skips_reset_without_repeat(self):
        async def scenario(code):
            fixture = TransformFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "AcceptTransform":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "AcceptTransform" for name, args, _ in fixture.calls), 1)
        for code in probe.UNKNOWN_GAME:
            with self.subTest(code=code): asyncio.run(scenario(code))

    def test_repeated_cancel_waits_one_reset_and_never_repeats_accept(self):
        async def scenario():
            fixture = TransformFixture()
            started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "AcceptTransform":
                    await fixture.call(name, arguments)
                    raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe":
                    started.set()
                    await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.stored, probe.IDENTITY)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "AcceptTransform" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
