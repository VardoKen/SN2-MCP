"""Изолированные проверки POD-пакета и подтверждений bounded array-пробы."""
from __future__ import annotations

import asyncio
import copy
import hashlib
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


prepare = load("prepare_pod_read_probe")
class_prepare = load("prepare_class_probe")
live = load("probe_array_mcp")


def reflected_functions():
    methods = []
    for name, (inputs, returned) in live.METHODS.items():
        parameters = []
        offset = 0
        for argument_name, argument_type, size in inputs:
            parameters.append({"name": argument_name, "type": argument_type, "element_size": size,
                               "offset": offset, "array_dim": 1, "return": False,
                               "out": False, "reference": False})
            offset += size
        parameter_size = offset
        if returned is not None:
            returned_type, size = returned
            parameters.append({"name": "ReturnValue", "type": returned_type, "element_size": size,
                               "offset": offset, "array_dim": 1, "return": True,
                               "out": True, "reference": False})
            if not inputs:
                parameter_size = size
        methods.append({"name": name, "parameters": parameters, "parameter_size": parameter_size,
                        "parameters_truncated": False})
    return {"items": methods, "truncated": False, "scan_complete": True, "scan_truncated": False}


class ArrayCallFixture:
    def __init__(self, *, defaults_wrong=False, missing_array_dim=False, initial_page_wrong=False,
                 filled_page_wrong=False, unconfirmed_cleanup=False):
        self.target = {"handle": "obj-1", "class": live.HOST, "name": "Default__" + live.HOST,
                       "path": "/Script/Angelscript.Default__" + live.HOST}
        self.numbers = [42] if defaults_wrong else []
        self.empty_numbers = []
        self.names = ["Entry", "None"]
        self.calls = []
        self.missing_array_dim = missing_array_dim
        self.initial_page_wrong = initial_page_wrong
        self.filled_page_wrong = filled_page_wrong
        self.unconfirmed_cleanup = unconfirmed_cleanup

    def page(self, arguments):
        property_name = arguments["property_name"]
        values = {"Numbers": self.numbers, "EmptyNumbers": self.empty_numbers, "Names": self.names}[property_name]
        offset, limit = arguments["offset"], arguments["limit"]
        items = [{"index": index, "supported": True, "value": value}
                 for index, value in enumerate(values[offset:offset + limit], offset)]
        next_offset = offset + len(items) if offset + len(items) < len(values) else None
        page = {"source": "live_native", "session_id": "fixture-owner", "handle": self.target["handle"],
                "property_name": property_name, "consistency": "single_tick_sequential",
                "element_type": "NameProperty" if property_name == "Names" else "IntProperty",
                "element_size": 8 if property_name == "Names" else 4, "array_dim": 1,
                "total_count": len(values), "offset": offset, "next_offset": next_offset,
                "has_more": next_offset is not None, "truncated": next_offset is not None,
                "partial": False, "items": items}
        if self.initial_page_wrong and property_name == "Names":
            page["session_id"] = "other-owner"
        if self.filled_page_wrong and property_name == "Numbers" and self.numbers and offset == 200:
            page["items"][0]["value"] = 17
        return page

    async def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "sn2_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "sn2_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "sn2_object_functions":
            functions = reflected_functions()
            if self.missing_array_dim:
                for method in functions["items"]:
                    for parameter in method["parameters"]:
                        parameter.pop("array_dim")
            return functions
        if name == "sn2_read_array":
            return self.page(arguments)
        if name == "sn2_invoke":
            method = arguments["function"]
            parameters = arguments["arguments"]
            if method == "ReadMarker":
                value = 101
            elif method == "ReadNumberCount":
                value = len(self.numbers)
            elif method == "ReadEmptyCount":
                value = len(self.empty_numbers)
            elif method == "ReadNameCount":
                value = len(self.names)
            elif method == "ReadNumberAt":
                index = parameters["Index"]
                value = self.numbers[index] if 0 <= index < len(self.numbers) else -1000000
            elif method == "ReadNameAt":
                index = parameters["Index"]
                value = self.names[index] if 0 <= index < len(self.names) else "None"
            elif method == "SetupFixture":
                self.numbers = [index * 3 - 7 for index in range(205)]
                value = None
            elif method == "ClearFixture":
                self.numbers = []
                value = None
            else:
                raise AssertionError(method)
            output = live.METHODS[method][1]
            return {"executed": not (method == "ClearFixture" and self.unconfirmed_cleanup),
                    "return_type": output[0] if output is not None else "", "result": value}
        raise AssertionError(name)


class ArrayPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name)
        (self.repository / "artifacts").mkdir()
        self.workspace = self.repository / "artifacts" / "pod-probe"

    def run_preparation(self):
        return prepare.prepare_probe(self.workspace, repository_root=self.repository)

    def test_exact_copy_keeps_fixture_and_shared_preparer_globals(self):
        shared_before = (class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT)
        fixture_before = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
        result = self.run_preparation()
        package = self.workspace / prepare.PACKAGE_NAME
        self.assertEqual((package / prepare.SCRIPT_RELATIVE).read_bytes(), fixture_before)
        self.assertEqual((prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes(), fixture_before)
        self.assertEqual(shared_before, (class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT))
        for relative, details in result["files"].items():
            raw = (package / relative).read_bytes()
            self.assertEqual(details["bytes"], len(raw))
            self.assertEqual(details["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["expected"]["marker"], 101)
        self.assertEqual(result["expected"]["initial_arrays"],
                         {"Numbers": [], "EmptyNumbers": [], "Names": ["Entry", "None"]})
        self.assertFalse(result["compiled"])
        self.assertFalse(result["game_launched"])
        self.assertFalse(result["dll_loaded"])
        self.assertEqual(result["runtime_validation"], "not_performed")
        self.assertEqual(list(self.workspace.iterdir()), [package])

    def test_existing_user_edit_and_non_artifacts_destination_are_preserved(self):
        self.run_preparation()
        source = self.workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE
        source.write_bytes(b"isolated user edit")
        with self.assertRaises(prepare.PackageError) as caught:
            self.run_preparation()
        self.assertEqual(caught.exception.code, "already_exists")
        self.assertEqual(source.read_bytes(), b"isolated user edit")
        destination = self.repository / "outside"
        with self.assertRaises(prepare.PackageError) as caught:
            prepare.prepare_probe(destination, repository_root=self.repository)
        self.assertEqual(caught.exception.code, "unsafe_path")
        self.assertFalse(destination.exists())

    def test_write_failure_removes_only_created_empty_package(self):
        with patch.object(class_prepare.PackageWorkspace, "write_package_file",
                          side_effect=prepare.WorkspaceError("isolated_failure", "write failed")):
            with self.assertRaises(prepare.WorkspaceError) as caught:
                self.run_preparation()
        self.assertEqual(caught.exception.code, "isolated_failure")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_foreign_file_appearing_during_failure_is_preserved(self):
        sentinels = []

        def fail_with_foreign_file(writer, *args, **kwargs):
            sentinel = writer.root / "User.txt"
            sentinel.write_bytes(b"user-owned content")
            sentinels.append(sentinel)
            raise prepare.WorkspaceError("isolated_failure", "write failed")

        with patch.object(class_prepare.PackageWorkspace, "write_package_file", fail_with_foreign_file):
            with self.assertRaises(prepare.WorkspaceError) as caught:
                self.run_preparation()
        self.assertEqual(caught.exception.code, "isolated_failure")
        self.assertEqual(len(sentinels), 1)
        self.assertEqual(sentinels[0].read_bytes(), b"user-owned content")
        self.assertTrue(any("Unsafe cleanup was skipped; remaining files were preserved" in note
                            for note in caught.exception.__notes__))


class ArrayRuntimeProofTests(unittest.TestCase):
    def test_full_pages_match_getters_and_cleanup_restores_only_own_array(self):
        fixture = ArrayCallFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(report["page_sizes"], [200, 5])
        self.assertEqual(report["names"], ["Entry", "None"])
        self.assertEqual(report["cleanup"], ["ClearFixture_and_confirmed_empty_array_pages"])
        self.assertEqual((fixture.numbers, fixture.empty_numbers, fixture.names), ([], [], ["Entry", "None"]))
        invoked = [arguments["function"] for name, arguments in fixture.calls if name == "sn2_invoke"]
        self.assertEqual(invoked.count("SetupFixture"), 1)
        self.assertEqual(invoked.count("ClearFixture"), 1)
        reads = [arguments for name, arguments in fixture.calls
                 if name == "sn2_read_array" and arguments["property_name"] == "Numbers" and arguments["limit"] == 200]
        self.assertEqual([arguments["offset"] for arguments in reads], [0, 200, 205])

    def test_initial_defaults_or_page_identity_are_rejected_without_mutation(self):
        for options in ({"defaults_wrong": True}, {"initial_page_wrong": True}):
            with self.subTest(options=options):
                fixture = ArrayCallFixture(**options)
                original = (fixture.numbers[:], fixture.empty_numbers[:], fixture.names[:])
                with self.assertRaises(live.ProbeError):
                    asyncio.run(live.exercise(fixture.call))
                self.assertEqual((fixture.numbers, fixture.empty_numbers, fixture.names), original)
                invoked = [arguments["function"] for name, arguments in fixture.calls if name == "sn2_invoke"]
                self.assertNotIn("SetupFixture", invoked)
                self.assertNotIn("ClearFixture", invoked)

    def test_actual_signature_requires_array_dim_before_any_call(self):
        fixture = ArrayCallFixture(missing_array_dim=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_signature_mismatch")
        self.assertFalse(any(name == "sn2_invoke" for name, _ in fixture.calls))

    def test_parameter_extent_and_scan_flags_are_verified(self):
        functions = reflected_functions()
        for method in live.METHODS:
            live.check_signature(functions, method)
        getter = live.check_signature(functions, "ReadNameAt")
        self.assertGreater(getter["parameters"][-1]["offset"] + getter["parameters"][-1]["element_size"],
                           getter["parameter_size"])
        for change in ("scan", "parameters", "input_out", "input_extent", "array_dim"):
            with self.subTest(change=change):
                invalid = copy.deepcopy(functions)
                method = next(method for method in invalid["items"] if method["name"] == "ReadNameAt")
                if change == "scan":
                    invalid["scan_complete"] = False
                elif change == "parameters":
                    method["parameters_truncated"] = True
                elif change == "input_out":
                    method["parameters"][0]["out"] = True
                elif change == "input_extent":
                    method["parameters"][0]["offset"] = 4
                else:
                    method["parameters"][0]["array_dim"] = 2
                with self.assertRaises(live.ProbeError):
                    live.check_signature(invalid, "ReadNameAt")

    def test_error_after_fill_still_clears_and_confirms_remaining_arrays(self):
        fixture = ArrayCallFixture(filled_page_wrong=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_array_values")
        self.assertEqual(fixture.numbers, [])
        self.assertEqual(fixture.names, ["Entry", "None"])
        self.assertEqual([arguments["function"] for name, arguments in fixture.calls
                          if name == "sn2_invoke" and arguments["function"] == "ClearFixture"], ["ClearFixture"])

    def test_clear_receipt_is_not_replaced_by_successful_local_mutation(self):
        fixture = ArrayCallFixture(unconfirmed_cleanup=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(caught.exception.code, "probe_call_unconfirmed")
        self.assertEqual(fixture.numbers, [])

    def test_page_metadata_and_values_do_not_treat_missing_as_empty(self):
        fixture = ArrayCallFixture()
        arguments = {"property_name": "Names", "offset": 0, "limit": 64}
        valid = fixture.page(arguments)
        for change in ("missing", "index", "partial", "inner", "offset", "next", "count", "unsupported"):
            with self.subTest(change=change):
                invalid = copy.deepcopy(valid)
                if change == "missing":
                    invalid["items"][0].pop("value")
                elif change == "index":
                    invalid["items"][1]["index"] = 0
                elif change == "partial":
                    invalid["partial"] = True
                elif change == "inner":
                    invalid["element_type"] = "ObjectProperty"
                elif change == "offset":
                    invalid["offset"] = True
                elif change == "next":
                    invalid["next_offset"] = 2
                elif change == "count":
                    invalid["total_count"] = 1
                else:
                    invalid["items"][0]["supported"] = False
                with self.assertRaises(live.ProbeError):
                    live.check_page(invalid, fixture.target, "fixture-owner", "Names",
                                    offset=0, limit=64, total=2, element_type="NameProperty", element_size=8)

    def test_repeated_cancel_waits_for_one_clear_and_remaining_confirmation(self):
        async def scenario():
            fixture = ArrayCallFixture()
            entered = asyncio.Event()
            release = asyncio.Event()

            async def call(name, arguments):
                if name == "sn2_invoke" and arguments["function"] == "ClearFixture":
                    entered.set()
                    await release.wait()
                return await fixture.call(name, arguments)

            task = asyncio.create_task(live.exercise(call))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(fixture.numbers, [])
            invoked = [arguments["function"] for name, arguments in fixture.calls if name == "sn2_invoke"]
            self.assertEqual(invoked.count("ClearFixture"), 1)
            self.assertEqual([name for name, _ in fixture.calls[-3:]], ["sn2_read_array"] * 3)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
