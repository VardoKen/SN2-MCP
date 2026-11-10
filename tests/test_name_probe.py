"""Изолированные проверки FName-fixture и её безопасного MCP-прохода."""
from __future__ import annotations

import asyncio
import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"


def load(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prepare = load("prepare_name_probe")
live = load("probe_name_mcp")
class_prepare = load("prepare_class_probe")


def functions():
    return {"items": [{"name": method, "parameter_size": size,
                      "parameters": [{"name": "ReturnValue", "type": field_type, "element_size": size,
                                      "offset": 0, "return": True, "out": True, "reference": False}]}
                     for method, (field_type, size) in live.METHODS.items()]}


class NameCallFixture:
    def __init__(self, *, wrong_return=False, false_change=False, wrong_owner=False, cleanup_unconfirmed=False):
        self.target = {"handle": "obj-1", "class": live.HOST, "name": "Default__" + live.HOST,
                       "path": "/Script/Angelscript.Default__" + live.HOST}
        self.calls = []
        self.wrong_return = wrong_return
        self.false_change = false_change
        self.wrong_owner = wrong_owner
        self.cleanup_unconfirmed = cleanup_unconfirmed
        self.active = False

    def snapshot(self):
        return {"session_id": "other-owner" if self.wrong_owner else "fixture-owner",
                "source": "live_native", "consistency": "single_tick_sequential",
                "partial": False, "truncated": False, "objects": [{**self.target,
                "properties": [{"name": name, "type": "NameProperty", "supported": True, "value": value}
                               for name, value in live.PROPERTIES.items()]}]}

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.target]}
        if name == "g1r_object_functions":
            return functions()
        if name == "g1r_invoke":
            method = arguments["function"]
            if method == "ReadMarker":
                return {"executed": True, "result": 71}
            if method in ("ReadText", "ReadEmptyText"):
                return {"executed": True, "result": "G1R MCP dev7" if method == "ReadText" else "", "cleanup_confirmed": not self.cleanup_unconfirmed}
            return {"executed": True, "result": "Wrong" if self.wrong_return else "Entry" if method == "ReadEntry" else "None"}
        if name == "g1r_snapshot":
            return self.snapshot()
        if name == "g1r_watch_start":
            self.active = True
            return {"watch_id": "watch-1"}
        if name == "g1r_watch_read":
            events = [{"kind": "baseline", "snapshot": self.snapshot()}]
            if self.false_change:
                events.append({"kind": "changed", "snapshot": self.snapshot()})
            return {"session_id": "fixture-owner", "state": "running", "events": events, "gap": False, "dropped_events": 0}
        if name == "g1r_watch_stop":
            self.active = False
            return {"cleanup_complete": True, "state": "stopped"}
        if name == "g1r_watch_forget":
            return {"cleanup_complete": True, "forgotten": True}
        raise AssertionError((name, arguments))


def input_functions():
    """Полная reflection собственной AS-fixture; возврат Echo лежит вне ParmsSize."""
    declarations = []
    for name, inputs, output in (
        ("ReadMarker", [], ("IntProperty", 4)),
        ("EchoName", [("Input", "NameProperty", 8)], ("NameProperty", 8)),
        ("ReadCallsCount", [], ("IntProperty", 4)),
        ("ResetProbe", [], None),
    ):
        parameters = []
        offset = 0
        for parameter_name, field_type, size in inputs:
            parameters.append({"name": parameter_name, "type": field_type, "element_size": size,
                               "offset": offset, "array_dim": 1, "return": False,
                               "reference": False, "out": False})
            offset += size
        parameter_size = offset
        if output is not None:
            field_type, size = output
            parameters.append({"name": "ReturnValue", "type": field_type, "element_size": size,
                               "offset": offset, "array_dim": 1, "return": True,
                               "reference": False, "out": True})
            if not inputs:
                parameter_size = size
        declarations.append({"name": name, "parameter_size": parameter_size,
                             "parameters_truncated": False, "parameters": parameters})
    return {"items": declarations, "truncated": False, "scan_complete": True, "scan_truncated": False}


