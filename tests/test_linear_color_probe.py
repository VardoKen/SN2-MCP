"""LinearColor probe проверяет canonicalRGBA, no clamp, native math и no retry."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import math
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("linear_color_contract_probe_test", TOOLS / "probe_linear_color_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ColorFixture:
    def __init__(self, *, scalar_width=8, bad_marker=False, bad_color=False, bad_native=False, mutate_refusal=False):
        self.scalar_width, self.bad_marker, self.bad_color, self.bad_native, self.mutate_refusal = scalar_width, bad_marker, bad_color, bad_native, mutate_refusal
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.native = {"handle": "obj-2", "name": "Default__" + probe.NATIVE_CLASS, "class": probe.NATIVE_CLASS, "path": probe.NATIVE_PATH}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate((*probe.METHODS, *probe.NATIVE_METHODS))}
        self.count, self.resets, self.calls = 0, 0, []

    @staticmethod
    def metadata():
        return {"struct_path": "/Script/CoreUObject.LinearColor", "struct_name": "LinearColor", "properties_size": 16,
                "min_alignment": 4, "components": [{"name": axis, "type": "FloatProperty", "element_size": 4,
                    "offset": i * 4, "array_dim": 1} for i, axis in enumerate(("R", "G", "B", "A"))]}

    def functions(self, name):
        fields, offset = [], 0
        if name in ("ReadColorR", "EchoColor", "LinearColor_ToNewOpacity"):
            fields.append({"name": "ActualColor" if name == "LinearColor_ToNewOpacity" else "Input", "type": "StructProperty",
                "element_size": 16, "offset": 0, "array_dim": 1, "return": False, "out": False, "reference": False,
                "const": False, "flags": 0x80, "direction": "input", "passing": "value", "struct": self.metadata()})
            offset = 16
        scalar_names = ("R", "G", "B", "A") if name == "MakeColor" else ("ActualOpacity",) if name == "LinearColor_ToNewOpacity" else ()
        for scalar in scalar_names:
            fields.append({"name": scalar, "type": "FloatProperty", "element_size": 4, "offset": offset, "array_dim": 1,
                           "return": False, "out": False, "reference": False, "const": False, "flags": 0x80})
            offset += 4
        size = offset
        returned = (("DoubleProperty" if self.scalar_width == 8 else "FloatProperty", self.scalar_width) if name == "ReadColorR" else
            {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4), "EchoColor": ("StructProperty", 16),
             "MakeStoredColor": ("StructProperty", 16), "MakeColor": ("StructProperty", 16), "LinearColor_ToNewOpacity": ("StructProperty", 16)}.get(name))
        if returned:
            field = {"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset, "array_dim": 1,
                     "return": True, "out": True, "reference": False, "const": False, "flags": 0x580}
            if field["type"] == "StructProperty": field.update(struct=self.metadata(), direction="return")
            fields.append(field)
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    @staticmethod
    def valid_color(value):
        return (isinstance(value, dict) and set(value) == set(probe.STORED)
                and all(type(component) in (int, float) and math.isfinite(component) and abs(component) <= 3.4028234663852886e38
                        for component in value.values()))

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.native if arguments["class_query"] == probe.NATIVE_CLASS else self.target], "truncated": False}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError((name, arguments))
        method, supplied = arguments["function"], arguments["arguments"]
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            actual = ("missing_argument" if "Input" not in supplied else "invalid_argument" if not self.valid_color(supplied["Input"])
                      else "unknown_argument" if set(supplied) != {"Input"} else None)
            if actual != expected_error: raise AssertionError(("wrong expected code", actual, expected_error))
            if self.mutate_refusal: self.count += 1
            return {"code": actual, "may_have_executed": False, "effect": {"executed": False, "may_have_executed": False}}
        if method == "ReadMarker": return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.resets += 1; self.count = 0
            return {"executed": True, "return_type": "", "result": None}
        if method in ("ReadColorR", "EchoColor", "MakeStoredColor"): self.count += 1
        if method == "ReadColorR":
            return {"executed": True, "return_type": "DoubleProperty" if self.scalar_width == 8 else "FloatProperty", "result": supplied["Input"]["R"]}
        if method == "EchoColor": value = dict(supplied["Input"])
        elif method == "MakeStoredColor": value = dict(probe.STORED)
        elif method == "MakeColor": value = dict(supplied)
        elif method == "LinearColor_ToNewOpacity": value = {**supplied["ActualColor"], "A": supplied["ActualOpacity"]}
        else: raise AssertionError(method)
        if self.bad_color and method == "EchoColor": value["G"] = True
        if self.bad_native and method in probe.NATIVE_METHODS: value["B"] = 1
        return {"executed": True, "return_type": "StructProperty", "result": value, "result_metadata": self.metadata()}


class LinearColorProbeTests(unittest.TestCase):
    def test_complete_value_series_float32_negative_hdr_native_math_reset(self):
        fixture = ColorFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["completed_mutating_calls"], 5)
        self.assertEqual(len(report["rejections"]), 13)
        self.assertEqual(report["local_nonfinite_json_rejections"], 3)
        self.assertEqual(report["native_math"]["MakeColor"]["result"], probe.STORED)
        self.assertEqual(report["native_math"]["LinearColor_ToNewOpacity"]["result"], {**probe.STORED, "A": 0.75})
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_scalar_return_width_is_live_reflection(self):
        report = asyncio.run(probe.exercise(ColorFixture(scalar_width=4).call))
        self.assertEqual(report["completed_mutating_calls"], 5)

    def test_layout_metadata_and_policies_are_strict(self):
        for alteration in ("path", "size", "alignment", "componenttype", "componentoffset", "array", "const", "out", "reference", "passing", "overlap"):
            listing = ColorFixture().functions("EchoColor")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "path": field["struct"]["struct_path"] = "/Script/CoreUObject.Color"
            elif alteration == "size": field["struct"]["properties_size"] = 32
            elif alteration == "alignment": field["struct"]["min_alignment"] = 8
            elif alteration == "componenttype": field["struct"]["components"][0]["type"] = "DoubleProperty"
            elif alteration == "componentoffset": field["struct"]["components"][1]["offset"] = 0
            elif alteration == "array": field["array_dim"] = True
            elif alteration == "const": field.update(const=True, flags=0x82)
            elif alteration == "out": field.update(out=True, flags=0x180)
            elif alteration == "reference": field.update(reference=True, flags=0x08000080)
            elif alteration == "passing": field["passing"] = "const_reference"
            else: item["parameters"][1]["offset"] = 0
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "EchoColor")

    def test_no_clamp_value_guard_rejects_bool_nonfinite_wrongshape(self):
        probe.check_color(probe.STORED, probe.STORED)
        for value in ({**probe.STORED, "G": 0}, {**probe.STORED, "B": 1}, {**probe.STORED, "R": True},
                      {**probe.STORED, "A": float("nan")}, {**probe.STORED, "X": 0}):
            with self.subTest(value=value), self.assertRaises(probe.ProbeError): probe.check_color(value, probe.STORED)

    def test_bad_result_marker_or_refusal_counter_are_not_accepted(self):
        fixture = ColorFixture(bad_marker=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        for kwargs in ({"bad_color": True}, {"bad_native": True}, {"mutate_refusal": True}):
            fixture = ColorFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)

    def test_native_const_signature_is_optional_and_never_invoked(self):
        fixture = ColorFixture()
        async def call(name, arguments, *, expected_error=None):
            result = await fixture.call(name, arguments, expected_error=expected_error)
            if name == "g1r_object_functions" and arguments["function_query"] == "LinearColor_ToNewOpacity":
                result["items"][0]["parameters"][0].update(flags=0x182, out=True, const=True, passing="const_reference")
            return result
        report = asyncio.run(probe.exercise(call))
        self.assertTrue(report["native_math"]["MakeColor"]["tested"])
        self.assertEqual(report["native_math"]["LinearColor_ToNewOpacity"]["status"], "unsupported_not_tested")
        self.assertFalse(any(name == "g1r_invoke" and args["function"] == "LinearColor_ToNewOpacity" for name, args, _ in fixture.calls))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_refusal_stops_without_counter_or_reset(self):
        async def scenario():
            fixture = ColorFixture()
            refusal_index = None
            async def call(name, arguments, *, expected_error=None):
                nonlocal refusal_index
                result = await fixture.call(name, arguments, expected_error=expected_error)
                if expected_error:
                    refusal_index = len(fixture.calls) - 1
                    return {"code": expected_error, "may_have_executed": True, "effect": {"executed": True}}
                return result
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, "probe_scalar_out_rejection_unconfirmed")
            self.assertEqual(fixture.calls[refusal_index + 1:], [])
            self.assertEqual(fixture.resets, 0)
        asyncio.run(scenario())

    def test_unknown_native_or_echo_result_skips_reset_no_retry(self):
        async def scenario(method):
            fixture = ColorFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == method:
                    await fixture.call(name, arguments)
                    raise probe.ProbeError("game_timeout", "Результат неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError): await probe.exercise(call)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == method for name, args, _ in fixture.calls), 1)
        for method in ("EchoColor", "MakeColor"):
            with self.subTest(method=method): asyncio.run(scenario(method))

    def test_repeated_cancel_waits_one_reset_without_repeating_mutation(self):
        async def scenario():
            fixture = ColorFixture()
            started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadColorR":
                    await fixture.call(name, arguments); raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe": started.set(); await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadColorR" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
