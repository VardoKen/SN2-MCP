"""Scalar-out проба подтверждает reflection, JSON-типы, отказы и одну очистку."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("scalar_out_contract_probe_test", TOOLS / "probe_scalar_out_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ScalarOutFixture:
    def __init__(self, *, float_width=8, bad_marker=False, bad_output=None, bad_metadata=False,
                 missing_effect=False, mutate_refusal=False, bad_reset=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.float_width, self.bad_marker, self.bad_output, self.bad_metadata = float_width, bad_marker, bad_output, bad_metadata
        self.missing_effect, self.mutate_refusal, self.bad_reset = missing_effect, mutate_refusal, bad_reset
        self.count, self.resets, self.calls = 0, 0, []

    def functions(self, name):
        fields, offset = [], 0
        if name == "GetOutputs":
            for key in probe.VALUES:
                kind, width = probe.TYPES.get(key, ("DoubleProperty" if self.float_width == 8 else "FloatProperty", self.float_width))
                offset = (offset + width - 1) // width * width
                metadata = {"type": kind, "element_size": width, "array_dim": 1}
                if kind == "BoolProperty":
                    metadata["bool_layout"] = {"field_size": 1, "byte_offset": 0, "byte_mask": 1, "field_mask": 255}
                fields.append({"name": key, "type": kind, "element_size": width, "offset": offset,
                               "array_dim": 1, "return": False, "out": True, "reference": False, "const": False,
                               "flags": 0x180, "direction": "output", "argument_marker": {"out": True}, "out_metadata": metadata})
                offset += width
        size = offset
        returned = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                    "GetOutputs": ("BoolProperty", 1)}.get(name)
        if returned:
            fields.append({"name": "ReturnValue", "type": returned[0], "element_size": returned[1],
                           "offset": offset, "array_dim": 1, "return": True, "out": True,
                           "reference": False, "const": False, "flags": 0x580})
            if offset == 0:
                size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size,
                           "parameter_count": len(fields), "parameters_truncated": False,
                           "signature_token": self.tokens[name], "owner": probe.HOST, "flags": 1234}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "sn2_native_status":
            return {"ready": True, "session_id": "native-owner"}
        if name == "sn2_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "sn2_object_functions":
            return self.functions(arguments["function_query"])
        if name != "sn2_invoke":
            raise AssertionError((name, arguments))
        method = arguments["function"]
        if arguments["expected_signature_token"] != self.tokens[method]:
            raise AssertionError("wrong token")
        if expected_error:
            supplied, actual_code = arguments["arguments"], None
            for key in probe.VALUES:
                if key not in supplied:
                    actual_code = "missing_argument"
                    break
                marker = supplied[key]
                if not isinstance(marker, dict) or set(marker) != {"out"} or marker["out"] is not True:
                    actual_code = "invalid_argument"
                    break
            if actual_code is None and set(supplied) != set(probe.VALUES):
                actual_code = "unknown_argument"
            if expected_error != actual_code:
                raise AssertionError(("wrong expected native error", expected_error, actual_code))
            if self.mutate_refusal:
                self.count += 1
            return {"code": actual_code, "may_have_executed": False,
                    "effect": {} if self.missing_effect else {"executed": False, "may_have_executed": False}}
        if method == "ReadMarker":
            return {"executed": True, "return_type": "IntProperty", "result": 9 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount":
            return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.resets += 1
            self.count = 0
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        if method == "GetOutputs":
            self.count += 1
            metadata = {field["name"]: field["out_metadata"] for field in self.functions(method)["items"][0]["parameters"]
                        if field["return"] is False}
            values = dict(probe.VALUES)
            if self.bad_output == "rounded": values["OutInt64"] = -(2**53)
            elif self.bad_output == "bool_as_int": values["OutBool"] = 1
            elif self.bad_output == "int_as_float": values["OutUInt64"] = float(2**64 - 1)
            elif self.bad_output == "float_as_int": values["OutFloat32"] = 1
            elif self.bad_output == "nan": values["OutFloat"] = float("nan")
            elif self.bad_output == "missing": values.pop("OutInt")
            elif self.bad_output == "extra": values["Unexpected"] = 0
            if self.bad_metadata:
                metadata["OutBool"]["bool_layout"]["field_mask"] = 2
            return {"executed": True, "return_type": "BoolProperty", "result": True,
                    "out_parameters": values, "out_parameter_metadata": metadata}
        raise AssertionError((name, arguments))


class ScalarOutProbeTests(unittest.TestCase):
    def test_complete_series_exact_outputs_full_reflection_50_refusals_single_reset(self):
        fixture = ScalarOutFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["out_parameters"], probe.VALUES)
        self.assertEqual(report["out_parameter_metadata"]["OutFloat"]["element_size"], 8)
        self.assertEqual(report["completed_mutating_calls"], 2)
        self.assertEqual(len(report["rejections"]), 50)
        self.assertEqual([entry["code"] for entry in report["rejections"]].count("invalid_argument"), 42)
        self.assertEqual([entry["code"] for entry in report["rejections"]].count("missing_argument"), 7)
        self.assertEqual(report["rejections"][-1]["code"], "unknown_argument")
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        for name in probe.METHODS:
            self.assertEqual(report["reflected_functions"][name], fixture.functions(name))
        mutations = [arguments["arguments"] for name, arguments, expected in fixture.calls
                     if name == "sn2_invoke" and arguments["function"] == "GetOutputs" and expected is None]
        self.assertEqual(list(mutations[0]), list(probe.VALUES))
        self.assertEqual(list(mutations[1]), list(reversed(probe.VALUES)))
        self.assertFalse(any(name in {"sn2_development_start", "sn2_development_stop", "sn2_native_load"}
                             for name, _, _ in fixture.calls))

    def test_float_keyword_width_is_live_reflection_dependent(self):
        fixture = ScalarOutFixture(float_width=4)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["out_parameter_metadata"]["OutFloat"]["type"], "FloatProperty")
        self.assertEqual(fixture.resets, 1)

    def test_wrong_marker_and_signature_fail_before_getoutputs(self):
        fixture = ScalarOutFixture(bad_marker=True)
        with self.assertRaises(probe.ProbeError):
            asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        self.assertFalse(any(name == "sn2_invoke" and args["function"] == "GetOutputs" for name, args, _ in fixture.calls))
        for alteration in ("out", "reference", "const", "direction", "marker", "width", "array", "count",
                           "overlap", "bounds", "duplicate", "flags", "metadata"):
            listing = ScalarOutFixture().functions("GetOutputs")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "out": field["out"] = False
            elif alteration == "reference": field["reference"] = True
            elif alteration == "const": field["const"] = True
            elif alteration == "direction": field["direction"] = "inout"
            elif alteration == "marker": field["argument_marker"] = {"out": 1}
            elif alteration == "width": field["element_size"] = 4
            elif alteration == "array": field["array_dim"] = True
            elif alteration == "count": item["parameter_count"] = 7
            elif alteration == "overlap": item["parameters"][-1]["offset"] = 0
            elif alteration == "bounds": field["offset"] = 64
            elif alteration == "duplicate": item["parameters"][1]["name"] = "OutBool"
            elif alteration == "flags": field["flags"] |= 0x08000000
            else: field.pop("out_metadata")
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError):
                probe.check_signature(listing, "GetOutputs")

    def test_bad_output_types_values_names_or_metadata_cleanup_once(self):
        for output in ("rounded", "bool_as_int", "int_as_float", "float_as_int", "nan", "missing", "extra"):
            fixture = ScalarOutFixture(bad_output=output)
            with self.subTest(output=output), self.assertRaises(probe.ProbeError):
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
        fixture = ScalarOutFixture(bad_metadata=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 1)

    def test_refusal_without_effect_or_with_counter_mutation_is_not_accepted(self):
        for kwargs in ({"missing_effect": True}, {"mutate_refusal": True}):
            fixture = ScalarOutFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError):
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)

    def test_primary_failure_survives_cleanup_failure_and_has_full_public_report(self):
        fixture = ScalarOutFixture(bad_output="rounded", bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_scalar_out_value")
        self.assertTrue(any("reflected_functions" in note and "OutUInt64" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned scalar-out CDO cleanup was not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_game_failures_skip_cleanup_and_never_repeat_getoutputs(self):
        async def scenario(code):
            fixture = ScalarOutFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "sn2_invoke" and arguments["function"] == "GetOutputs":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат исходного вызова неизвестен")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught:
                await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertTrue(any("Scalar out cleanup was skipped after an uncertain call or unavailable connection" in note
                                for note in caught.exception.__notes__))
            self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "GetOutputs" for name, args, _ in fixture.calls), 1)
        for code in probe.UNKNOWN_GAME:
            with self.subTest(code=code): asyncio.run(scenario(code))

    def test_processed_game_result_error_allows_one_separate_reset(self):
        async def scenario():
            fixture = ScalarOutFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "sn2_invoke" and arguments["function"] == "GetOutputs":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError("game_result_error", "Выполненный результат невалиден")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, "game_result_error")
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "GetOutputs" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())

    def test_exact_bool_layout_and_optional_raw_flags(self):
        fixture = ScalarOutFixture()
        listing = fixture.functions("GetOutputs")
        for field in listing["items"][0]["parameters"]: field.pop("flags")
        probe.check_signature(listing, "GetOutputs")
        for mask in (1, 2, 4, 128, 255):
            probe.check_metadata({"type": "BoolProperty", "element_size": 1, "array_dim": 1,
                    "bool_layout": {"field_size": 1, "byte_offset": 0, "byte_mask": mask, "field_mask": mask}}, "BoolProperty", 1)
            probe.check_metadata({"type": "BoolProperty", "element_size": 1, "array_dim": 1,
                    "bool_layout": {"field_size": 1, "byte_offset": 0, "byte_mask": mask, "field_mask": 255}}, "BoolProperty", 1)
        for mask in (0, 3, 256, True):
            with self.subTest(mask=mask), self.assertRaises(probe.ProbeError):
                probe.check_metadata({"type": "BoolProperty", "element_size": 1, "array_dim": 1,
                    "bool_layout": {"field_size": 1, "byte_offset": 0, "byte_mask": mask, "field_mask": mask}}, "BoolProperty", 1)
        for byte_mask, field_mask in ((2, 1), (1, 2), (255, 1), (3, 255), (0, 255)):
            with self.subTest(byte_mask=byte_mask, field_mask=field_mask), self.assertRaises(probe.ProbeError):
                probe.check_metadata({"type": "BoolProperty", "element_size": 1, "array_dim": 1,
                    "bool_layout": {"field_size": 1, "byte_offset": 0, "byte_mask": byte_mask, "field_mask": field_mask}}, "BoolProperty", 1)

    def test_actual_mcp_error_envelope_accepts_nested_false_and_rejects_conflicts(self):
        class Session:
            async def call_tool(self, name, arguments):
                error = {"code": "invalid_argument", "may_have_executed": False, "effect": {"executed": False}}
                return SimpleNamespace(is_error=True, content=[SimpleNamespace(type="text", text=__import__("json").dumps(error))])
        result = asyncio.run(probe.make_caller(Session())("sn2_invoke", {}, expected_error="invalid_argument"))
        probe.check_rejection(result, "GetOutputs", {"OutBool": None})
        for change in ({"executed": True}, {"effect": {}}, {"effect": {"executed": None}},
                       {"effect": {"executed": False, "may_have_executed": True}}):
            with self.subTest(change=change), self.assertRaises(probe.ProbeError):
                probe.check_rejection({**result, **change}, "GetOutputs", None)

    def test_missing_unknown_and_malformed_errors_are_distinct(self):
        fixture = ScalarOutFixture()
        valid = {name: {"out": True} for name in probe.VALUES}
        for supplied, expected in (({**valid, "OutBool": None}, "invalid_argument"),
                                   ({name: marker for name, marker in valid.items() if name != "OutInt"}, "missing_argument"),
                                   ({**valid, "UnknownForProbe": {"out": True}}, "unknown_argument")):
            arguments = {"function": "GetOutputs", "arguments": supplied,
                         "expected_signature_token": fixture.tokens["GetOutputs"]}
            with self.subTest(expected=expected):
                result = asyncio.run(fixture.call("sn2_invoke", arguments, expected_error=expected))
                probe.check_rejection(result, "GetOutputs", supplied, expected)
                wrong = "missing_argument" if expected == "invalid_argument" else "invalid_argument"
                with self.assertRaises(AssertionError):
                    asyncio.run(fixture.call("sn2_invoke", arguments, expected_error=wrong))
                with self.assertRaises(probe.ProbeError):
                    probe.check_rejection(result, "GetOutputs", supplied, wrong)

    def test_repeated_cancel_during_cleanup_never_repeats_mutation(self):
        async def scenario():
            fixture = ScalarOutFixture()
            reset_started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "sn2_invoke" and arguments["function"] == "GetOutputs" and not expected_error:
                    await fixture.call(name, arguments)
                    raise asyncio.CancelledError()
                if name == "sn2_invoke" and arguments["function"] == "ResetProbe":
                    reset_started.set()
                    await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(reset_started.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
            self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "GetOutputs" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
