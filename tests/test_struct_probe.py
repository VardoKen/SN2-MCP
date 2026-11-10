"""Проверки доказательств копируемого struct-чтения на собственном CDO."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("probe_struct_mcp", TOOLS / "probe_struct_mcp.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


def signature(method):
    field_type, size = live.METHODS[method]
    return {"items": [{"name": method, "parameter_size": size, "parameters_truncated": False,
                       "parameters": [{"name": "ReturnValue", "type": field_type, "element_size": size,
                                       "offset": 0, "return": True, "out": True, "reference": False, "array_dim": 1}]}],
            "truncated": False, "scan_complete": True}


class StructFixture:
    def __init__(self, *, widths=(4, 8), failure=None, scalar_double=False):
        self.target = {"handle": "obj-1", "class": live.HOST, "name": "Default__" + live.HOST,
                       "path": "/Script/Angelscript.Default__" + live.HOST}
        self.calls = []
        self.widths = dict(zip(live.FIELDS, widths))
        self.failure = failure
        self.scalar_double = scalar_double

    def result(self, name):
        width = self.widths[name]
        return {"source": "live_native", "session_id": "fixture-owner", "handle": "obj-1",
                "property_name": name, "struct_path": "/Script/ConfirmedFixture." + name,
                "consistency": "copied_bytes_then_metadata_revalidated", "partial": False, "truncated": False,
                "properties_size": width * 3, "min_alignment": width,
                "components": [{"name": field, "type": "FloatProperty" if width == 4 else "DoubleProperty",
                                "element_size": width, "offset": index * width, "value": value}
                               for index, (field, (_, value)) in enumerate(live.FIELDS[name].items())]}

    async def call(self, name, args):
        self.calls.append((name, args))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions":
            result = signature(args["function_query"])
            if self.scalar_double and args["function_query"] != "ReadMarker":
                result["items"][0]["parameter_size"] = 8
                result["items"][0]["parameters"][0].update(type="DoubleProperty", element_size=8)
            if self.failure == "missing_array_dim":
                result["items"][0]["parameters"][0].pop("array_dim")
            return result
        if name == "g1r_invoke":
            method = args["function"]
            field_type = "DoubleProperty" if self.scalar_double and method != "ReadMarker" else live.METHODS[method][0]
            if method == "ReadMarker":
                value = 101
            else:
                value = next(value for fields in live.FIELDS.values() for reader, value in fields.values() if reader == method)
            if self.failure == "wrong_initial_scalar" and method == "ReadVectorX":
                value = 0.0
            return {"executed": True, "return_type": field_type, "result": value}
        if name == "g1r_read_struct":
            result = self.result(args["property_name"])
            if self.failure == "wrong_owner":
                result["session_id"] = "other-owner"
            elif self.failure == "native_rejection":
                raise live.ProbeError("unsupported_struct_layout", "изолированный отказ native")
            elif self.failure == "overlap":
                result["components"][1]["offset"] = 0
            elif self.failure == "unstable_copy":
                count = sum(call == "g1r_read_struct" for call, _ in self.calls)
                if count == 2:
                    result["struct_path"] += "Changed"
            return result
        raise AssertionError((name, args))


class StructProbeTests(unittest.TestCase):
    def test_success_uses_reflected_float_and_double_layouts_and_only_read_methods(self):
        fixture = StructFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(report["structs"]["StoredVector"]["properties_size"], 12)
        self.assertEqual(report["structs"]["StoredRotation"]["properties_size"], 24)
        self.assertEqual(report["structs"]["StoredRotation"]["numeric_type"], "DoubleProperty")
        self.assertFalse(report["mutations_performed"])
        self.assertFalse(report["game_launched"])
        invoked = [args["function"] for name, args in fixture.calls if name == "g1r_invoke"]
        self.assertTrue(set(invoked) <= set(live.METHODS))
        self.assertNotIn("Setup", invoked)

    def test_old_signature_without_array_dim_stops_before_any_invoke_or_struct_read(self):
        fixture = StructFixture(failure="missing_array_dim")
        with self.assertRaises(live.ProbeError):
            asyncio.run(live.exercise(fixture.call))
        self.assertFalse(any(name in {"g1r_invoke", "g1r_read_struct"} for name, _ in fixture.calls))

    def test_initial_scalar_values_are_verified_before_struct_reader(self):
        fixture = StructFixture(failure="wrong_initial_scalar")
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_initial_values")
        self.assertFalse(any(name == "g1r_read_struct" for name, _ in fixture.calls))

    def test_invalid_layouts_values_and_metadata_refuse_claiming_component_proof(self):
        fixture = StructFixture()
        original = fixture.result("StoredVector")
        cases = []
        def change(operation):
            candidate = copy.deepcopy(original)
            operation(candidate)
            cases.append(candidate)
        change(lambda value: value.update(session_id="foreign"))
        change(lambda value: value.update(handle="obj-2"))
        change(lambda value: value.update(properties_size=24))
        change(lambda value: value.update(min_alignment=8))
        change(lambda value: value["components"][1].update(offset=0))
        change(lambda value: value["components"][2].update(name="W"))
        change(lambda value: value["components"][0].update(value=float("nan")))
        change(lambda value: value["components"][0].update(value=True))
        change(lambda value: value["components"][0].update(value=10**1000))
        change(lambda value: value["components"][0].update(type="IntProperty"))
        change(lambda value: value["components"].append(copy.deepcopy(value["components"][0])))
        change(lambda value: value["components"][0].update(extra="unconfirmed"))
        change(lambda value: value.update(error={"code": "invalid_layout", "message": "неподтверждённое чтение"}))
        change(lambda value: value.update(partial=True))
        change(lambda value: value.update(truncated=True))
        change(lambda value: value.update(consistency="unverified"))
        for index, value in enumerate(cases):
            with self.subTest(index=index), self.assertRaises(live.ProbeError):
                live.check_struct(value, fixture.target, "fixture-owner", "StoredVector")

    def test_native_rejection_is_not_retried_or_converted_to_success(self):
        fixture = StructFixture(failure="native_rejection")
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "unsupported_struct_layout")
        self.assertEqual(sum(name == "g1r_read_struct" for name, _ in fixture.calls), 1)

    def test_changed_struct_metadata_between_reads_does_not_pass(self):
        fixture = StructFixture(failure="unstable_copy")
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_struct_unstable")

    def test_bounded_scalar_signature_checks_full_scan_and_output_extent(self):
        original = signature("ReadVectorX")
        for mutation in (lambda value: value.update(truncated=True),
                         lambda value: value.update(scan_complete=False),
                         lambda value: value["items"][0].update(parameters_truncated=True),
                         lambda value: value["items"][0]["parameters"][0].update(offset=31),
                         lambda value: value["items"][0]["parameters"].append({"name": "Input"})):
            candidate = copy.deepcopy(original)
            mutation(candidate)
            with self.assertRaises(live.ProbeError):
                live.check_signature(candidate, "ReadVectorX")

    def test_reflected_return_name_is_not_assumed_when_return_flag_and_scalar_contract_match(self):
        reflected = signature("ReadVectorX")
        reflected["items"][0]["parameters"][0]["name"] = "ReturnValueGenerated"
        live.check_signature(reflected, "ReadVectorX")
        reflected["items"][0]["parameters"][0]["name"] = ""
        with self.assertRaises(live.ProbeError):
            live.check_signature(reflected, "ReadVectorX")

    def test_confirmed_double_getters_use_their_reflected_return_type(self):
        fixture = StructFixture(scalar_double=True)
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(len(report["structs"]), 2)
        for field_type, width in (("IntProperty", 4), ("FloatProperty", 8), ("DoubleProperty", 4)):
            reflected = signature("ReadVectorX")
            reflected["items"][0]["parameters"][0].update(type=field_type, element_size=width)
            with self.assertRaises(live.ProbeError):
                live.check_signature(reflected, "ReadVectorX")
        marker = signature("ReadMarker")
        marker["items"][0]["parameters"][0].update(type="DoubleProperty", element_size=8)
        with self.assertRaises(live.ProbeError):
            live.check_signature(marker, "ReadMarker")


if __name__ == "__main__":
    unittest.main()
