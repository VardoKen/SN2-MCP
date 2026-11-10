"""Изолированная приёмка отказов контрактов и восстановления своей fixture."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("probe_contract_test", TOOLS / "probe_function_contract_mcp.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ContractFixture:
    def __init__(self, *, mutation_on_refusal=False, omit_effect=False, wrong_order=False,
                 bad_reset=False, missing_token=False):
        self.target = {"handle": "obj-1", "name": "Default__" + probe.HOST, "class": probe.HOST,
                       "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.tokens = {"ReadMarker": "sig-1", "ReadCallsCount": "sig-2", "EchoName": "sig-3", "ResetProbe": "sig-4"}
        self.count = 0
        self.calls = []
        self.resets = 0
        self.mutation_on_refusal = mutation_on_refusal
        self.omit_effect = omit_effect
        self.wrong_order = wrong_order
        self.bad_reset = bad_reset
        self.missing_token = missing_token

    def properties(self, names):
        fields = []
        for name in names:
            if name == "MissingForProbe":
                fields.append({"name": name, "type": None, "supported": False,
                               "error": {"code": "property_not_found"}})
            else:
                fields.append({"name": name, "supported": True,
                               "type": "IntProperty" if name == "CallsCount" else "NameProperty",
                               "value": self.count if name == "CallsCount" else "None" if name == "StoredNone" else "Entry"})
        if self.wrong_order: fields.reverse()
        return {"items": fields, "partial": "MissingForProbe" in names, "truncated": False, "scanned_fields": 3}

    def functions(self, query):
        inputs, output = probe.check_input_signature.__globals__["INPUT_METHODS"][query]
        params = []
        offset = 0
        for name, type_name, size in inputs:
            params.append({"name": name, "type": type_name, "element_size": size, "offset": offset,
                           "array_dim": 1, "return": False, "out": False, "reference": False})
            offset += size
        parameter_size = offset
        if output is not None:
            type_name, size = output
            params.append({"name": "ReturnValue", "type": type_name, "element_size": size, "offset": offset,
                           "array_dim": 1, "return": True, "out": True, "reference": False})
            if not inputs: parameter_size = size
        return {"items": [{"name": query, "parameter_size": parameter_size, "parameters": params,
                           "parameters_truncated": False, "signature_token": None if self.missing_token else self.tokens[query]}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, arguments, expected_error))
        if expected_error:
            if name == "g1r_invoke" and self.mutation_on_refusal:
                self.count += 1
            result = {"code": expected_error, "executed": False}
            if not self.omit_effect: result["may_have_executed"] = False
            return result
        if name == "g1r_native_status": return {"ready": True, "session_id": "native-owner"}
        if name == "g1r_find_objects": return {"items": [self.target]}
        if name == "g1r_object_functions": return self.functions(arguments["function_query"])
        if name == "g1r_object_properties": return self.properties(arguments["property_names"])
        if name == "g1r_invoke":
            method = arguments["function"]
            if arguments.get("expected_signature_token") != self.tokens[method]: raise AssertionError("wrong positive token")
            if method == "ReadMarker": return {"executed": True, "result": 71}
            if method == "ReadCallsCount": return {"executed": True, "result": self.count}
            if method == "ResetProbe":
                self.resets += 1
                self.count = 0
                return {"executed": not self.bad_reset, "result": None}
        raise AssertionError((name, arguments))


class FunctionContractProbeTests(unittest.TestCase):
    def test_complete_read_only_contract_series_and_one_confirmed_reset(self):
        fixture = ContractFixture()
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["rejections"], ["invalid_signature_token", "unknown_signature_token",
                                               "signature_token_mismatch"] + ["invalid_property_names"] * 4)
        self.assertEqual(report["cleanup"], "ResetProbe_and_confirmed_names_zero_counter")
        self.assertEqual(report["changed_runtime_signature"], "not_tested")
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.resets, 1)
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"}
                             for name, _, _ in fixture.calls))
        invalid_calls = [arguments for name, arguments, error in fixture.calls if name == "g1r_invoke" and error]
        self.assertTrue(all(arguments["function"] == "EchoName" for arguments in invalid_calls))
        self.assertEqual(fixture.calls[-1][1]["function"], "ReadCallsCount")

    def test_missing_token_and_wrong_order_refuse_before_echo_or_reset(self):
        for fixture, code in ((ContractFixture(missing_token=True), "probe_signature_token_absent"),
                              (ContractFixture(wrong_order=True), "probe_property_selection_invalid")):
            with self.subTest(code=code), self.assertRaises(probe.ProbeError) as caught:
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 0)
            self.assertFalse(any(name == "g1r_invoke" and arguments["function"] == "EchoName"
                                 for name, arguments, _ in fixture.calls))

    def test_false_refusal_effect_or_missing_metadata_triggers_reset(self):
        for fixture, code in ((ContractFixture(mutation_on_refusal=True), "probe_contract_call_unconfirmed"),
                              (ContractFixture(omit_effect=True), "probe_contract_rejection_unconfirmed")):
            with self.subTest(code=code), self.assertRaises(probe.ProbeError) as caught:
                asyncio.run(probe.exercise(fixture.call))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)

    def test_reset_must_be_executed(self):
        fixture = ContractFixture(bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_contract_cleanup_unconfirmed")
        self.assertEqual(fixture.resets, 1)

    def test_token_and_missing_property_validation_is_exact(self):
        fixture = ContractFixture()
        for token in ("sig-0", "sig-01", "sig--1", "sig-1\n", "sig-" + "9" * 21, ""):
            listing = fixture.functions("ReadMarker")
            listing["items"][0]["signature_token"] = token
            with self.subTest(token=token), self.assertRaises(probe.ProbeError):
                probe.check_token(listing, "ReadMarker")
        for alteration in ("fake_value", "fake_supported", "scan_incomplete"):
            selected = fixture.properties(probe.SELECTED_NAMES)
            missing = selected["items"][2]
            if alteration == "fake_value": missing["value"] = None
            elif alteration == "fake_supported": missing["supported"] = True
            else: missing["error"]["code"] = "property_scan_truncated"
            with self.subTest(alteration=alteration), self.assertRaises(probe.ProbeError):
                probe.check_selected(selected, probe.SELECTED_NAMES, include_missing=True)

    def test_primary_failure_survives_failed_cleanup(self):
        fixture = ContractFixture(mutation_on_refusal=True, bad_reset=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_contract_call_unconfirmed")
        self.assertTrue(any("NameProbe cleanup was not confirmed" in note
                            for note in caught.exception.__notes__))
        self.assertEqual(fixture.resets, 1)

    def test_cancellation_during_refusal_waits_for_single_reset(self):
        async def scenario():
            fixture = ContractFixture()
            reset_started, release = asyncio.Event(), asyncio.Event()
            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and expected_error:
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
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(fixture.resets, 1)
            self.assertEqual(fixture.count, 0)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
