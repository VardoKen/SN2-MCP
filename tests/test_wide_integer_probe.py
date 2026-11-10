"""Изолированная приёмка integer-пробы: границы, реальный эффект и защищённая очистка."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("wide_integer_probe_test", TOOLS / "probe_wide_integer_mcp.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class IntegerFixture:
    def __init__(self, *, missing_token=False, wrong_signature=False, bad_marker=False, nonzero_count=False,
                 float_result=False, mutate_on_refusal=False, missing_effect=False, bad_reset=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {method: "sig-" + str(index + 1) for index, method in enumerate(probe.METHODS)}
        self.count = 1 if nonzero_count else 0
        self.resets = 0
        self.calls = []
        self.missing_token, self.wrong_signature = missing_token, wrong_signature
        self.bad_marker, self.float_result = bad_marker, float_result
        self.mutate_on_refusal, self.missing_effect, self.bad_reset = mutate_on_refusal, missing_effect, bad_reset

    def functions(self, method):
        inputs, returned = probe.METHODS[method]
        parameters, offset = [], 0
        for name, type_name, size in inputs:
            parameters.append({"name": name, "type": type_name, "element_size": size, "offset": offset,
                               "array_dim": 1, "return": False, "out": False, "reference": False})
            offset += size
        parameter_size = offset
        if returned is not None:
            type_name, size = returned
            parameters.append({"name": "ReturnValue", "type": type_name, "element_size": size, "offset": offset,
                               "array_dim": 1, "return": True, "out": True, "reference": False})
            if not inputs:
                parameter_size = size
        if self.wrong_signature and method == "EchoUInt64":
            parameters[0]["reference"] = True
        return {"items": [{"name": method, "parameter_size": parameter_size, "parameter_count": len(parameters), "parameters": parameters,
                           "parameters_truncated": False, "signature_token": None if self.missing_token else self.tokens[method]}],
                "scan_complete": True, "scan_truncated": False, "truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, arguments, expected_error))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions":
            return self.functions(arguments["function_query"])
        if name != "g1r_invoke":
            raise AssertionError((name, arguments))
        method = arguments["function"]
        if arguments["expected_signature_token"] != self.tokens[method]:
            raise AssertionError("token not matched")
        returned = probe.METHODS[method][1]
        return_type = returned[0] if returned is not None else ""
        if method == "ReadMarker":
            return {"executed": True, "result": 999 if self.bad_marker else probe.MARKER, "return_type": return_type}
        if method == "ReadCallsCount":
            return {"executed": True, "result": self.count, "return_type": return_type}
        if method == "ResetProbe":
            self.resets += 1
            self.count = 0
            return {"executed": not self.bad_reset, "result": None, "return_type": ""}
        value = arguments["arguments"]["Input"]
        if expected_error:
            if self.mutate_on_refusal:
                self.count += 1
            result = {"code": "invalid_argument", "request_id": "native-request-1",
                      "effect": {"executed": False, "may_have_executed": False, "do_not_retry": False}}
            if not self.missing_effect:
                result["may_have_executed"] = False
            return result
        self.count += 1
        return {"executed": True, "result": float(value) if self.float_result else value, "return_type": return_type}


class WideIntegerProbeTests(unittest.TestCase):
    def test_full_boundaries_exact_values_refusals_and_one_reset(self):
        fixture = IntegerFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["completed_calls"], 11)
        self.assertEqual(report["successes"]["EchoUInt64"][-1], 2**64 - 1)
        self.assertIn(-(2**53 + 1), report["successes"]["EchoInt64"])
        self.assertEqual(sum(map(len, report["rejections"].values())), 18)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(report["cleanup"], ["ResetProbe_and_confirmed_zero_counter_marker151"])
        invalid = [arguments for name, arguments, expected in fixture.calls if expected]
        self.assertTrue(any(type(item["arguments"]["Input"]) is float for item in invalid))
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_signature_token_marker_and_initial_count_fail_before_echo(self):
        for kwargs, code in (({"missing_token": True}, "probe_signature_token_absent"),
                             ({"wrong_signature": True}, "probe_signature_mismatch"),
                             ({"bad_marker": True}, "probe_integer_result"),
                             ({"nonzero_count": True}, "probe_integer_result")):
            fixture = IntegerFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError) as caught:
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(caught.exception.code, code)
            self.assertFalse(any(name == "g1r_invoke" and arguments["function"].startswith("Echo") for name, arguments, _ in fixture.calls))
            self.assertEqual(fixture.resets, 0)

    def test_precision_loss_after_started_echo_triggers_one_reset(self):
        fixture = IntegerFixture(float_result=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_integer_result")
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)

    def test_refusal_effect_and_missing_metadata_are_not_accepted(self):
        for kwargs in ({"mutate_on_refusal": True}, {"missing_effect": True}):
            fixture = IntegerFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError):
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
        valid = {"code": "invalid_argument", "may_have_executed": False,
                 "effect": {"executed": False, "may_have_executed": False}}
        probe.check_integer_rejection(valid, "EchoUInt64", -1)
        probe.check_integer_rejection({"code": "invalid_argument", "may_have_executed": False,
                                       "executed": False}, "EchoUInt64", -1)
        for altered in ({"effect": {}}, {"effect": {"executed": True}},
                        {"effect": {"executed": None}}, {"effect": {"executed": False, "may_have_executed": True}},
                        {"executed": True}, {"executed": None}, {"may_have_executed": None}):
            with self.subTest(altered=altered), self.assertRaises(probe.ProbeError) as caught:
                probe.check_integer_rejection({**valid, **altered}, "EchoUInt64", -1)
            diagnostic = str(caught.exception)
            self.assertIn('"method": "EchoUInt64"', diagnostic)
            self.assertIn('"input": -1', diagnostic)
            self.assertIn('"payload":', diagnostic)

    def test_actual_mcp_error_envelope_preserves_effect_without_promoting_executed(self):
        async def scenario():
            payload = {"code": "invalid_argument", "message": "Аргумент вне uint64", "may_have_executed": False,
                       "request_id": "real-shaped-1", "effect": {"executed": False, "may_have_executed": False, "do_not_retry": False}}
            class Session:
                calls = 0
                async def call_tool(self, name, arguments):
                    self.calls += 1
                    return SimpleNamespace(is_error=True, structured_content=None,
                                           content=[SimpleNamespace(type="text", text=json.dumps(payload))])
            session = Session()
            result = await probe.make_caller(session)("g1r_invoke", {"Input": -1}, expected_error="invalid_argument")
            self.assertNotIn("executed", result)
            self.assertEqual(result["effect"], payload["effect"])
            probe.check_integer_rejection(result, "EchoUInt64", -1)
            self.assertEqual(session.calls, 1)
        asyncio.run(scenario())

    def test_cleanup_failure_preserves_primary_error(self):
        fixture = IntegerFixture(float_result=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_integer_result")
        self.assertTrue(any("Wide-integer cleanup is not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_wrong_unsigned_signed_width_or_ref_signature_rejected(self):
        fixture = IntegerFixture()
        for alteration in ("type", "size", "array", "offset", "return_flag", "reference", "out", "declared_count"):
            functions = fixture.functions("EchoUInt64")
            parameter = functions["items"][0]["parameters"][0]
            if alteration == "type": parameter["type"] = "Int64Property"
            elif alteration == "size": parameter["element_size"] = 4
            elif alteration == "array": parameter["array_dim"] = True
            elif alteration == "offset": parameter["offset"] = 31
            elif alteration == "return_flag": parameter["return"] = None
            elif alteration == "reference": parameter["reference"] = True
            elif alteration == "out": parameter["out"] = True
            else: functions["items"][0]["parameter_count"] = 1
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError):
                probe.check_signature(functions, "EchoUInt64")

    def test_repeated_cancellation_during_cleanup_never_repeats_echo_or_reset(self):
        async def scenario():
            fixture = IntegerFixture()
            reset_started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"].startswith("Echo") and not expected_error:
                    await fixture.call(name, arguments, expected_error=expected_error)
                    raise asyncio.CancelledError()
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe":
                    reset_started.set()
                    await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)
            task = asyncio.create_task(probe.exercise(call))
            await asyncio.wait_for(reset_started.wait(), 2)
            task.cancel(); await asyncio.sleep(0); task.cancel(); await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
            echoes = [arguments for name, arguments, expected in fixture.calls if name == "g1r_invoke" and arguments["function"].startswith("Echo")]
            self.assertEqual(len(echoes), 1)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
