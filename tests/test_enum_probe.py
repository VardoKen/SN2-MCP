"""Проверки собственной enum-fixture и подтверждений выполнения через MCP-копии."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare_enum_probe")
live = load("probe_enum_mcp")
class_prepare = load("prepare_class_probe")


def functions():
    methods = []
    for name in live.METHODS:
        params = []
        if name in {"EchoMode", "StoreMode"}:
            params.append({"name": "Input", "type": "EnumProperty", "element_size": 1, "offset": 0,
                           "array_dim": 1, "return": False, "reference": False, "out": False})
        if name in {"ReadMode", "EchoMode", "ReadMarker"}:
            params.append({"name": "ReturnValue", "type": "IntProperty" if name == "ReadMarker" else "EnumProperty",
                           "element_size": 4 if name == "ReadMarker" else 1,
                           "offset": 1 if name == "EchoMode" else 0, "array_dim": 1,
                           "return": True, "reference": False, "out": True})
        size = 4 if name == "ReadMarker" else 1 if params else 0
        methods.append({"name": name, "parameter_size": size, "parameters": params})
    return {"items": methods, "truncated": False}


def metadata(value):
    return {"path": live.ENUM_PATH, "underlying_type": "ByteProperty", "name_status": "matched",
            "value_name": live.ENUM_NAME + "::" + live.VALUES[value], "matched_count": 1,
            "ambiguous": False, "reason": None}


class EnumCallFixture:
    def __init__(self, *, wrong_return=False, invalid_executed=False, wrong_cleanup=False,
                 wrong_signature=False, wrong_target=False):
        self.target = {"handle": "obj-1", "class": live.HOST, "name": "Default__" + live.HOST,
                       "path": "/Script/Angelscript.Default__" + live.HOST}
        self.mode = 0
        self.count = 0
        self.calls = []
        self.wrong_return = wrong_return
        self.invalid_executed = invalid_executed
        self.wrong_cleanup = wrong_cleanup
        self.wrong_signature = wrong_signature
        self.wrong_target = wrong_target
        self.resets = 0

    def snapshot(self):
        return {"source": "live_native", "consistency": "single_tick_sequential", "session_id": "session-fixture",
                "snapshot_id": "snapshot-" + str(len(self.calls)), "partial": False, "truncated": False,
                "objects": [{"handle": self.target["handle"], "path": self.target["path"], "class": self.target["class"],
                             "partial": False, "truncated": False,
                             "properties": [{"name": "StoredMode", "type": "EnumProperty", "supported": True,
                                             "value": self.mode, "enum": metadata(self.mode)},
                                            {"name": "CallsCount", "type": "IntProperty", "supported": True, "value": self.count}]}]}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "sn2_native_status":
            return {"ready": True, "session_id": "session-fixture"}
        if name == "sn2_find_objects":
            target = dict(self.target)
            if self.wrong_target: target["path"] = "/Script/Other.Default__" + live.HOST
            return {"items": [target], "truncated": False}
        if name == "sn2_object_functions":
            result = functions()
            if self.wrong_signature:
                next(method for method in result["items"] if method["name"] == "EchoMode")["parameters"][0]["reference"] = True
            return result
        if name == "sn2_snapshot":
            return self.snapshot()
        if name == "sn2_compare_snapshots":
            return {"comparable": True, "unknown_count": 0, "coverage": {"complete": True}, "truncated": False}
        if name == "sn2_invoke":
            method = arguments["function"]
            if expected_error is not None:
                if self.invalid_executed: self.count += 1
                return {"code": expected_error, "may_have_executed": False}
            if method == "ResetProbe":
                self.resets += 1
                if not self.wrong_cleanup:
                    self.mode = self.count = 0
                return {"executed": True, "return_type": "", "result": None}
            if method == "ReadMarker":
                return {"executed": True, "return_type": "IntProperty", "result": 81}
            if method == "StoreMode":
                self.mode = arguments["arguments"]["Input"]
                self.count += 1
                return {"executed": True, "return_type": "", "result": None}
            value = self.mode if method == "ReadMode" else arguments["arguments"]["Input"]
            if method == "EchoMode": self.count += 1
            returned = 1 if self.wrong_return and method == "EchoMode" and value == 7 else value
            return {"executed": True, "return_type": "EnumProperty", "result": returned, "return_enum": metadata(returned)}
        raise AssertionError((name, arguments, expected_error))


class EnumProbeTests(unittest.TestCase):
    def test_preparation_is_isolated_and_does_not_change_existing_preparer_globals(self):
        before = (class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "enum-probe"
            result = prepare.prepare_probe(workspace, repository_root=repository)
            self.assertEqual(result["package_name"], prepare.PACKAGE_NAME)
            self.assertEqual(result["expected"]["marker"], 81)
            self.assertEqual(result["expected"]["declared_values"], {"First": 0, "Second": 1, "Third": 7})
            self.assertEqual((workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE).read_bytes(),
                             (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())
            with self.assertRaises(Exception):
                prepare.prepare_probe(workspace, repository_root=repository)
        self.assertEqual(before, (class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT))

    def test_echo_as_return_outside_parms_size_is_valid(self):
        method = live.check_signature(functions(), "EchoMode")
        self.assertEqual(method["parameter_size"], 1)
        self.assertEqual(method["parameters"][1]["offset"], 1)

    def test_reflected_array_dim_is_required_before_any_enum_call(self):
        old_response = functions()
        for method in old_response["items"]:
            for parameter in method["parameters"]:
                parameter.pop("array_dim")
        with self.assertRaises(live.ProbeError) as caught:
            live.check_signature(old_response, "EchoMode")
        self.assertEqual(caught.exception.code, "probe_signature_mismatch")
        fixture = EnumCallFixture()
        original_call = fixture.call
        async def old_call(name, arguments, *, expected_error=None):
            if name == "sn2_object_functions":
                return old_response
            return await original_call(name, arguments, expected_error=expected_error)
        with self.assertRaises(live.ProbeError):
            asyncio.run(live.exercise(old_call))
        self.assertFalse(any(name == "sn2_invoke" for name, _ in fixture.calls))

    def test_wrong_signature_roles_type_width_or_ref_are_rejected(self):
        for change in ("reference", "array", "type", "size", "offset", "input_out"):
            with self.subTest(change=change):
                invalid = functions()
                param = next(method for method in invalid["items"] if method["name"] == "EchoMode")["parameters"][0]
                if change == "reference": param["reference"] = True
                elif change == "array": param["array_dim"] = 2
                elif change == "type": param["type"] = "IntProperty"
                elif change == "size": param["element_size"] = 16
                elif change == "offset": param["offset"] = 2
                else: param["out"] = True
                with self.assertRaises(live.ProbeError):
                    live.check_signature(invalid, "EchoMode")

    def test_known_values_are_by_number_and_invalid_metadata_is_rejected(self):
        for value in live.VALUES:
            self.assertEqual(live.check_enum(metadata(value), value), "ByteProperty")
        for value in (2, True, 1.0):
            with self.assertRaises(live.ProbeError):
                live.check_enum(metadata(0), value)
        invalid = metadata(7)
        invalid["value_name"] = live.ENUM_NAME + "::Second"
        with self.assertRaises(live.ProbeError):
            live.check_enum(invalid, 7)

    def test_full_sequence_increments_once_rejects_before_execution_and_restores_zero(self):
        fixture = EnumCallFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual((fixture.mode, fixture.count), (0, 0))
        self.assertEqual(report["declared_values"], [0, 1, 7])
        self.assertIn("invalid_args_rejected_before_execution_calls_count_unchanged", report["checks"])
        self.assertEqual(report["cleanup"], ["ResetProbe_and_confirmed_zero_snapshot"])
        invoked = [arguments["function"] for name, arguments in fixture.calls if name == "sn2_invoke"]
        self.assertEqual(invoked[0], "ReadMarker")
        self.assertEqual(invoked[-1], "ResetProbe")
        self.assertEqual(invoked.count("ResetProbe"), 1)
        self.assertFalse(any(name in {"sn2_development_start", "sn2_native_load"} for name, _ in fixture.calls))

    def test_return_mismatch_still_restores_both_fields(self):
        fixture = EnumCallFixture(wrong_return=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_enum_result")
        self.assertEqual((fixture.mode, fixture.count), (0, 0))

    def test_hidden_execution_on_rejected_input_is_detected(self):
        fixture = EnumCallFixture(invalid_executed=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_rejection_executed")
        self.assertEqual((fixture.mode, fixture.count), (0, 0))

    def test_cleanup_must_be_confirmed_by_snapshot(self):
        fixture = EnumCallFixture(wrong_cleanup=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_cleanup_unconfirmed")

    def test_unverified_target_and_signature_never_allow_reset_or_other_invokes(self):
        for options in ({"wrong_target": True}, {"wrong_signature": True}):
            with self.subTest(options=options):
                fixture = EnumCallFixture(**options)
                with self.assertRaises(live.ProbeError):
                    asyncio.run(live.exercise(fixture.call))
                self.assertFalse(any(name == "sn2_invoke" for name, _ in fixture.calls))

    def test_wrong_source_defaults_fail_before_any_reset_or_mutating_enum_call(self):
        fixture = EnumCallFixture()
        fixture.mode = 1
        fixture.count = 3
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_initial_state")
        self.assertEqual((fixture.mode, fixture.count), (1, 3))
        invoked = [arguments["function"] for name, arguments in fixture.calls if name == "sn2_invoke"]
        self.assertEqual(invoked, ["ReadMarker"])


if __name__ == "__main__":
    unittest.main()
