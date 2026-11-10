"""Const Transform проба сохраняет actual CPF и проверяет native point math без игры."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

from tests.test_transform_probe import TransformFixture

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("const_transform_contract_probe_test", TOOLS / "probe_const_transform_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ConstTransformFixture(TransformFixture):
    def __init__(self, *, input_flags=0x182, native_transform_flags=0x08000182, native_vector_flags=0x80,
                 bad_native=False, bad_native_target=False, **kwargs):
        super().__init__(**kwargs)
        self.target.update(name="Default__" + probe.HOST, **{"class": probe.HOST}, path="/Script/Angelscript.Default__" + probe.HOST)
        self.tokens["AcceptConstTransform"] = self.tokens.pop("AcceptTransform")
        self.tokens[probe.NATIVE_METHOD] = "sig-8"
        self.input_flags, self.native_transform_flags, self.native_vector_flags = input_flags, native_transform_flags, native_vector_flags
        self.bad_native, self.bad_native_target = bad_native, bad_native_target
        self.native_target = {"handle": "obj-2", "name": "Default__" + probe.NATIVE_CLASS, "class": probe.NATIVE_CLASS,
                              "path": "/Script/Engine.Default__Other" if bad_native_target else probe.NATIVE_PATH}

    def functions(self, name):
        if name == probe.NATIVE_METHOD:
            fields = []
            for key, width, offset, flags in (("ActualTransform", 96, 0, self.native_transform_flags),
                                             ("ActualPoint", 24, 96, self.native_vector_flags)):
                fields.append({"name": key, "type": "StructProperty", "element_size": width, "offset": offset,
                    "array_dim": 1, "return": False, "out": bool(flags & 0x100), "const": bool(flags & 0x2),
                    "reference": bool(flags & 0x08000000), "flags": flags, "direction": "input",
                    "passing": "const_reference" if flags & 0x2 else "value",
                    "struct": self.transform_metadata() if width == 96 else self.vector_metadata()})
            fields.append({"name": "ReturnValue", "type": "StructProperty", "element_size": 24, "offset": 120,
                "array_dim": 1, "return": True, "out": True, "const": False, "reference": False, "flags": 0x580,
                "struct": self.vector_metadata(), "direction": "return"})
            return {"items": [{"name": name, "parameters": fields, "parameter_size": 144, "parameter_count": 3,
                               "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234}],
                    "truncated": False, "scan_complete": True, "scan_truncated": False}
        actual = "AcceptTransform" if name == "AcceptConstTransform" else name
        if actual == "AcceptTransform": self.tokens[actual] = self.tokens[name]
        result = super().functions(actual)
        if actual == "AcceptTransform": self.tokens.pop(actual)
        item = result["items"][0]
        item["name"] = name
        if name == "AcceptConstTransform":
            flags = self.input_flags
            item["parameters"][0].update(flags=flags, out=bool(flags & 0x100), const=bool(flags & 0x2),
                reference=bool(flags & 0x08000000), passing="const_reference")
        return result

    async def call(self, name, arguments, *, expected_error=None):
        if name == "g1r_find_objects" and arguments["class_query"] == probe.NATIVE_CLASS:
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            return {"items": [self.native_target], "truncated": False}
        if name == "g1r_invoke" and arguments["function"] == probe.NATIVE_METHOD:
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            if arguments["handle"] != "obj-2" or arguments["expected_signature_token"] != self.tokens[probe.NATIVE_METHOD]:
                raise AssertionError("native target/token")
            supplied = arguments["arguments"]
            if set(supplied) != {"ActualTransform", "ActualPoint"}: raise AssertionError("names guessed")
            transform, point = supplied["ActualTransform"], supplied["ActualPoint"]
            result = {axis: point[axis] * transform["Scale3D"][axis] + transform["Translation"][axis] for axis in ("X", "Y", "Z")}
            if self.bad_native: result["X"] += 1
            return {"executed": True, "return_type": "StructProperty", "result": result, "result_metadata": self.vector_metadata()}
        if name == "g1r_invoke" and arguments["function"] == "AcceptConstTransform":
            rewritten = {**arguments, "function": "AcceptTransform"}
            self.tokens["AcceptTransform"] = self.tokens["AcceptConstTransform"]
            try: result = await super().call(name, rewritten, expected_error=expected_error)
            finally:
                self.tokens.pop("AcceptTransform")
                if self.calls: self.calls[-1] = (name, copy.deepcopy(arguments), expected_error)
            return result
        if name == "g1r_invoke" and arguments["function"] == "ReadMarker":
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        return await super().call(name, arguments, expected_error=expected_error)


class ConstTransformProbeTests(unittest.TestCase):
    def test_full_as_const_out_native_const_ref_math_getters_refusals_cleanup(self):
        fixture = ConstTransformFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["passing"], "const_reference")
        self.assertEqual(len(report["accepted"]), 2)
        self.assertEqual(len(report["rejections"]), 17)
        self.assertEqual(report["local_nonfinite_json_rejections"], 9)
        self.assertTrue(report["native_math"]["tested"])
        self.assertEqual(report["native_math"]["result"], {"X": 3.25, "Y": -1.5, "Z": 0.75})
        self.assertEqual(report["native_math"]["transform_parameter"], "ActualTransform")
        self.assertEqual(report["native_math"]["point_parameter"], "ActualPoint")
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertEqual(report["native_math"]["reflected_functions"], fixture.functions(probe.NATIVE_METHOD))

    def test_const_ref_without_out_and_native_const_vector_are_valid(self):
        fixture = ConstTransformFixture(input_flags=0x08000082, native_transform_flags=0x182, native_vector_flags=0x08000082)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertTrue(report["native_math"]["tested"])
        self.assertEqual(fixture.resets, 1)

    def test_const_only_mutable_inout_and_pure_out_reject_before_accept(self):
        for flags in (0x82, 0x08000080, 0x180, 0x08000180):
            fixture = ConstTransformFixture(input_flags=flags)
            with self.subTest(flags=flags), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] == "AcceptConstTransform" for name, args, _ in fixture.calls))

    def test_native_signature_mismatch_never_invokes_math(self):
        for kwargs in ({"native_transform_flags": 0x82}, {"native_vector_flags": 0x180}, {"bad_native_target": True}):
            fixture = ConstTransformFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] == probe.NATIVE_METHOD for name, args, _ in fixture.calls))

    def test_native_metadata_parameters_and_boundaries_are_strict(self):
        for alteration in ("count", "overlap", "misalignment", "transformpath", "vectorwidth", "flags", "returnref", "marker", "namealias"):
            fixture = ConstTransformFixture()
            listing = fixture.functions(probe.NATIVE_METHOD)
            item, transform = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "count": item["parameter_count"] = 4
            elif alteration == "overlap": item["parameters"][1]["offset"] = 80
            elif alteration == "misalignment": transform["offset"] = 1
            elif alteration == "transformpath": transform["struct"]["struct_path"] = "/Script/CoreUObject.Other"
            elif alteration == "vectorwidth": item["parameters"][1]["element_size"] = 12
            elif alteration == "flags": transform["const"] = False
            elif alteration == "returnref": item["parameters"][2].update(flags=0x08000580, reference=True)
            elif alteration == "marker": transform["argument_marker"] = {"out": True}
            else: item["parameters"][1]["name"] = transform["name"]
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_native_signature(listing)

    def test_bad_native_result_and_primary_cleanup_failure_are_preserved(self):
        fixture = ConstTransformFixture(bad_native=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_vector_value")
        self.assertTrue(any("TransformLocation" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned Transform CDO cleanup is not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_native_result_skips_reset_and_does_not_retry_math(self):
        async def scenario(code):
            fixture = ConstTransformFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == probe.NATIVE_METHOD:
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == probe.NATIVE_METHOD for name, args, _ in fixture.calls), 1)
        for code in ("game_timeout", "game_call_fault", "native_disconnected"):
            with self.subTest(code=code): asyncio.run(scenario(code))


if __name__ == "__main__": unittest.main()
