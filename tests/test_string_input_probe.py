"""FString probe: UTF16, exact effects, ownership diagnostics и single-reset без игры."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("string_input_contract_probe_test", TOOLS / "probe_string_input_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class StringFixture:
    def __init__(self, *, input_flags=0x182, bad_marker=False, bad_length=False, bad_echo=False,
                 missing_cleanup=False, cleanup_false=False, missing_effect=False, mutate_refusal=False, bad_reset=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {name: "sig-" + str(i + 1) for i, name in enumerate(probe.METHODS)}
        self.input_flags, self.bad_marker, self.bad_length, self.bad_echo = input_flags, bad_marker, bad_length, bad_echo
        self.missing_cleanup, self.cleanup_false = missing_cleanup, cleanup_false
        self.missing_effect, self.mutate_refusal, self.bad_reset = missing_effect, mutate_refusal, bad_reset
        self.count, self.resets, self.calls = 0, 0, []

    def functions(self, name):
        keys = ("Left", "Right") if name == "ConcatInputs" else ("Input",) if name in ("ReadEcho", "ReadInputLength") else ()
        fields, offset = [], 0
        for key in keys:
            flags = self.input_flags
            fields.append({"name": key, "type": "StrProperty", "element_size": 16, "offset": offset, "array_dim": 1,
                "return": False, "out": bool(flags & 0x100), "const": bool(flags & 0x2), "reference": bool(flags & 0x08000000),
                "flags": flags, "direction": "input", "passing": "const_reference", "argument_constraints": dict(probe.CONSTRAINTS)})
            offset += 16
        size = offset
        returned = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                    "ReadInputLength": ("IntProperty", 4), "ReadEcho": ("StrProperty", 16), "ConcatInputs": ("StrProperty", 16)}.get(name)
        if returned:
            fields.append({"name": "ReturnValue", "type": returned[0], "element_size": returned[1], "offset": offset,
                "array_dim": 1, "return": True, "out": True, "const": False, "reference": False, "flags": 0x580})
            if offset == 0: size = returned[1]
        return {"items": [{"name": name, "parameters": fields, "parameter_size": size, "parameter_count": len(fields),
                           "parameters_truncated": False, "signature_token": self.tokens[name], "flags": 1234}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name != "g1r_invoke": raise AssertionError((name, arguments))
        method, supplied = arguments["function"], arguments["arguments"]
        if arguments["expected_signature_token"] != self.tokens[method]: raise AssertionError("wrong token")
        if expected_error:
            keys, actual = (("Left", "Right") if method == "ConcatInputs" else ("Input",)), None
            for key in keys:
                if key not in supplied: actual = "missing_argument"; break
                value = supplied[key]
                if not isinstance(value, str) or "\0" in value or probe.utf16_units(value) > 32767:
                    actual = "invalid_argument"; break
            if actual is None and set(supplied) != set(keys): actual = "unknown_argument"
            if actual != expected_error: raise AssertionError(("wrong expected error", actual, expected_error))
            if self.mutate_refusal: self.count += 1
            return {"code": actual, "may_have_executed": False,
                    "effect": {} if self.missing_effect else {"executed": False, "may_have_executed": False}}
        if method == "ReadMarker": return {"executed": True, "return_type": "IntProperty", "result": 0 if self.bad_marker else probe.MARKER}
        if method == "ReadCallsCount": return {"executed": True, "return_type": "IntProperty", "result": self.count}
        if method == "ResetProbe":
            self.count = 0
            self.resets += 1
            return {"executed": not self.bad_reset, "return_type": "", "result": None}
        self.count += 1
        result = (probe.utf16_units(supplied["Input"]) if method == "ReadInputLength" else supplied["Input"] if method == "ReadEcho"
                  else supplied["Left"] + supplied["Right"])
        if self.bad_length and method == "ReadInputLength": result += 1
        if self.bad_echo and method == "ReadEcho": result += "!"
        response = {"executed": True, "return_type": "IntProperty" if method == "ReadInputLength" else "StrProperty", "result": result,
                    "cleanup_confirmed": True, "input_cleanup_confirmed": not self.cleanup_false}
        if self.missing_cleanup: response.pop("input_cleanup_confirmed")
        return response


class StringInputProbeTests(unittest.TestCase):
    def test_full_unicode_series_exact_utf16_cleanup_and_counter(self):
        fixture = StringFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["completed_mutating_calls"], 9)
        self.assertEqual(len(report["rejections"]), 16)
        self.assertEqual([item["result"] for item in report["calls"] if item["method"] == "ReadInputLength"], [0, 6, 6, 4])
        self.assertEqual(report["calls"][-1]["result"], "Gothic 😀")
        self.assertEqual(report["calls"][-1]["utf16_units"], 9)
        self.assertTrue(all(item["cleanup"] == {"cleanup_confirmed": True, "input_cleanup_confirmed": True} for item in report["calls"]))
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(set(report["reflected_functions"]), set(probe.METHODS))
        self.assertEqual(report["native_string"], "not_tested")
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_const_ref_without_out_is_valid(self):
        report = asyncio.run(probe.exercise(StringFixture(input_flags=0x08000082).call))
        self.assertEqual(report["completed_mutating_calls"], 9)

    def test_invalid_const_only_value_out_and_mutable_flags_reject_before_input_call(self):
        for flags in (0x82, 0x80, 0x180, 0x08000180):
            fixture = StringFixture(input_flags=flags)
            with self.subTest(flags=flags), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and args["function"] == "ReadInputLength" for name, args, _ in fixture.calls))

    def test_exact_signature_names_constraints_offsets_types_and_flags(self):
        for alteration in ("width", "array", "passing", "direction", "constraints", "flags", "overlap", "name", "returnref", "count"):
            fixture = StringFixture()
            listing = fixture.functions("ConcatInputs")
            item, field = listing["items"][0], listing["items"][0]["parameters"][0]
            if alteration == "width": field["element_size"] = 8
            elif alteration == "array": field["array_dim"] = True
            elif alteration == "passing": field["passing"] = "value"
            elif alteration == "direction": field["direction"] = "output"
            elif alteration == "constraints": field["argument_constraints"]["max_utf16_units"] = 65535
            elif alteration == "flags": field["const"] = False
            elif alteration == "overlap": item["parameters"][2]["offset"] = 16
            elif alteration == "name": field["name"] = "Other"
            elif alteration == "returnref": item["parameters"][2].update(reference=True, flags=0x08000580)
            else: item["parameter_count"] = 2
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "ConcatInputs")

    def test_bad_values_cleanup_effect_or_counter_are_not_accepted(self):
        fixture = StringFixture(bad_marker=True)
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.resets, 0)
        for kwargs in ({"bad_length": True}, {"bad_echo": True}, {"missing_effect": True}, {"mutate_refusal": True}):
            fixture = StringFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
        for kwargs in ({"missing_cleanup": True}, {"cleanup_false": True}, {"bad_length": True, "cleanup_false": True},
                       {"bad_echo": True, "missing_cleanup": True}):
            fixture = StringFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError) as caught:
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(caught.exception.code, "probe_string_cleanup_unconfirmed")
            self.assertEqual(fixture.resets, 0)

    def test_length_limits_are_utf16_and_error_codes_independent(self):
        self.assertEqual(probe.utf16_units("😀" * 16384), 32768)
        fixture = StringFixture()
        arguments = {"function": "ReadInputLength", "arguments": {"Input": "😀" * 16384},
                     "expected_signature_token": fixture.tokens["ReadInputLength"]}
        with self.assertRaises(AssertionError): asyncio.run(fixture.call("g1r_invoke", arguments, expected_error="missing_argument"))

    def test_primary_failure_keeps_reflection_and_cleanup_failure(self):
        fixture = StringFixture(bad_echo=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_scalar_out_result")
        self.assertTrue(any("reflected_functions" in note for note in caught.exception.__notes__))
        self.assertTrue(any("Owned FString CDO cleanup was not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_unknown_execution_or_ownership_skips_reset_without_retry(self):
        async def scenario(code):
            fixture = StringFixture()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadInputLength":
                    await fixture.call(name, arguments)
                    raise probe.ProbeError(code, "Результат или владение неизвестны")
                return await fixture.call(name, arguments, expected_error=expected_error)
            with self.assertRaises(probe.ProbeError) as caught: await probe.exercise(call)
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadInputLength" for name, args, _ in fixture.calls), 1)
        for code in (*probe.UNKNOWN_GAME, "game_result_error", "string_ownership_unknown", "string_input_quarantined", "string_inputs_quarantined"):
            with self.subTest(code=code): asyncio.run(scenario(code))

    def test_repeated_cancel_waits_one_reset_and_never_repeats_input(self):
        async def scenario():
            fixture = StringFixture()
            started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ReadInputLength":
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
            self.assertEqual(sum(name == "g1r_invoke" and args["function"] == "ReadInputLength" for name, args, _ in fixture.calls), 1)
        asyncio.run(scenario())


if __name__ == "__main__": unittest.main()