class NameInputCallFixture:
    def __init__(self, *, wrong_return=False, invalid_executed=False,
                 wrong_cleanup=False, reset_executed=True):
        self.target = {"handle": "obj-1", "class": live.HOST, "name": "Default__" + live.HOST,
                       "path": "/Script/Angelscript.Default__" + live.HOST}
        self.names = {"StoredEntry": "Entry", "StoredNone": "None"}
        self.count = 0
        self.calls = []
        self.echo_values = []
        self.rejections = []
        self.resets = 0
        self.declarations = input_functions()
        self.wrong_return = wrong_return
        self.invalid_executed = invalid_executed
        self.wrong_cleanup = wrong_cleanup
        self.reset_executed = reset_executed

    def snapshot(self):
        properties = [{"name": name, "type": "NameProperty", "supported": True, "value": value}
                      for name, value in self.names.items()]
        properties.append({"name": "CallsCount", "type": "IntProperty", "supported": True, "value": self.count})
        return {"session_id": "fixture-owner", "source": "live_native",
                "consistency": "single_tick_sequential", "partial": False, "truncated": False,
                "objects": [{**self.target, "partial": False, "truncated": False, "properties": properties}]}

    def rejection_code(self, value):
        if not isinstance(value, dict) or set(value) != {"name_from"}:
            return "invalid_name_argument"
        source = value["name_from"]
        if not isinstance(source, dict) or set(source) != {"handle", "property_name"}:
            return "invalid_name_argument"
        if source["handle"] != self.target["handle"]:
            return "stale_handle"
        if source["property_name"] == "CallsCount":
            return "unsupported_name_source"
        if source["property_name"] not in self.names:
            return "property_not_found"
        return None

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.target], "truncated": False}
        if name == "g1r_object_functions":
            return copy.deepcopy(self.declarations)
        if name == "g1r_snapshot":
            return self.snapshot()
        if name == "g1r_invoke":
            method = arguments["function"]
            if method == "EchoName":
                value = arguments["arguments"]["Input"]
                code = self.rejection_code(value)
                if code is not None:
                    self.rejections.append((copy.deepcopy(value), code, self.count))
                    if self.invalid_executed:
                        self.count += 1
                    return {"code": code, "may_have_executed": False}
                if expected_error is not None:
                    raise AssertionError("Корректный source ошибочно отправлен как отказ")
                self.count += 1
                copied = self.names[value["name_from"]["property_name"]]
                self.echo_values.append(copied)
                return {"executed": True, "return_type": "NameProperty",
                        "result": "Wrong" if self.wrong_return else copied}
            if method == "ReadMarker":
                return {"executed": True, "return_type": "IntProperty", "result": 71}
            if method == "ReadCallsCount":
                return {"executed": True, "return_type": "IntProperty", "result": self.count}
            if method == "ResetProbe":
                self.resets += 1
                if not self.wrong_cleanup:
                    self.count = 0
                return {"executed": self.reset_executed, "return_type": "", "result": None}
        raise AssertionError((name, arguments, expected_error))

    def invoked_methods(self):
        return [arguments["function"] for name, arguments in self.calls if name == "g1r_invoke"]


