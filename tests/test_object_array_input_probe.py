"""Object-array probe: declaredclass, handles, cleanup и эффекты без игры."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("object_array_input_contract_probe_test", TOOLS / "probe_object_array_input_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ObjectArrayFixture:
    def __init__(self, *, input_flags=0x182, bad_marker=False, bad_result=False,
                 missing_cleanup=False, cleanup_false=False, missing_effect=False, mutate_refusal=False, bad_reset=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.input_flags, self.bad_marker, self.bad_result = input_flags, bad_marker, bad_result
        self.missing_cleanup, self.cleanup_false = missing_cleanup, cleanup_false
        self.missing_effect, self.mutate_refusal, self.bad_reset = missing_effect, mutate_refusal, bad_reset
        self.count, self.resets, self.calls = 0, 0, []

    def functions(self, name):
        fields, offset = [], 0
        if name in probe.INPUT_METHODS:
            flags = self.input_flags
            fields.append({"name": "Input", "type": "ArrayProperty", "element_size": 16, "offset": 0, "array_dim": 1,
                "return": False, "out": bool(flags & 0x100), "const": bool(flags & 0x2), "reference": bool(flags & 0x08000000),
                "flags": flags, "direction": "input", "passing": "const_reference", "argument_constraints": dict(probe.CONSTRAINTS),
                "array": {"inner_type": "ObjectProperty", "inner_element_size": 8, "inner_array_dim": 1, "inner_offset": 0,
                          "property_class": {"path": "/Script/CoreUObject.Object", "name": "Object"}}})
            offset = 16
        size = offset
        returned = ("BoolProperty", 1) if name == "ContainsSelf" else ("IntProperty", 4) if name != "ResetProbe" else None
        if returned:
            fields.append({"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset,
                "array_dim": 1, "return": True, "out": True, "const": False, "reference": False, "flags": 0x580})
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    def validate(self, supplied):
        if "Input" not in supplied: return "missing_argument"
        values = supplied["Input"]
        if not isinstance(values, list) or len(values) > 32: return "invalid_argument"
        for value in values:
            if value is None: continue
            if (not isinstance(value, dict) or set(value) != {"handle"} or not isinstance(value["handle"], str)
                    or not 1 <= len(value["handle"]) <= 80): return "invalid_object_argument"
            if value["handle"] != self.target["handle"]: return "stale_handle"
        return "unknown_argument" if set(supplied) != {"Input"} else None

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError((name, arguments))
        method, supplied = arguments["function"], arguments["arguments"]
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            actual = self.validate(supplied)
            if actual != expected_error: raise AssertionError(("wrong expected error", actual, expected_error))
            if self.mutate_refusal: self.count += 1
            return {"code": actual, "may_have_executed": False,
                    "effect": {} if self.missing_effect else {"executed": False, "may_have_executed": False,
                    "cleanup_confirmed": True, "array_input_cleanup_confirmed": True}}
        if method == "ReadMarker": return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.count = 0
            self.resets += 1
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        values = supplied["Input"]
        self.count += 1
        result = len(values) if method == "ReadCount" else values.count(None) if method == "ReadNullCount" else {"handle": self.target["handle"]} in values
        if self.bad_result: result = not result if type(result) is bool else result + 1
        response = {"executed": True, "return_type": "BoolProperty" if method == "ContainsSelf" else "IntProperty", "result": result,
                    "cleanup_confirmed": True, "array_input_cleanup_confirmed": not self.cleanup_false}
        if self.missing_cleanup: response.pop("array_input_cleanup_confirmed")
        return response


class ObjectArrayInputProbeTests(unittest.TestCase):
    def test_complete_array_cases32boundary_cleanup_refusals_and_counter(self):
        fixture = ObjectArrayFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["completed_mutating_calls"], 15)
        self.assertEqual(len(report["rejections"]), 18)
        self.assertEqual([item["result"] for item in report["calls"] if item["method"] == "ReadCount"], [0, 1, 1, 3, 32])
        self.assertEqual([item["result"] for item in report["calls"] if item["method"] == "ReadNullCount"], [0, 1, 0, 1, 0])
        self.assertEqual([item["result"] for item in report["calls"] if item["method"] == "ContainsSelf"], [False, False, True, True, True])
        self.assertTrue(all(item["cleanup"] == {"cleanup_confirmed": True, "array_input_cleanup_confirmed": True} for item in report["calls"]))
        self.assertTrue(all(item["cleanup"] for item in report["rejections"]))
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_const_ref_without_out_is_valid(self):
        report = asyncio.run(probe.exercise(ObjectArrayFixture(input_flags=0x08000082).call))
        self.assertEqual(report["completed_mutating_calls"], 15)

    def test_invalid_const_only_value_out_mutable_flags_reject_before_input(self):
        for flags in (0x82, 0x80, 0x180, 0x08000180):
            fixture = ObjectArrayFixture(input_flags=flags)
            with self.subTest(flags=flags), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] in probe.INPUT_METHODS for name, args, _ in fixture.calls))

    def test_exact_inner_declaredclass_layout_and_signature_are_strict(self):
        for alteration in ("type", "width", "dim", "offset", "class", "classname", "constraints", "passing", "flags", "span"):
            fixture = ObjectArrayFixture()
            listing = fixture.functions("ReadCount")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            metadata = field["array"]
            if alteration == "type": metadata["inner_type"] = "ClassProperty"
            elif alteration == "width": metadata["inner_element_size"] = 4
            elif alteration == "dim": metadata["inner_array_dim"] = True
            elif alteration == "offset": metadata["inner_offset"] = 8
            elif alteration == "class": metadata["property_class"]["path"] = "/Script/Engine.Actor"
            elif alteration == "classname": metadata["property_class"]["name"] = "Actor"
            elif alteration == "constraints": field["argument_constraints"]["max_elements"] = 64
            elif alteration == "passing": field["passing"] = "value"
            elif alteration == "flags": field["const"] = False
            else: item["parameters"][1]["offset"] = 0
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "ReadCount")

    def test_bad_values_or_refusal_effect_reset_once_cleanup_unknown_skips(self):
        for kwargs in ({"bad_result": True}, {"mutate_refusal": True}):
            fixture = ObjectArrayFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
        fixture = ObjectArrayFixture(missing_effect=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        for kwargs in ({"missing_cleanup": True}, {"cleanup_false": True}, {"bad_result": True, "cleanup_false": True}):
            fixture = ObjectArrayFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(caught.exception.code, "probe_object_array_cleanup_unconfirmed")
            self.assertEqual(fixture.resets, 0)

    def test_stale_and_malformed_errors_independent_of_expected_code(self):
        fixture = ObjectArrayFixture()
        for supplied, expected in (({"Input": [{"handle": "obj-18446744073709551615"}]}, "stale_handle"),
                                   ({"Input": [True]}, "invalid_object_argument"), ({"Input": [None] * 33}, "invalid_argument")):
            self.assertEqual(fixture.validate(supplied), expected)
            arguments = {"function": "ReadCount", "arguments": supplied, "expected_signature_token": fixture.tokens["ReadCount"]}
            with self.assertRaises(AssertionError): asyncio.run(fixture.call("g1r_invoke", arguments, expected_error="missing_argument"))

    def test_primary_failure_preserves_fullmetadata_and_cleanup_failure(self):
        fixture = ObjectArrayFixture(bad_result=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertTrue(any("reflected_functions" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned object-array CDO cleanup was not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_execution_or_quarantine_skips_reset_without_retry(self):
        async def scenario(code):
            fixture = ObjectArrayFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadCount":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат или владение неизвестны")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadCount" for name, args, _ in fixture.calls), 1)
        for code in (*probe.UNKNOWN_GAME, "game_result_error", "object_array_input_quarantined"):
            with self.subTest(code=code): asyncio.run(scenario(code))

    def test_unknown_refusal_envelope_stops_without_counter_reset_or_retry(self):
        async def scenario(use_mcp):
            fixture = ObjectArrayFixture()
            refusal_index = None
            async def direct(name, arguments, *, expected_error=None):
                nonlocal refusal_index
                result = await fixture.call(name, arguments, expected_error=expected_error)
                if expected_error:
                    refusal_index = len(fixture.calls) - 1
                    return {"code": expected_error, "may_have_executed": True,
                            "effect": {"executed": True, "may_have_executed": True, "do_not_retry": True}}
                return result
            if use_mcp:
                class Session:
                    async def call_tool(self, name, arguments):
                        # Actual shared caller validates the envelope before returning to exercise.
                        expected = fixture.validate(arguments["arguments"]) if name == "g1r_invoke" and arguments["function"] == "ReadCount" else None
                        result = await direct(name, arguments, expected_error=expected)
                        if expected:
                            return SimpleNamespace(is_error=True, content=[SimpleNamespace(type="text", text=json.dumps(result))])
                        return SimpleNamespace(is_error=False, structured_content=result)
                call = probe.make_caller(Session())
            else:
                call = direct
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, "probe_contract_rejection_unconfirmed" if use_mcp else "probe_scalar_out_rejection_unconfirmed")
            self.assertIsNotNone(refusal_index)
            self.assertEqual(fixture.calls[refusal_index + 1:], [])
            self.assertEqual(fixture.resets, 0)
            if not use_mcp:
                self.assertTrue(any('"may_have_executed": true' in note for note in caught.exception.__notes__))
        for use_mcp in (False, True):
            with self.subTest(use_mcp=use_mcp): asyncio.run(scenario(use_mcp))

    def test_refusal_false_cleanup_or_quarantine_stops_before_counter(self):
        async def scenario(details):
            fixture = ObjectArrayFixture()
            refusal_index = None
            async def call(name, arguments, *, expected_error=None):
                nonlocal refusal_index
                result = await fixture.call(name, arguments, expected_error=expected_error)
                if expected_error:
                    refusal_index = len(fixture.calls) - 1
                    result["effect"].update(details)
                return result
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, "probe_object_array_cleanup_unconfirmed")
            self.assertEqual(fixture.calls[refusal_index + 1:], [])
            self.assertEqual(fixture.resets, 0)
        for details in ({"cleanup_confirmed": False}, {"array_input_cleanup_confirmed": False}, {"object_array_input_quarantined": True},
                        {"cleanup_state": "unknown"}):
            with self.subTest(details=details): asyncio.run(scenario(details))

    def test_actual_released_refusal_without_allocation_or_quarantine_is_accepted(self):
        fixture = ObjectArrayFixture()
        async def call(name, arguments, *, expected_error=None):
            result = await fixture.call(name, arguments, expected_error=expected_error)
            if expected_error == "unknown_argument":
                return {"code": "unknown_argument", "message": "Переданы лишние аргументы", "may_have_executed": False,
                        "request_id": "actual-native-regression", "effect": {
                            "array_input_cleanup_confirmed": True, "cause_code": "unknown_argument", "cleanup_attempted": False,
                            "cleanup_confirmed": True, "cleanup_state": "released", "do_not_retry": False,
                            "executed": False, "may_have_executed": False, "object_array_input_quarantined": False}}
            return result
        report = asyncio.run(probe.exercise(call))
        self.assertEqual(len(report["rejections"]), 18)
        self.assertEqual(report["last_refusal"]["effect"]["cleanup_attempted"], False)
        self.assertEqual(report["last_refusal"]["effect"]["cleanup_state"], "released")
        self.assertEqual(report["last_refusal"]["effect"]["object_array_input_quarantined"], False)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)

    def test_repeated_cancel_waits_one_reset_and_never_repeats_array(self):
        async def scenario():
            fixture = ObjectArrayFixture()
            started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadCount":
                    await fixture.call(name, arguments)
                    raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe":
                    started.set()
                    await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel(); await asyncio.sleep(0)
            task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done()); release.set()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadCount" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
