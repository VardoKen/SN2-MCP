"""Изолированные проверки подготовки и управления собственной class-fixture."""
from __future__ import annotations

import asyncio
import hashlib
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


prepare = load("prepare_class_probe")
live = load("probe_class_mcp")


def target(name, index, *, host=False):
    object_name = "Default__" + name if host else name
    return {"handle": f"obj-{index}", "index": index, "name": object_name,
            "class": name if host else "ASClass", "path": "/Script/Angelscript." + object_name}


def signature(name):
    inputs, output = live.SIGNATURES[name]
    parameters, offset = [], 0
    for argument, field_type, size in inputs:
        parameters.append({"name": argument, "type": field_type, "element_size": size, "offset": offset,
                           "return": False, "out": False, "reference": False})
        offset += size
    input_span = offset
    if output:
        field_type, size = output
        parameters.append({"name": "ReturnValue", "type": field_type, "element_size": size, "offset": offset,
                           "return": True, "out": True, "reference": False})
        offset += size
    return {"name": name, "parameter_size": input_span if inputs else offset, "parameters": parameters}


class ClassCallFixture:
    def __init__(self, fail_store=False):
        self.host = target(live.HOST, 1, host=True)
        self.classes = {name: target(name, index) for index, name in enumerate(live.CLASS_NAMES, start=2)}
        self.fields = {name: self.descriptor(self.classes[live.CLASS_NAMES[0]]) for name in live.PROPERTIES}
        self.calls = []
        self.watch = False
        self.baseline = None
        self.changed = None
        self.fail_store = fail_store

    def descriptor(self, item):
        return {**item, "session_id": "fixture-owner"}

    def snapshot(self):
        return {"session_id": "fixture-owner", "source": "live_native", "consistency": "single_tick_sequential",
                "partial": False, "truncated": False, "objects": [{**self.host,
            "properties": [{"name": name, "type": "ClassProperty", "supported": True, "value": value}
                           for name, value in self.fields.items()]}]}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, arguments))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "g1r_find_objects":
            query = arguments["query"]
            return {"items": [self.host] if query.startswith("Default__") else [self.classes[query]]}
        if name == "g1r_object_functions":
            return {"items": [signature(name) for name in live.SIGNATURES]}
        if name == "g1r_snapshot":
            return self.snapshot()
        if name == "g1r_invoke":
            method, args = arguments["function"], arguments["arguments"]
            if method == "ReadMarker":
                return {"executed": True, "result": 61}
            if method == "ReadBaseClass":
                return {"executed": True, "result": self.descriptor(self.classes[live.CLASS_NAMES[0]])}
            value = args["Input"]
            if value is not None and value["handle"] == self.host["handle"]:
                if expected_error != "invalid_class_argument":
                    raise AssertionError("Обычный объект не должен приниматься как класс")
                return {"code": expected_error, "may_have_executed": False}
            referenced = None if value is None else next(item for item in self.classes.values() if item["handle"] == value["handle"])
            if method == "EchoTypedClass" and referenced is not None and referenced["name"] == live.CLASS_NAMES[2]:
                if expected_error != "incompatible_class_argument":
                    raise AssertionError("Нет ожидаемого отказа несовместимого класса")
                return {"code": expected_error, "may_have_executed": False}
            if method.startswith("Echo"):
                return {"executed": True, "result": None if referenced is None else self.descriptor(referenced)}
            if method.startswith("Store"):
                if self.fail_store and referenced is not None:
                    raise live.ProbeError("isolated_failure", "Сбой проверочной записи после начала наблюдения")
                field = "StoredAnyClass" if method == "StoreAnyClass" else "StoredTypedClass"
                self.fields[field] = None if referenced is None else self.descriptor(referenced)
                if self.watch:
                    self.changed = self.snapshot()
                return {"executed": True, "result": None}
        if name == "g1r_watch_start":
            self.watch = True
            self.baseline = self.snapshot()
            return {"watch_id": "watch-1"}
        if name == "g1r_watch_read":
            return {"dropped_events": 0, "gap": False, "last_seq": 2,
                    "events": [{"kind": "baseline", "snapshot": self.baseline}, {"kind": "changed", "snapshot": self.changed}]}
        if name == "g1r_compare_snapshots":
            return {"value_changes": 2}
        if name == "g1r_watch_stop":
            self.watch = False
            return {"cleanup_complete": True, "state": "stopped"}
        if name == "g1r_watch_forget":
            return {"forgotten": True, "cleanup_complete": True}
        raise AssertionError((name, arguments))


class ClassProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        (self.repository / "artifacts").mkdir()
        self.workspace = self.repository / "artifacts" / "class-probe"

    def prepare(self):
        return prepare.prepare_probe(self.workspace, repository_root=self.repository)

    def test_preparation_hashes_exact_source_and_unverified_runtime(self):
        result = self.prepare()
        package = self.workspace / prepare.PACKAGE_NAME
        self.assertEqual((package / prepare.SCRIPT_RELATIVE).read_bytes(), (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes())
        for relative, details in result["files"].items():
            raw = (package / relative).read_bytes()
            self.assertEqual(details["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertFalse(result["compiled"])
        self.assertFalse(result["game_launched"])
        self.assertFalse(result["dll_loaded"])
        self.assertEqual(result["expected"]["verification"], "unverified_until_runtime_reflection_and_calls")
        self.assertEqual(list(self.workspace.iterdir()), [package])

    def test_existing_and_non_artifacts_destinations_are_preserved(self):
        self.prepare()
        source = self.workspace / prepare.PACKAGE_NAME / prepare.SCRIPT_RELATIVE
        source.write_bytes(b"isolated user edit")
        with self.assertRaises(prepare.PackageError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "already_exists")
        self.assertEqual(source.read_bytes(), b"isolated user edit")
        with self.assertRaises(prepare.PackageError) as captured:
            prepare.prepare_probe(self.repository / "outside", repository_root=self.repository)
        self.assertEqual(captured.exception.code, "unsafe_path")
        self.assertFalse((self.repository / "outside").exists())

    def test_write_failure_cleans_created_package(self):
        with patch.object(prepare.PackageWorkspace, "write_package_file", side_effect=prepare.WorkspaceError("isolated_failure", "write failure")):
            with self.assertRaises(prepare.WorkspaceError):
                self.prepare()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_cleanup_rejects_external_parent_replacement(self):
        outside = self.repository / "external"
        outside.mkdir()
        sentinel = outside / "G1RMCPClassProbe.as"
        sentinel.write_bytes(b"external sentinel")
        changes = []

        def replace_parent(writer, package_name, file_path, content, *args, **kwargs):
            parent = writer.root / package_name / "Script"
            moved = parent.with_name("Script-original")
            parent.rename(moved)
            try:
                parent.symlink_to(outside, target_is_directory=True)
            except OSError:
                moved.rename(parent)
                self.skipTest("Symlink не разрешён")
            changes.append((parent, moved))
            raise prepare.WorkspaceError("isolated_failure", "parent replacement")

        with patch.object(prepare.PackageWorkspace, "write_package_file", new=replace_parent):
            with self.assertRaises(prepare.WorkspaceError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertTrue(captured.exception.__notes__)
        self.assertEqual(sentinel.read_bytes(), b"external sentinel")
        parent, moved = changes[0]
        parent.unlink()
        moved.rename(parent)

    def test_live_contract_rejects_wrong_class_target_and_signature(self):
        base = target(live.CLASS_NAMES[0], 2)
        self.assertEqual(live.check_target(base, live.CLASS_NAMES[0]), base)
        host = target(live.HOST, 1, host=True)
        live.check_target(host, live.HOST, host=True)
        with self.assertRaises(live.ProbeError):
            live.check_target({**base, "class": live.HOST}, live.CLASS_NAMES[0])
        with self.assertRaises(live.ProbeError):
            live.check_target({**host, "path": "/Game/UserObject"}, live.HOST, host=True)
        live.check_result({"executed": True, "result": {**base, "session_id": "fixture-owner"}}, base, "fixture-owner")
        with self.assertRaises(live.ProbeError):
            live.check_result({"executed": True, "result": {**base, "session_id": "other-owner"}}, base, "fixture-owner")
        for name in live.SIGNATURES:
            live.check_signature({"items": [signature(name)]}, name)
        actual_as = signature("EchoTypedClass")
        self.assertEqual(actual_as["parameter_size"], 8)
        self.assertEqual(actual_as["parameters"][1]["offset"], 8)
        invalid = signature("EchoTypedClass")
        invalid["parameters"][0]["type"] = "ObjectProperty"
        with self.assertRaises(live.ProbeError):
            live.check_signature({"items": [invalid]}, "EchoTypedClass")

    def test_live_exercise_restores_null_and_stops_observation(self):
        fixture = ClassCallFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(fixture.fields, {name: None for name in live.PROPERTIES})
        self.assertFalse(fixture.watch)
        self.assertEqual(report["foreign_class"]["code"], "incompatible_class_argument")
        self.assertEqual(report["ordinary_object"]["code"], "invalid_class_argument")
        self.assertEqual(report["cleanup"], ["StoreAnyClass", "StoreTypedClass", "restored_null_snapshot", "g1r_watch_stop", "g1r_watch_forget"])

    def test_live_exercise_restores_and_stops_after_failure(self):
        fixture = ClassCallFixture(fail_store=True)
        with self.assertRaises(live.ProbeError) as captured:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertEqual(fixture.fields, {name: None for name in live.PROPERTIES})
        self.assertFalse(fixture.watch)
        self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])

    def test_live_missing_initial_class_defaults_rejects_before_any_writes(self):
        fixture = ClassCallFixture()
        fixture.fields = {name: None for name in live.PROPERTIES}
        with self.assertRaises(live.ProbeError) as captured:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(captured.exception.code, "probe_class_identity")
        self.assertFalse(any(name == "g1r_invoke" and arguments["function"].startswith("Store") for name, arguments in fixture.calls))

    def test_class_cleanup_requires_explicit_null_and_terminal_flags(self):
        for mode in ("snapshot", "stop", "forget"):
            fixture = ClassCallFixture()
            restoring = False

            async def call(name, arguments, **keywords):
                nonlocal restoring
                result = await fixture.call(name, arguments, **keywords)
                if name == "g1r_compare_snapshots":
                    restoring = True
                if restoring:
                    if mode == "snapshot" and name == "g1r_snapshot":
                        result["objects"][0]["properties"][0].pop("value")
                    if mode == "stop" and name == "g1r_watch_stop":
                        return {"state": "running", "cleanup_complete": True}
                    if mode == "forget" and name == "g1r_watch_forget":
                        return {"cleanup_complete": True}
                return result

            with self.subTest(mode=mode), self.assertRaises(live.ProbeError) as captured:
                asyncio.run(live.exercise(call))
            self.assertEqual(captured.exception.code, "probe_cleanup_failed")
            self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])


if __name__ == "__main__":
    unittest.main()