class NameProbeTests(unittest.TestCase):
    def test_prepare_copies_own_source_and_keeps_runtime_unverified(self):
        original_class_constants = (class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT)
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "name-probe"
            result = prepare.prepare_probe(workspace, repository_root=repository)
            self.assertEqual((workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE).read_bytes(),
                             (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())
            self.assertEqual(result["package_name"], prepare.PACKAGE_NAME)
            self.assertEqual(result["expected"]["marker"], 71)
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertEqual(list(workspace.iterdir()), [workspace / prepare.PACKAGE_NAME])
        self.assertEqual((class_prepare.PACKAGE_NAME, class_prepare.SCRIPT_RELATIVE, class_prepare.FIXTURE_ROOT), original_class_constants)

    def test_reused_preparation_cleans_after_write_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "name-probe"
            with patch.object(class_prepare.PackageWorkspace, "write_package_file", side_effect=prepare.WorkspaceError("isolated_failure", "write failure")):
                with self.assertRaises(prepare.WorkspaceError):
                    prepare.prepare_probe(workspace, repository_root=repository)
            self.assertEqual(list(workspace.iterdir()), [])

    def test_existing_and_outside_destinations_are_not_modified(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / "artifacts").mkdir()
            workspace = repository / "artifacts" / "name-probe"
            prepare.prepare_probe(workspace, repository_root=repository)
            source = workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE
            original = source.read_bytes()
            with self.assertRaises(prepare.PackageError) as captured:
                prepare.prepare_probe(workspace, repository_root=repository)
            self.assertEqual(captured.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), original)
            with self.assertRaises(prepare.PackageError) as captured:
                prepare.prepare_probe(repository / "outside", repository_root=repository)
            self.assertEqual(captured.exception.code, "unsafe_path")

    def test_signatures_require_name_return_eight_bytes_and_no_input(self):
        declaration = functions()
        for method in live.METHODS:
            live.check_signature(declaration, method)
        declaration["items"][1]["parameters"][0]["type"] = "StrProperty"
        with self.assertRaises(live.ProbeError):
            live.check_signature(declaration, "ReadEntry")
        declaration = functions()
        declaration["items"][2]["parameters"].append({"name": "Input", "return": False, "type": "NameProperty", "element_size": 8})
        with self.assertRaises(live.ProbeError):
            live.check_signature(declaration, "ReadNone")

    def test_live_pass_repeats_returns_and_cleans_observation(self):
        fixture = NameCallFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(report["returns"], {"ReadEntry": {"text": "Entry", "repetitions": 3},
                                            "ReadNone": {"text": "None", "repetitions": 3},
                                            "ReadText": {"text": "G1R MCP dev7", "repetitions": 3},
                                            "ReadEmptyText": {"text": "", "repetitions": 3}})
        self.assertEqual(report["cleanup"], ["g1r_watch_stop", "g1r_watch_forget"])
        self.assertFalse(fixture.active)

    def test_wrong_return_or_snapshot_owner_rejects_without_watch(self):
        for fixture in (NameCallFixture(wrong_return=True), NameCallFixture(wrong_owner=True)):
            with self.subTest(fixture=fixture), self.assertRaises(live.ProbeError):
                asyncio.run(live.exercise(fixture.call))
            self.assertFalse(any(name == "g1r_watch_start" for name, _ in fixture.calls))

    def test_false_change_rejects_but_still_cleans_observation(self):
        fixture = NameCallFixture(false_change=True)
        with self.assertRaises(live.ProbeError) as captured:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(captured.exception.code, "probe_false_name_change")
        self.assertFalse(fixture.active)
        self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])

    def test_fstring_cleanup_must_be_explicitly_confirmed(self):
        fixture = NameCallFixture(cleanup_unconfirmed=True)
        with self.assertRaises(live.ProbeError) as captured:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(captured.exception.code, "probe_string_cleanup_unconfirmed")
        self.assertFalse(any(name == "g1r_watch_start" for name, _ in fixture.calls))

    def test_cancelled_name_watch_cleanup_still_forgets_observation(self):
        async def scenario():
            fixture = NameCallFixture()
            started, release = asyncio.Event(), asyncio.Event()

            async def call(name, arguments):
                result = await fixture.call(name, arguments)
                if name == "g1r_watch_stop":
                    started.set()
                    await release.wait()
                return result

            task = asyncio.create_task(live.exercise(call))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(fixture.active)
            self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])
        asyncio.run(scenario())


