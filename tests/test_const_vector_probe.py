"""Const-vector проба проверяет действительные CPF, native math и cleanup без игры."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import math
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("const_vector_contract_probe_test", TOOLS / "probe_const_vector_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ConstVectorFixture:
    def __init__(self, *, width=8, input_flags=0x182, native_flags=0x08000182,
                 bad_marker=False, bad_echo=False, missing_effect=False, mutate_refusal=False, bad_reset=False,
                 bad_native=False, bad_native_target=False):
        self.width, self.input_flags, self.native_flags = width, input_flags, native_flags
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.native_target = {"handle": "obj-2", "name": "Default__" + probe.NATIVE_CLASS,
                              "class": probe.NATIVE_CLASS, "path": probe.NATIVE_PATH}
        if bad_native_target: self.native_target["path"] = "/Script/Engine.Default__Other"
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate((*probe.METHODS, probe.NATIVE_METHOD))}
        self.count, self.resets, self.calls = 0, 0, []
        self.bad_marker, self.bad_echo = bad_marker, bad_echo
        self.missing_effect, self.mutate_refusal, self.bad_reset, self.bad_native = missing_effect, mutate_refusal, bad_reset, bad_native

    def vector_metadata(self):
        return {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector", "properties_size": self.width * 3,
                "min_alignment": self.width, "components": [{"name": axis,
                "type": "DoubleProperty" if self.width == 8 else "FloatProperty", "element_size": self.width,
                "offset": i * self.width, "array_dim": 1} for i, axis in enumerate(probe.STORED)]}

    def layout(self):
        result = {"source": "live_native", "session_id": "native-owner", "handle": "obj-1", "property_name": "StoredVector",
                  "struct_path": "/Script/CoreUObject.Vector", "properties_size": self.width * 3,
                  "min_alignment": self.width, "consistency": "copied_bytes_then_metadata_revalidated", "partial": False, "truncated": False}
        result["components"] = [{**component, "value": probe.STORED[component["name"]]}
                                for component in self.vector_metadata()["components"]]
        return result

    def functions(self, name):
        native = name == probe.NATIVE_METHOD
        fields, offset = [], 0
        if name == "EchoConstVector" or native:
            flags = self.native_flags if native else self.input_flags
            fields.append({"name": "NativeVectorInput" if native else "Input", "type": "StructProperty",
                "element_size": self.width * 3, "offset": 0, "array_dim": 1, "return": False,
                "flags": flags, "out": bool(flags & 0x100), "reference": bool(flags & 0x08000000),
                "const": bool(flags & 0x2), "direction": "input", "passing": "const_reference", "struct": self.vector_metadata()})
            offset = self.width * 3
        size = offset
        returned = (("BoolProperty", 1) if native else
            {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
             "GetVector": ("StructProperty", self.width * 3), "EchoConstVector": ("StructProperty", self.width * 3)}.get(name))
        if returned:
            field = {"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset,
                     "array_dim": 1, "return": True, "out": True, "reference": False, "const": False, "flags": 0x580}
            if field["type"] == "StructProperty":
                field.update(struct=self.vector_metadata(), direction="return")
            fields.append(field)
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234,
                           "owner": probe.NATIVE_CLASS if native else probe.HOST}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.native_target if arguments["class_query"] == probe.NATIVE_CLASS else self.target], "truncated": False}
        if name == "g1r_read_struct": return self.layout()
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError((name, arguments))
        method = arguments["function"]
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            supplied = arguments["arguments"]
            if "Input" not in supplied: actual = "missing_argument"
            else:
                vector = supplied["Input"]
                valid = (isinstance(vector, dict) and set(vector) == {"X", "Y", "Z"}
                         and all(type(value) in (int, float) and math.isfinite(value) for value in vector.values()))
                actual = "invalid_argument" if not valid else "unknown_argument" if set(supplied) != {"Input"} else None
            if actual != expected_error: raise AssertionError(("wrong expected error", actual, expected_error))
            if self.mutate_refusal: self.count += 1
            return {"code": actual, "may_have_executed": False,
                    "effect": {} if self.missing_effect else {"executed": False, "may_have_executed": False}}
        if method == "ReadMarker":
            return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.count = 0
            self.resets += 1
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        if method == "GetVector":
            return {"executed": True, "return_type": "StructProperty", "result": dict(probe.STORED), "result_metadata": self.vector_metadata()}
        if method == "EchoConstVector":
            self.count += 1
            value = dict(arguments["arguments"]["Input"])
            if self.bad_echo: value["X"] = True
            return {"executed": True, "return_type": "StructProperty", "result": value, "result_metadata": self.vector_metadata()}
        if method == probe.NATIVE_METHOD:
            if arguments["handle"] != "obj-2": raise AssertionError("native target mismatch")
            value = arguments["arguments"]["NativeVectorInput"]
            result = all(component == 0 for component in value.values())
            return {"executed": True, "return_type": "BoolProperty", "result": not result if self.bad_native else result}
        raise AssertionError((name, arguments))


class ConstVectorProbeTests(unittest.TestCase):
    def test_full_double_series_as_const_out_native_const_ref_fullreflection_cleanup(self):
        fixture = ConstVectorFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(len(report["echoes"]), 3)
        self.assertEqual(report["echoes"][-1]["X"], 2**53 + 2)
        self.assertEqual(len(report["rejections"]), 11)
        self.assertEqual(report["local_nonfinite_json_rejections"], 3)
        self.assertEqual(report["completed_mutating_calls"], 3)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertTrue(report["native_math"]["tested"])
        self.assertEqual(report["native_math"]["input_name"], "NativeVectorInput")
        self.assertEqual([item["result"] for item in report["native_math"]["results"]], [True, False])
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertEqual(report["native_math"]["reflected_functions"], fixture.functions(probe.NATIVE_METHOD))
        self.assertEqual(report["inout"], "not_tested")
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_float_layout_and_const_ref_without_out_are_supported(self):
        fixture = ConstVectorFixture(width=4, input_flags=0x08000082, native_flags=0x182)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["layout"]["size"], 12)
        self.assertEqual(len(report["echoes"]), 2)
        self.assertEqual(fixture.resets, 1)

    def test_const_only_mutable_reference_and_pure_out_flags_reject_before_echo(self):
        for flags in (0x82, 0x08000080, 0x180, 0x08000180, 0x482):
            fixture = ConstVectorFixture(input_flags=flags)
            with self.subTest(flags=flags), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] == "EchoConstVector" for name, args, _ in fixture.calls))

    def test_signature_flags_metadata_name_boundaries_and_return_are_strict(self):
        for alteration in ("missingflags", "boolflags", "inconsistentconst", "passing", "direction", "marker",
                           "path", "size", "array", "components", "name", "overlap", "refreturn", "count"):
            fixture = ConstVectorFixture()
            listing = fixture.functions("EchoConstVector")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "missingflags": field.pop("flags")
            elif alteration == "boolflags": field["flags"] = True
            elif alteration == "inconsistentconst": field["const"] = False
            elif alteration == "passing": field["passing"] = "value"
            elif alteration == "direction": field["direction"] = "output"
            elif alteration == "marker": field["argument_marker"] = {"out": True}
            elif alteration == "path": field["struct"]["struct_path"] = "/Script/CoreUObject.Rotator"
            elif alteration == "size": field["element_size"] = 12
            elif alteration == "array": field["array_dim"] = True
            elif alteration == "components": field["struct"]["components"][1]["offset"] = 0
            elif alteration == "name": field["name"] = "OtherInput"
            elif alteration == "overlap": item["parameters"][1]["offset"] = 0
            elif alteration == "refreturn": item["parameters"][1].update(flags=0x08000580, reference=True)
            else: item["parameter_count"] = 1
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError):
                probe.check_signature(listing, "EchoConstVector", 24)

    def test_native_signature_or_target_mismatch_never_invokes_native(self):
        for kwargs in ({"native_flags": 0x82}, {"bad_native_target": True}):
            fixture = ConstVectorFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] == probe.NATIVE_METHOD for name, args, _ in fixture.calls))

    def test_bad_marker_echo_native_result_and_effect_cleanup_correctly(self):
        fixture = ConstVectorFixture(bad_marker=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        for kwargs in ({"bad_echo": True}, {"bad_native": True}, {"missing_effect": True}, {"mutate_refusal": True}):
            fixture = ConstVectorFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)

    def test_primary_failure_retains_reflection_and_cleanup_note(self):
        fixture = ConstVectorFixture(bad_echo=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_vector_value")
        self.assertTrue(any("reflected_functions" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned const-vector CDO cleanup is not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_failures_in_as_or_native_skip_reset_without_repeat(self):
        async def scenario(code, failed_method):
            fixture = ConstVectorFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == failed_method:
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат исходного вызова неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertTrue(any("Const-vector cleanup skipped after an uncertain call or unavailable connection" in note
                                for note in caught.exception.__notes__))
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == failed_method for name, args, _ in fixture.calls), 1)
        for code in probe.UNKNOWN_GAME:
            for method in ("EchoConstVector", probe.NATIVE_METHOD):
                with self.subTest(code=code, method=method): asyncio.run(scenario(code, method))

    def test_repeated_cancel_waits_one_reset_and_never_repeats_echo(self):
        async def scenario():
            fixture = ConstVectorFixture()
            started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "EchoConstVector" and not expected_error:
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
            self.assertEqual(fixture.count, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "EchoConstVector" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
