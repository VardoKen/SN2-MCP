"""FVector-проба изолирована от игры и подтверждает эффекты, границы и очистку."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("vector_contract_probe_test", TOOLS / "probe_vector_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class VectorFixture:
    def __init__(self, *, width=8, wrong_signature=False, bad_marker=False, bad_layout=False,
                 echo_bool=False, bad_out=False, missing_effect=False, mutate_refusal=False, bad_reset=False):
        self.width = width
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.count = 0
        self.resets = 0
        self.calls = []
        self.wrong_signature, self.bad_marker, self.bad_layout = wrong_signature, bad_marker, bad_layout
        self.echo_bool, self.bad_out = echo_bool, bad_out
        self.missing_effect, self.mutate_refusal, self.bad_reset = missing_effect, mutate_refusal, bad_reset

    def layout(self):
        return {"source": "live_native", "session_id": "native-owner", "handle": "obj-1", "property_name": "StoredVector",
                "struct_path": "/Script/CoreUObject.Vector", "properties_size": self.width * 3, "min_alignment": self.width,
                "consistency": "copied_bytes_then_metadata_revalidated", "partial": False, "truncated": False,
                "components": [{"name": axis, "type": "DoubleProperty" if self.width == 8 else "FloatProperty",
                                "element_size": self.width, "offset": i * self.width,
                                "value": 99 if self.bad_layout else probe.STORED[axis]} for i, axis in enumerate(probe.STORED)]}

    def functions(self, name):
        vector_size = self.width * 3
        fields = []
        offset = 0
        if name in {"EchoVector", "WriteVector"}:
            fields.append({"name": "Input" if name == "EchoVector" else "Output", "type": "StructProperty",
                           "element_size": vector_size, "offset": 0, "array_dim": 1, "return": False,
                           "out": name == "WriteVector", "reference": False})
            offset = vector_size
        size = offset
        returned = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                    "GetVector": ("StructProperty", vector_size), "EchoVector": ("StructProperty", vector_size),
                    "WriteVector": ("BoolProperty", 1)}.get(name)
        if returned:
            fields.append({"name": "ReturnValue", "type": returned[0], "element_size": returned[1],
                           "offset": offset, "array_dim": 1, "return": True, "out": True, "reference": False})
            if offset == 0:
                size = returned[1]
        if self.wrong_signature and name == "WriteVector":
            fields[0]["out"] = False
        for field in fields:
            if field["type"] == "StructProperty":
                field["struct"] = {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector",
                                   "properties_size": vector_size, "min_alignment": self.width,
                                   "components": [{"name": axis, "type": "DoubleProperty" if self.width == 8 else "FloatProperty",
                                                   "element_size": self.width, "offset": i * self.width, "array_dim": 1}
                                                  for i, axis in enumerate(probe.STORED)]}
                field["direction"] = "return" if field["return"] else "output" if name == "WriteVector" else "input"
                if field["direction"] == "output":
                    field["argument_marker"] = {"out": True}
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name]}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, arguments, expected_error))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "g1r_read_struct":
            return self.layout()
        if name == "g1r_object_functions":
            return self.functions(arguments["function_query"])
        if name != "g1r_invoke":
            raise AssertionError((name, arguments))
        method = arguments["function"]
        if arguments["expected_signature_token"] != self.tokens[method]:
            raise AssertionError("wrong token")
        if expected_error:
            if self.mutate_refusal:
                self.count += 1
            result = {"code": "invalid_argument", "may_have_executed": False,
                      "effect": {"executed": False, "may_have_executed": False}}
            if self.missing_effect:
                result["effect"] = {}
            return result
        if method == "ReadMarker":
            return {"executed": True, "return_type": "IntProperty", "result": 9 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount":
            return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.resets += 1
            self.count = 0
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        if method == "GetVector":
            metadata = self.functions(method)["items"][0]["parameters"][0]["struct"]
            return {"executed": True, "return_type": "StructProperty", "result": dict(probe.STORED), "result_metadata": metadata}
        if method == "EchoVector":
            self.count += 1
            value = dict(arguments["arguments"]["Input"])
            if self.echo_bool:
                value["X"] = True
            metadata = self.functions(method)["items"][0]["parameters"][1]["struct"]
            return {"executed": True, "return_type": "StructProperty", "result": value, "result_metadata": metadata}
        if method == "WriteVector":
            self.count += 1
            out = {} if self.bad_out else {"Output": dict(probe.STORED)}
            metadata = self.functions(method)["items"][0]["parameters"][0]["struct"]
            return {"executed": True, "return_type": "BoolProperty", "result": True, "out_parameters": out,
                    "out_parameter_metadata": {"Output": metadata}}
        raise AssertionError((name, arguments))


class VectorProbeTests(unittest.TestCase):
    def test_complete_double_series_exact_vectors_out_refusals_single_reset(self):
        fixture = VectorFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["layout"]["component_width"], 8)
        self.assertEqual(len(report["echoes"]), 3)
        self.assertEqual(report["echoes"][-1]["X"], 2**53 + 2)
        self.assertEqual(report["out_parameters"], {"Output": probe.STORED})
        self.assertEqual(len(report["rejections"]), 15)
        self.assertEqual(report["local_nonfinite_json_rejections"], 3)
        self.assertEqual(report["completed_mutating_calls"], 4)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(report["inout"], "not_tested")
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_float_layout_omits_large_double_case(self):
        fixture = VectorFixture(width=4)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["layout"]["size"], 12)
        self.assertEqual(len(report["echoes"]), 2)
        self.assertEqual(report["completed_mutating_calls"], 3)
        self.assertEqual(fixture.resets, 1)

    def test_wrong_layout_marker_out_signature_fail_before_mutation(self):
        for kwargs in ({"bad_layout": True}, {"wrong_signature": True}, {"bad_marker": True}):
            fixture = VectorFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError):
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and arguments["function"] in {"EchoVector", "WriteVector"}
                                 for name, arguments, _ in fixture.calls))

    def test_bad_return_out_or_false_effect_restores_counter_once(self):
        for kwargs in ({"echo_bool": True}, {"bad_out": True}, {"missing_effect": True}, {"mutate_refusal": True}):
            fixture = VectorFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError):
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)

    def test_primary_failure_survives_cleanup_error(self):
        fixture = VectorFixture(echo_bool=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_vector_value")
        self.assertTrue(any("Owned FVector CDO cleanup is not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_timeout_or_call_fault_skips_cleanup_and_preserves_original(self):
        async def scenario(code):
            fixture = VectorFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "EchoVector":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат исходного вызова неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught:
                await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertTrue(any("FVector cleanup skipped after an uncertain call or unavailable connection" in note
                                for note in caught.exception.__notes__))
            self.assertEqual(sum(name == "g1r_invoke" and arguments["function"] == "EchoVector" for name, arguments, _ in fixture.calls), 1)
        for code in ("game_timeout", "game_call_fault", "native_disconnected"):
            with self.subTest(code=code):
                asyncio.run(scenario(code))

    def test_vector_shape_finite_numbers_and_exact_effect_validation(self):
        for vector in (None, [], {**probe.STORED, "W": 0}, {"X": True, "Y": -2.5, "Z": 3.75},
                       {"X": float("nan"), "Y": -2.5, "Z": 3.75}, {"X": "1.25", "Y": -2.5, "Z": 3.75}):
            with self.subTest(vector=vector), self.assertRaises(probe.ProbeError):
                probe.check_vector(vector, probe.STORED)
        valid = {"code": "invalid_argument", "may_have_executed": False, "effect": {"executed": False}}
        probe.check_rejection(valid, "EchoVector", None)
        for extra in ({"executed": True}, {"effect": {}}, {"effect": {"executed": None}},
                      {"effect": {"executed": False, "may_have_executed": True}}):
            with self.subTest(extra=extra), self.assertRaises(probe.ProbeError):
                probe.check_rejection({**valid, **extra}, "EchoVector", None)

    def test_double_call_extent_out_flags_and_scalar_return_types_are_strict(self):
        fixture = VectorFixture()
        for method, alteration in (("EchoVector", "size"), ("WriteVector", "out"), ("EchoVector", "ref"),
                                   ("GetVector", "array"), ("WriteVector", "count")):
            listing = fixture.functions(method)
            item = listing["items"][0]
            if alteration == "size": item["parameters"][0]["element_size"] = 12
            elif alteration == "out": item["parameters"][0]["out"] = False
            elif alteration == "ref": item["parameters"][0]["reference"] = True
            elif alteration == "array": item["parameters"][0]["array_dim"] = True
            else: item["parameter_count"] = 0
            with self.subTest(method=method, alteration=alteration), self.assertRaises(probe.ProbeError):
                probe.check_signature(listing, method, 24)
        with self.assertRaises(probe.ProbeError):
            probe.check_scalar({"executed": True, "return_type": "BoolProperty", "result": 1}, "BoolProperty", True)

    def test_echo_input_and_write_out_cannot_overlap_return(self):
        fixture = VectorFixture()
        for method in ("EchoVector", "WriteVector"):
            listing = fixture.functions(method)
            item = listing["items"][0]
            item["parameters"][1]["offset"] = 0
            with self.subTest(method=method), self.assertRaises(probe.ProbeError) as caught:
                probe.check_signature(listing, method, 24)
            self.assertEqual(caught.exception.code, "probe_signature_mismatch")
        # ReturnValue после ParmsSize допустим при непересекающихся ограниченных диапазонах.
        probe.check_signature(fixture.functions("EchoVector"), "EchoVector", 24)
        probe.check_signature(fixture.functions("WriteVector"), "WriteVector", 24)

    def test_every_struct_parameter_has_exact_vector_public_metadata(self):
        fixture = VectorFixture()
        for method, field_index in (("GetVector", 0), ("EchoVector", 0), ("EchoVector", 1), ("WriteVector", 0)):
            for alteration in ("absent", "path", "type", "width", "array", "overlap_components", "direction", "marker"):
                if alteration == "marker" and method != "WriteVector":
                    continue
                listing = fixture.functions(method)
                parameter = listing["items"][0]["parameters"][field_index]
                if alteration == "absent": parameter.pop("struct")
                elif alteration == "path": parameter["struct"]["struct_path"] = "/Script/CoreUObject.Rotator"
                elif alteration == "type": parameter["struct"]["components"][0]["type"] = "FloatProperty"
                elif alteration == "width": parameter["struct"]["properties_size"] = 12
                elif alteration == "array": parameter["struct"]["components"][0]["array_dim"] = True
                elif alteration == "overlap_components": parameter["struct"]["components"][1]["offset"] = 0
                elif alteration == "direction": parameter["direction"] = "inout"
                else: parameter["argument_marker"] = {"out": False}
                with self.subTest(method=method, field=field_index, alteration=alteration), self.assertRaises(probe.ProbeError) as caught:
                    probe.check_signature(listing, method, 24)
                self.assertEqual(caught.exception.code, "probe_signature_vector_metadata")

    def test_repeated_cancel_during_cleanup_never_repeats_mutation(self):
        async def scenario():
            fixture = VectorFixture()
            reset_started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "EchoVector" and not expected_error:
                    await fixture.call(name, arguments)
                    raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe":
                    reset_started.set(); await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(reset_started.wait(), 2)
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
            self.assertEqual(sum(name == "g1r_invoke" and arguments["function"] == "EchoVector" for name, arguments, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
