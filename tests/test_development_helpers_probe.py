"""Main/helper companion: current enum channel, own live targets и одна игра."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.test_hit_result_wrapper_probe import WrapperFixture
from tests.test_development_mcp_probe import FakeMCP

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("development_helpers_probe_test", TOOLS / "probe_development_helpers_contract.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)
import probe_development_mcp as development


class HelpersFixture(WrapperFixture):
    def __init__(self, *, channel=7, has_helper_world=True, has_gameplay_player=False,
                 bad_version=False, bad_enum=False):
        super().__init__()
        self.channel, self.has_helper_world, self.bad_version, self.bad_enum = channel, has_helper_world, bad_version, bad_enum
        self.has_gameplay_player = has_gameplay_player
        self.helper = {"handle": "obj-helper", "name": probe.HOST + "_0", "class": probe.HOST,
                       "path": "/Game/Menu.Menu." + probe.HOST + "_0"}
        self.helper_cdo = {"handle": "obj-helper-cdo", "name": "Default__" + probe.HOST, "class": probe.HOST,
                           "path": "/Script/Angelscript.Default__" + probe.HOST}
        self.helper_tokens = {name: "sig-" + str(i + 20) for i, name in enumerate(probe.METHODS)}

    def helper_functions(self, name):
        if name == "TraceImpactPoint":
            listing = super().functions(name)
            item = listing["items"][0]
            fields = item["parameters"]
            # Place extra scalar inputs before ActorsToIgnore, preserving alignment.
            fields[2]["offset"], fields[3]["offset"], fields[4]["offset"] = 56, 72, 96
            extras = [{"name": key, "type": kind, "element_size": width, "offset": offset, "array_dim": 1,
                       "return": False, "out": False, "const": False, "reference": False, "flags": 0x80}
                      for key, kind, width, offset in (("Channel", "ByteProperty", 1, 48), ("TraceComplex", "BoolProperty", 1, 49),
                                                       ("IgnoreSelf", "BoolProperty", 1, 50))]
            item.update(parameters=fields[:2] + extras + fields[2:], parameter_count=8, parameter_size=96,
                        signature_token=self.helper_tokens[name])
            return listing
        kind, width = {"GetHelperVersion": ("StrProperty", 16), "HasWorld": ("BoolProperty", 1),
                       "HasGameplayPlayer": ("BoolProperty", 1),
                       "GetVisibilityTraceChannel": ("ByteProperty", 1)}[name]
        return {"items": [{"name": name, "parameters": [{"name": "ReturnValue", "type": kind, "element_size": width,
                "offset": 0, "array_dim": 1, "return": True, "out": True, "const": False, "reference": False, "flags": 0x580}],
                "parameter_size": width, "parameter_count": 1, "parameters_truncated": False, "signature_token": self.helper_tokens[name]}],
                "truncated": False, "scan_complete": True, "scan_truncated": False}

    async def call(self, name, arguments, *, expected_error=None):
        if name == "g1r_find_objects" and arguments["class_query"] == probe.HOST:
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            return {"items": [self.helper_cdo, self.helper], "truncated": False}
        if name == "g1r_object_functions" and arguments["handle"] == self.helper["handle"]:
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            return self.helper_functions(arguments["function_query"])
        if name == "g1r_invoke" and arguments["handle"] == self.helper["handle"]:
            self.calls.append((name, copy.deepcopy(arguments), expected_error))
            method = arguments["function"]
            if arguments["expected_signature_token"] != self.helper_tokens[method]: raise AssertionError("helper token")
            if method == "GetHelperVersion":
                return {"executed": True, "return_type": "StrProperty", "result": "wrong" if self.bad_version else probe.HELPER_VERSION, "cleanup_confirmed": True}
            if method == "HasWorld": return {"executed": True, "return_type": "BoolProperty", "result": self.has_helper_world}
            if method == "HasGameplayPlayer":
                return {"executed": True, "return_type": "BoolProperty", "result": self.has_gameplay_player}
            if method == "GetVisibilityTraceChannel":
                return {"executed": True, "return_type": "ByteProperty", "result": self.channel, "return_enum": {
                    "path": "/Script/Engine.Other" if self.bad_enum else probe.ENUM_PATH, "underlying_type": "ByteProperty",
                    "name_status": "matched", "value_name": "ETraceTypeQuery::TraceTypeQuery7", "matched_count": 1,
                    "ambiguous": False, "reason": None}}
            if arguments["arguments"]["Channel"] != self.channel: raise AssertionError("channel guessed")
            return {"executed": True, "return_type": "BoolProperty", "result": False, "out_parameters": {"ImpactPoint": dict(probe.ZERO)},
                    "out_parameter_metadata": {"ImpactPoint": self.metadata()}, "cleanup_confirmed": True, "array_input_cleanup_confirmed": True}
        return await super().call(name, arguments, expected_error=expected_error)


class DevelopmentHelpersProbeTests(unittest.TestCase):
    def test_helper_version_is_independent_of_bridge_version(self):
        fixture = HelpersFixture()
        with patch.object(probe, "VERSION", "1.0.0-dev.50"):
            report = asyncio.run(probe.helper_exercise(fixture.call))
        self.assertEqual(report["version"], probe.HELPER_VERSION)
        self.assertTrue(report["trace"]["tested"])

    def test_main_and_helper_distinct_live_classes_current_nonzero_channel(self):
        fixture = HelpersFixture(channel=7)
        report = asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(report["main"]["live_instance"]["handle"], "obj-live")
        self.assertEqual(report["helper"]["target"]["handle"], "obj-helper")
        self.assertEqual(report["helper"]["trace"]["arguments"]["Channel"], 7)
        self.assertTrue(report["helper"]["trace"]["tested"])
        self.assertEqual(fixture.resets, 1)
        helper_calls = [args["function"] for name, args, _ in fixture.calls if name == "g1r_invoke" and args["handle"] == "obj-helper"]
        self.assertEqual(helper_calls, ["GetHelperVersion", "HasWorld", "HasGameplayPlayer",
                                        "GetVisibilityTraceChannel", "TraceImpactPoint"])

    def test_gameplay_player_ready_records_false_and_true_independently_of_world(self):
        for ready in (False, True):
            fixture = HelpersFixture(has_helper_world=True, has_gameplay_player=ready)
            with self.subTest(ready=ready):
                report = asyncio.run(probe.helper_exercise(fixture.call))
                self.assertTrue(report["has_world"])
                self.assertIs(report["gameplay_player_ready"], ready)
                self.assertIs(report["gameplay_player_result"]["result"], ready)
                self.assertTrue(report["trace"]["tested"])

    def test_gameplay_player_result_rejects_non_bool_before_trace(self):
        for value in (0, 1, None, "true"):
            fixture = HelpersFixture(has_gameplay_player=value)
            with self.subTest(value=value), self.assertRaises(probe.ProbeError) as caught:
                asyncio.run(probe.helper_exercise(fixture.call))
            self.assertEqual(caught.exception.code, "probe_scalar_out_result")
            self.assertFalse(any(name == "g1r_invoke" and arguments["handle"] == "obj-helper"
                                 and arguments["function"] == "TraceImpactPoint"
                                 for name, arguments, _ in fixture.calls))

    def test_gameplay_player_signature_requires_one_byte_bool(self):
        for kind, width in (("IntProperty", 4), ("BoolProperty", 4)):
            listing = HelpersFixture().helper_functions("HasGameplayPlayer")
            listing["items"][0]["parameters"][0].update(type=kind, element_size=width)
            with self.subTest(kind=kind, width=width), self.assertRaises(probe.ProbeError) as caught:
                probe.check_signature(listing, "HasGameplayPlayer")
            self.assertEqual(caught.exception.code, "probe_signature_mismatch")

    def test_wrong_version_world_or_enum_stops_before_helper_trace(self):
        for kwargs in ({"bad_version": True}, {"has_helper_world": False}, {"bad_enum": True}):
            fixture = HelpersFixture(**kwargs)
            with self.subTest(kwargs=kwargs), self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
            self.assertFalse(any(name == "g1r_invoke" and args["handle"] == "obj-helper" and args["function"] == "TraceImpactPoint"
                                 for name, args, _ in fixture.calls))

    def test_channel_failure_retains_exact_getter_result_before_validation(self):
        fixture = HelpersFixture(bad_enum=True)
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.helper_exercise(fixture.call))
        self.assertTrue(any('"channel_result"' in note and '"result": 7' in note and '"path": "/Script/Engine.Other"' in note
                            for note in caught.exception.__notes__))
        self.assertTrue(any('"version_result"' in note and '"world_result"' in note for note in caught.exception.__notes__))

    def test_missing_helper_preserves_actual_find_listing_without_any_helper_invocation(self):
        fixture = HelpersFixture()
        async def call(name, arguments, *, expected_error=None):
            result = await fixture.call(name, arguments, expected_error=expected_error)
            if name == "g1r_find_objects" and arguments["class_query"] == probe.HOST:
                return {"items": [], "truncated": False}
            return result
        with self.assertRaises(probe.ProbeError) as caught: asyncio.run(probe.helper_exercise(call))
        self.assertTrue(any('"find_result": {"items": [], "truncated": false}' in note for note in caught.exception.__notes__))
        self.assertFalse(any(name == "g1r_invoke" for name, _, _ in fixture.calls))

    def test_channel_ambiguous_missing_path_or_bool_value_not_guessed(self):
        valid = {"executed": True, "return_type": "ByteProperty", "result": 7, "return_enum": {"path": probe.ENUM_PATH,
            "underlying_type": "ByteProperty", "name_status": "matched", "value_name": "ETraceTypeQuery::TraceTypeQuery7",
            "matched_count": 1, "ambiguous": False, "reason": None}}
        self.assertEqual(probe.check_channel(valid), 7)
        for change in ("ambiguous", "bool", "missing", "reason"):
            result = copy.deepcopy(valid)
            if change == "ambiguous": result["return_enum"]["ambiguous"] = True
            elif change == "bool": result["result"] = True
            elif change == "missing": result["return_enum"].pop("path")
            else: result["return_enum"]["reason"] = "enum_unavailable"
            with self.subTest(change=change), self.assertRaises(probe.ProbeError): probe.check_channel(result)

    def test_actual_engine_unqualified_enum_label_and_null_reason_are_valid(self):
        actual = {"executed": True, "function": "GetVisibilityTraceChannel", "result": 0,
                  "return_type": "ByteProperty", "thread_id": 16424, "return_enum": {
                      "ambiguous": False, "matched_count": 1, "name_status": "matched", "path": probe.ENUM_PATH,
                      "reason": None, "underlying_type": "ByteProperty", "value_name": "TraceTypeQuery1"}}
        self.assertEqual(probe.check_channel(actual), 0)
        for change in ("emptylabel", "badpath", "ambiguous", "bool", "missingmeta", "overflow"):
            value = copy.deepcopy(actual)
            if change == "emptylabel": value["return_enum"]["value_name"] = ""
            elif change == "badpath": value["return_enum"]["path"] = "/Script/Engine.Other"
            elif change == "ambiguous": value["return_enum"]["ambiguous"] = True
            elif change == "bool": value["result"] = False
            elif change == "overflow": value["result"] = 256
            else: value.pop("return_enum")
            with self.subTest(change=change), self.assertRaises(probe.ProbeError): probe.check_channel(value)

    def test_helper_trace_rejects_extra_parameter_overlap_and_mutable_scalar(self):
        for change in ("count", "overlap", "const"):
            listing = HelpersFixture().helper_functions("TraceImpactPoint")
            item = listing["items"][0]
            if change == "count": item["parameter_count"] = 9
            elif change == "overlap": item["parameters"][3]["offset"] = 48
            else: item["parameters"][2].update(const=True, flags=0x82)
            with self.subTest(change=change), self.assertRaises(probe.ProbeError): probe.check_signature(listing, "TraceImpactPoint")

    def test_companion_forwarded_to_recipe_start_and_recheck_default_unchanged(self):
        async def scenario():
            fake = FakeMCP()
            async def callback(call): return {"ok": True}
            await development.development_cycle(fake.call, "artifacts/main", 251, probe=callback,
                                                companion_packages=["development/helpers/G1RMCPDevelopmentHelpers"])
            related = [args for name, args in fake.calls if name in {"g1r_development_recipe", "g1r_development_start"}]
            self.assertEqual(len(related), 3)
            self.assertTrue(all(args["companion_packages"] == ["development/helpers/G1RMCPDevelopmentHelpers"] for args in related))
            default = FakeMCP()
            await development.development_cycle(default.call, "artifacts/main", 251, probe=callback)
            self.assertTrue(all("companion_packages" not in args for name, args in default.calls
                                if name in {"g1r_development_recipe", "g1r_development_start"}))
        asyncio.run(scenario())

    def test_invalid_companion_shape_rejects_before_commands(self):
        for value in ("package", [None], [""], [True]):
            fake = FakeMCP()
            with self.subTest(value=value), self.assertRaises(development.ProbeError):
                asyncio.run(development.development_cycle(fake.call, "artifacts/main", 251, companion_packages=value))
            self.assertEqual(fake.calls, [])


if __name__ == "__main__": unittest.main()