class NameInputProbeTests(unittest.TestCase):
    def test_entry_and_none_copy_exact_sources_and_reject_ten_inputs_before_execution(self):
        fixture = NameInputCallFixture()
        report = asyncio.run(live.exercise_name_inputs(fixture.call))
        self.assertEqual(fixture.echo_values, ["Entry", "None"])
        self.assertEqual([code for _, code, _ in fixture.rejections],
                         ["invalid_name_argument"] * 7 + ["unsupported_name_source", "property_not_found", "stale_handle"])
        self.assertEqual(report["rejections"], [code for _, code, _ in fixture.rejections])
        self.assertEqual([count for _, _, count in fixture.rejections], [2] * 10)
        echo_args = [args["arguments"]["Input"] for name, args in fixture.calls
                     if name == "g1r_invoke" and args["function"] == "EchoName"]
        self.assertEqual(echo_args[:2], [
            {"name_from": {"handle": "obj-1", "property_name": "StoredEntry"}},
            {"name_from": {"handle": "obj-1", "property_name": "StoredNone"}},
        ])
        self.assertEqual(len(echo_args), 12)
        self.assertEqual(report["cleanup"], ["ResetProbe_and_confirmed_names_zero_counter"])
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.names, {"StoredEntry": "Entry", "StoredNone": "None"})
        self.assertFalse(report["game_launched"])
        self.assertFalse(report["dll_loaded"])

    def test_signature_accepts_full_catalog_and_as_return_outside_parameter_size(self):
        declarations = input_functions()
        self.assertEqual(set(live.INPUT_METHODS), {"ReadMarker", "EchoName", "ReadCallsCount", "ResetProbe"})
        for name in live.INPUT_METHODS:
            live.check_input_signature(declarations, name)
        echo = live.check_input_signature(declarations, "EchoName")
        self.assertEqual(echo["parameter_size"], 8)
        self.assertEqual(echo["parameters"][1]["offset"], 8)

    def test_scan_parameter_and_buffer_contracts_are_required(self):
        cases = [
            ("truncated", True), ("scan_complete", False), ("scan_truncated", True),
            ("scan_complete", None),
        ]
        for key, value in cases:
            with self.subTest(catalog_field=key, value=value):
                declarations = input_functions()
                if value is None:
                    declarations.pop(key)
                else:
                    declarations[key] = value
                with self.assertRaises(live.ProbeError):
                    live.check_input_signature(declarations, "EchoName")
        cases = [
            ("parameters_truncated", True), ("parameters_truncated", None),
            ("parameter_size", 7), ("parameter_size", True),
        ]
        for key, value in cases:
            with self.subTest(method_field=key, value=value):
                declarations = input_functions()
                method = declarations["items"][1]
                if value is None:
                    method.pop(key)
                else:
                    method[key] = value
                with self.assertRaises(live.ProbeError):
                    live.check_input_signature(declarations, "EchoName")
        for index, key, value in (
            (0, "array_dim", None), (1, "array_dim", None), (0, "array_dim", 2),
            (0, "array_dim", True), (0, "out", True), (0, "reference", True),
            (0, "return", None), (0, "offset", 1), (0, "offset", -1),
            (0, "element_size", 4), (0, "type", "IntProperty"),
            (1, "reference", True), (1, "offset", 25), (1, "type", "StrProperty"),
        ):
            with self.subTest(index=index, parameter_field=key, value=value):
                declarations = input_functions()
                parameter = declarations["items"][1]["parameters"][index]
                if value is None:
                    parameter.pop(key)
                else:
                    parameter[key] = value
                with self.assertRaises(live.ProbeError):
                    live.check_input_signature(declarations, "EchoName")

    def test_missing_array_dim_rejects_before_any_invoke(self):
        fixture = NameInputCallFixture()
        fixture.declarations["items"][1]["parameters"][0].pop("array_dim")
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise_name_inputs(fixture.call))
        self.assertEqual(caught.exception.code, "probe_signature_mismatch")
        self.assertEqual(fixture.invoked_methods(), [])
        self.assertEqual(fixture.resets, 0)

    def test_wrong_initial_names_or_counter_do_not_echo_or_reset(self):
        for property_name, value in (("StoredEntry", "UserEntry"), ("StoredNone", "UserNone"), ("CallsCount", 3)):
            with self.subTest(property_name=property_name):
                fixture = NameInputCallFixture()
                if property_name == "CallsCount":
                    fixture.count = value
                else:
                    fixture.names[property_name] = value
                original = (copy.deepcopy(fixture.names), fixture.count)
                with self.assertRaises(live.ProbeError):
                    asyncio.run(live.exercise_name_inputs(fixture.call))
                self.assertNotIn("EchoName", fixture.invoked_methods())
                self.assertNotIn("ResetProbe", fixture.invoked_methods())
                self.assertEqual((fixture.names, fixture.count), original)

    def test_missing_boolean_null_or_unsupported_counter_is_not_zero(self):
        for value in ("missing", False, None, -1):
            with self.subTest(value=value):
                fixture = NameInputCallFixture()

                async def call(name, arguments, *, expected_error=None):
                    result = await fixture.call(name, arguments, expected_error=expected_error)
                    if name == "g1r_snapshot":
                        counter = result["objects"][0]["properties"][2]
                        if value == "missing":
                            counter.pop("value")
                        else:
                            counter["value"] = value
                    return result

                with self.assertRaises(live.ProbeError) as caught:
                    asyncio.run(live.exercise_name_inputs(call))
                self.assertEqual(caught.exception.code, "probe_counter_invalid")
                self.assertNotIn("EchoName", fixture.invoked_methods())
                self.assertEqual(fixture.resets, 0)
        snapshot = NameInputCallFixture().snapshot()
        snapshot["objects"][0]["properties"][2]["supported"] = False
        with self.assertRaises(live.ProbeError):
            live.input_snapshot_values(snapshot, NameInputCallFixture().target, "fixture-owner")

    def test_counter_read_receipt_must_be_typed_and_explicit_before_echo(self):
        for mode in ("missing", "null", "boolean", "wrong_type"):
            with self.subTest(mode=mode):
                fixture = NameInputCallFixture()

                async def call(name, arguments, *, expected_error=None):
                    result = await fixture.call(name, arguments, expected_error=expected_error)
                    if name == "g1r_invoke" and arguments["function"] == "ReadCallsCount":
                        if mode == "missing":
                            result.pop("result")
                        elif mode == "null":
                            result["result"] = None
                        elif mode == "boolean":
                            result["result"] = False
                        else:
                            result["return_type"] = "NameProperty"
                    return result

                with self.assertRaises(live.ProbeError) as caught:
                    asyncio.run(live.exercise_name_inputs(call))
                self.assertEqual(caught.exception.code, "probe_call_unconfirmed")
                self.assertNotIn("EchoName", fixture.invoked_methods())
                self.assertEqual(fixture.resets, 0)

    def test_wrong_echo_return_still_resets_once_and_restores_counter(self):
        fixture = NameInputCallFixture(wrong_return=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise_name_inputs(fixture.call))
        self.assertEqual(caught.exception.code, "probe_name_input_result")
        self.assertEqual(fixture.echo_values, ["Entry"])
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)
        self.assertEqual(fixture.invoked_methods()[-2:], ["ResetProbe", "ReadCallsCount"])

    def test_lost_echo_response_still_requires_reset(self):
        fixture = NameInputCallFixture()

        async def call(name, arguments, *, expected_error=None):
            result = await fixture.call(name, arguments, expected_error=expected_error)
            if name == "g1r_invoke" and arguments["function"] == "EchoName":
                raise ConnectionError("Потерян ответ после исполнения собственной fixture")
            return result

        with self.assertRaises(ConnectionError):
            asyncio.run(live.exercise_name_inputs(call))
        self.assertEqual(fixture.echo_values, ["Entry"])
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)

    def test_invalid_rejection_that_changes_counter_is_detected_and_cleaned(self):
        fixture = NameInputCallFixture(invalid_executed=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise_name_inputs(fixture.call))
        self.assertEqual(caught.exception.code, "probe_name_rejection_executed")
        self.assertEqual(len(fixture.rejections), 1)
        self.assertEqual(fixture.resets, 1)
        self.assertEqual(fixture.count, 0)

    def test_rejection_receipt_code_and_execution_state_are_both_required(self):
        for mode in ("wrong_code", "missing_execution_state", "may_have_executed"):
            with self.subTest(mode=mode):
                fixture = NameInputCallFixture()

                async def call(name, arguments, *, expected_error=None):
                    result = await fixture.call(name, arguments, expected_error=expected_error)
                    if expected_error is not None:
                        if mode == "wrong_code":
                            result["code"] = "other_error"
                        elif mode == "missing_execution_state":
                            result.pop("may_have_executed")
                        else:
                            result["may_have_executed"] = True
                    return result

                with self.assertRaises(live.ProbeError) as caught:
                    asyncio.run(live.exercise_name_inputs(call))
                self.assertEqual(caught.exception.code, "probe_name_rejection_executed")
                self.assertEqual(fixture.resets, 1)
                self.assertEqual(fixture.count, 0)

    def test_reset_execution_and_zero_snapshot_must_be_confirmed(self):
        for fixture, code in ((NameInputCallFixture(reset_executed=False), "probe_call_unconfirmed"),
                              (NameInputCallFixture(wrong_cleanup=True), "probe_cleanup_unconfirmed")):
            with self.subTest(code=code), self.assertRaises(live.ProbeError) as caught:
                asyncio.run(live.exercise_name_inputs(fixture.call))
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(fixture.resets, 1)

    def test_primary_echo_error_survives_failed_cleanup_with_note(self):
        fixture = NameInputCallFixture(wrong_return=True, wrong_cleanup=True)
        with self.assertRaises(live.ProbeError) as caught:
            asyncio.run(live.exercise_name_inputs(fixture.call))
        self.assertEqual(caught.exception.code, "probe_name_input_result")
        self.assertEqual(fixture.resets, 1)
        self.assertTrue(any("FName input cleanup was not confirmed" in note
                            for note in caught.exception.__notes__))

    def test_repeated_cancellation_waits_for_one_reset_and_its_verification(self):
        async def scenario():
            fixture = NameInputCallFixture()
            started, release = asyncio.Event(), asyncio.Event()

            async def call(name, arguments, *, expected_error=None):
                if name == "g1r_invoke" and arguments["function"] == "ResetProbe":
                    started.set()
                    await release.wait()
                return await fixture.call(name, arguments, expected_error=expected_error)

            task = asyncio.create_task(live.exercise_name_inputs(call))
            await asyncio.wait_for(started.wait(), timeout=2)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertEqual(fixture.count, 2)
            self.assertEqual(fixture.resets, 0)
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(fixture.count, 0)
            self.assertEqual(fixture.resets, 1)
            self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_snapshot", "g1r_invoke"])
            self.assertEqual(fixture.invoked_methods()[-2:], ["ResetProbe", "ReadCallsCount"])

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
