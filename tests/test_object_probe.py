"""Изолированная подготовка объектной fixture без запуска или загрузки игры."""
from __future__ import annotations

import hashlib
import importlib.util
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

TOOL = Path(__file__).resolve().parents[1] / "tools" / "prepare_object_probe.py"
SPEC = importlib.util.spec_from_file_location("prepare_object_probe", TOOL)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)
LIVE_SPEC = importlib.util.spec_from_file_location("probe_object_mcp", TOOL.with_name("probe_object_mcp.py"))
live = importlib.util.module_from_spec(LIVE_SPEC)
LIVE_SPEC.loader.exec_module(live)


def target(class_name, index):
    return {"handle": f"obj-{index}", "index": index, "name": f"Default__{class_name}",
            "class": class_name, "path": f"/Script/Angelscript.Default__{class_name}"}


def signature(method_name):
    inputs, output = live.SIGNATURES[method_name]
    parameters = []
    offset = 0
    for name, field_type, size in inputs:
        parameters.append({"name": name, "type": field_type, "element_size": size,
                           "offset": offset, "return": False, "out": False, "reference": False})
        offset += size
    if output is not None:
        field_type, size = output
        parameters.append({"name": "ReturnValue", "type": field_type, "element_size": size,
                           "offset": offset, "return": True, "out": True, "reference": False})
        offset += size
    return {"name": method_name, "parameter_size": offset, "parameters": parameters}


class ObjectProbeCallFixture:
    def __init__(self, *, fail_echo=False):
        self.targets = {name: target(name, index) for index, name in enumerate(live.PROBE_CLASSES, start=1)}
        self.fields = {name: None for name in live.PROPERTIES}
        self.calls = []
        self.fail_echo = fail_echo
        self.watch = False
        self.baseline = None
        self.changed = None

    def descriptor(self, item):
        return {**item, "session_id": "fixture-owner"}

    def snapshot(self):
        base = self.targets["G1RMCPObjectProbe"]
        return {"session_id": "fixture-owner", "source": "live_native", "consistency": "single_tick_sequential",
                "partial": False, "truncated": False, "objects": [{**base,
            "properties": [{"name": name, "type": "ObjectProperty", "supported": True, "value": value}
                           for name, value in self.fields.items()]}]}

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, arguments))
        if name == "g1r_native_status":
            return {"ready": True, "session_id": "fixture-owner"}
        if name == "g1r_find_objects":
            return {"items": [self.targets[arguments["class_query"]]]}
        if name == "g1r_object_functions":
            return {"items": [signature(method) for method in live.SIGNATURES]}
        if name == "g1r_snapshot":
            return self.snapshot()
        if name == "g1r_invoke":
            method, args = arguments["function"], arguments["arguments"]
            called_target = next(item for item in self.targets.values() if item["handle"] == arguments["handle"])
            if method == "ReadMarker":
                return {"executed": True, "result": live.PROBE_CLASSES[called_target["class"]]}
            if method == "ReadSelf":
                return {"executed": True, "result": self.descriptor(called_target)}
            value = args["Input"]
            referenced = None if value is None else next(item for item in self.targets.values() if item["handle"] == value["handle"])
            if method == "EchoObject" and self.fail_echo:
                raise live.ProbeError("isolated_failure", "Изолированный отказ после подготовки собственных полей")
            if method in {"EchoObject", "EchoTypedObject"}:
                if method == "EchoTypedObject" and referenced is not None and referenced["class"] == "G1RMCPOtherObjectProbe":
                    if expected_error != "incompatible_object_argument":
                        raise AssertionError("Отсутствует ожидаемый класс отказа")
                    return {"code": expected_error, "may_have_executed": False}
                return {"executed": True, "result": None if referenced is None else self.descriptor(referenced)}
            if method == "IsNullObject":
                return {"executed": True, "result": referenced is None}
            if method in {"StoreObject", "StoreTypedObject"}:
                field = "StoredObject" if method == "StoreObject" else "StoredTypedObject"
                self.fields[field] = None if referenced is None else self.descriptor(referenced)
                if self.watch:
                    self.changed = self.snapshot()
                return {"executed": True, "result": None}
        if name == "g1r_watch_start":
            self.watch = True
            self.baseline = self.snapshot()
            return {"watch_id": "watch-1"}
        if name == "g1r_watch_read":
            return {"dropped_events": 0, "gap": False, "last_seq": 2, "events": [
                {"kind": "baseline", "snapshot": self.baseline}, {"kind": "changed", "snapshot": self.changed}]}
        if name == "g1r_compare_snapshots":
            return {"value_changes": 2}
        if name == "g1r_watch_stop":
            self.watch = False
            return {"cleanup_complete": True, "state": "stopped"}
        if name == "g1r_watch_forget":
            return {"forgotten": True, "cleanup_complete": True}
        raise AssertionError((name, arguments))


class ObjectProbeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        (self.repository / "artifacts").mkdir()
        self.workspace = self.repository / "artifacts" / "object-probe"

    def prepare(self):
        return probe.prepare_probe(self.workspace, repository_root=self.repository)

    def test_prepared_package_has_verified_hashes_and_unverified_runtime(self):
        result = self.prepare()
        package = self.workspace / probe.PACKAGE_NAME
        source = package / probe.SCRIPT_RELATIVE
        self.assertEqual(source.read_bytes(), (probe.FIXTURE_ROOT / probe.SCRIPT_RELATIVE).read_bytes())
        for relative_path, details in result["files"].items():
            raw = (package / relative_path).read_bytes()
            self.assertEqual(details["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(details["bytes"], len(raw))
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["script_count"], 1)
        self.assertFalse(result["compiled"])
        self.assertFalse(result["game_launched"])
        self.assertFalse(result["dll_loaded"])
        self.assertEqual(result["runtime_validation"], "not_performed")
        self.assertEqual(result["expected"]["verification"], "unverified_until_runtime_reflection_and_calls")
        self.assertEqual(result["startup_parameters"], {"ModsDirectory": str(self.workspace), "Mods": [probe.PACKAGE_NAME]})
        self.assertEqual({path.relative_to(package).as_posix() for path in package.rglob("*") if path.is_file()},
                         {"GothicMod.json", probe.SCRIPT_RELATIVE})
        self.assertEqual(list(self.workspace.iterdir()), [package])

    def test_existing_package_is_not_overwritten(self):
        self.prepare()
        source = self.workspace / probe.PACKAGE_NAME / probe.SCRIPT_RELATIVE
        source.write_text("// Изолированная пользовательская правка\n", encoding="utf-8")
        before = source.read_bytes()
        with self.assertRaises(probe.PackageError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "already_exists")
        self.assertEqual(source.read_bytes(), before)
        self.assertEqual(list(self.workspace.iterdir()), [self.workspace / probe.PACKAGE_NAME])

    def test_non_artifacts_and_artifacts_root_are_rejected(self):
        for destination in (self.repository / "mods", self.repository / "artifacts", self.repository / "artifacts" / ".." / "outside"):
            with self.subTest(destination=destination), self.assertRaises(probe.PackageError) as captured:
                probe.prepare_probe(destination, repository_root=self.repository)
            self.assertEqual(captured.exception.code, "unsafe_path")
        self.assertFalse((self.repository / "mods").exists())
        self.assertFalse((self.repository / "outside").exists())

    def test_symlink_workspace_is_rejected_without_target_changes(self):
        target = self.repository / "isolated-target"
        target.mkdir()
        try:
            self.workspace.symlink_to(target, target_is_directory=True)
        except OSError:
            self.skipTest("Система не разрешает создание symlink")
        with self.assertRaises(probe.WorkspaceError) as captured:
            self.prepare()
        self.assertEqual(captured.exception.code, "unsafe_path")
        self.assertEqual(list(target.iterdir()), [])

    def test_staged_write_failure_leaves_no_package_or_draft(self):
        def fail_write(workspace, *arguments, **keywords):
            raise probe.WorkspaceError("access_error", "Изолированный сбой записи")

        with patch.object(probe.PackageWorkspace, "write_package_file", side_effect=fail_write):
            with self.assertRaises(probe.WorkspaceError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "access_error")
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_live_probe_guards_exact_own_cdo_marker_and_argument_layout(self):
        base = target("G1RMCPObjectProbe", 1)
        self.assertEqual(live.check_target(base, "G1RMCPObjectProbe"), base)
        for key, value in (("path", "/Game/User.Other"), ("class", "OtherClass"), ("name", "UserObject")):
            with self.subTest(key=key), self.assertRaises(live.ProbeError):
                live.check_target({**base, key: value}, "G1RMCPObjectProbe")
        live.check_marker({"executed": True, "result": 41}, "G1RMCPObjectProbe")
        for marker in (True, 99, 41.0):
            with self.subTest(marker=marker), self.assertRaises(live.ProbeError):
                live.check_marker({"executed": True, "result": marker}, "G1RMCPObjectProbe")
        for method in live.SIGNATURES:
            live.check_signature({"items": [signature(method)]}, method)
        actual_as = signature("EchoObject")
        actual_as["parameter_size"] = 8
        self.assertEqual(live.check_signature({"items": [actual_as]}, "EchoObject"), actual_as)
        invalid_span = signature("EchoObject")
        invalid_span["parameters"][1]["offset"] = 32
        with self.assertRaises(live.ProbeError):
            live.check_signature({"items": [invalid_span]}, "EchoObject")
        invalid = signature("EchoObject")
        invalid["parameters"][0]["name"] = "UnexpectedInput"
        with self.assertRaises(live.ProbeError):
            live.check_signature({"items": [invalid]}, "EchoObject")
        invalid = signature("EchoTypedObject")
        invalid["parameters"][0]["type"] = "ClassProperty"
        with self.assertRaises(live.ProbeError):
            live.check_signature({"items": [invalid]}, "EchoTypedObject")
        error = SimpleNamespace(content=[SimpleNamespace(type="text", text='Error calling tool: {"code":"stale_handle","may_have_executed":false}')])
        self.assertEqual(live.error_payload(error), {"code": "stale_handle", "may_have_executed": False})
        specific = live.ProbeError("probe_signature_mismatch", "Исходная причина")
        grouped = ExceptionGroup("outer", [ExceptionGroup("inner", [specific])])
        self.assertIs(live._single_error(grouped), specific)

    def test_live_probe_completes_and_restores_fields_and_observation(self):
        fixture = ObjectProbeCallFixture()
        report = asyncio.run(live.exercise(fixture.call))
        self.assertEqual(fixture.fields, {"StoredObject": None, "StoredTypedObject": None})
        self.assertFalse(fixture.watch)
        self.assertEqual(report["class_mismatch"]["code"], "incompatible_object_argument")
        self.assertEqual(report["cleanup"], ["StoreObject", "StoreTypedObject", "restored_null_snapshot", "g1r_watch_stop", "g1r_watch_forget"])
        self.assertIn("object_properties_snapshots_watch_change_and_stability", report["checks"])
        mismatch = [arguments for name, arguments in fixture.calls if name == "g1r_invoke"
                    and arguments["function"] == "EchoTypedObject"
                    and arguments["arguments"].get("Input") == {"handle": "obj-3"}]
        self.assertEqual(len(mismatch), 1)

    def test_live_probe_restores_own_fields_after_failure_without_repeating_call(self):
        fixture = ObjectProbeCallFixture(fail_echo=True)
        with self.assertRaises(live.ProbeError) as captured:
            asyncio.run(live.exercise(fixture.call))
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertEqual(fixture.fields, {"StoredObject": None, "StoredTypedObject": None})
        echo_calls = [arguments for name, arguments in fixture.calls if name == "g1r_invoke" and arguments["function"] == "EchoObject"]
        self.assertEqual(len(echo_calls), 1)
        cleanup_calls = fixture.calls[-3:-1]
        self.assertEqual([arguments["function"] for name, arguments in cleanup_calls], ["StoreObject", "StoreTypedObject"])

    def test_cleanup_flags_and_missing_null_value_do_not_claim_success(self):
        for mode in ("setter", "stop", "forget", "snapshot"):
            fixture = ObjectProbeCallFixture()
            stopping = False

            async def call(name, arguments, **keywords):
                nonlocal stopping
                result = await fixture.call(name, arguments, **keywords)
                if name == "g1r_compare_snapshots":
                    stopping = True
                if stopping:
                    if mode == "setter" and name == "g1r_invoke" and arguments["function"] == "StoreObject":
                        return {"executed": False}
                    if mode == "stop" and name == "g1r_watch_stop":
                        return {"cleanup_complete": True, "state": "running"}
                    if mode == "forget" and name == "g1r_watch_forget":
                        return {"cleanup_complete": True, "forgotten": False}
                    if mode == "snapshot" and name == "g1r_snapshot":
                        result["objects"][0]["properties"][0].pop("value")
                return result

            with self.subTest(mode=mode), self.assertRaises(live.ProbeError) as captured:
                asyncio.run(live.exercise(call))
            self.assertEqual(captured.exception.code, "probe_cleanup_failed")
            self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])

    def test_repeated_cancellation_during_cleanup_keeps_all_steps(self):
        async def scenario():
            fixture = ObjectProbeCallFixture()
            started, release = asyncio.Event(), asyncio.Event()
            stopping = False

            async def call(name, arguments, **keywords):
                nonlocal stopping
                result = await fixture.call(name, arguments, **keywords)
                if name == "g1r_compare_snapshots":
                    stopping = True
                elif stopping and name == "g1r_invoke" and arguments["function"] == "StoreObject":
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
            self.assertFalse(fixture.watch)
            self.assertEqual(fixture.fields, {name: None for name in live.PROPERTIES})
            self.assertEqual([name for name, _ in fixture.calls[-2:]], ["g1r_watch_stop", "g1r_watch_forget"])

        asyncio.run(scenario())

    def test_atomic_destination_failure_preserves_original_destination(self):
        original = Path.rename

        def fail_rename(path, destination):
            if path.name == probe.PACKAGE_NAME and ".g1r-object-probe-" in str(path):
                raise OSError("Изолированный сбой атомарного завершения")
            return original(path, destination)

        with patch.object(Path, "rename", fail_rename):
            with self.assertRaises(OSError):
                self.prepare()
        self.assertEqual(list(self.workspace.iterdir()), [])

    def test_cleanup_rejects_replaced_parent_without_touching_external_files(self):
        outside = self.repository / "external-target"
        outside.mkdir()
        sentinel = outside / "G1RMCPObjectProbe.as"
        sentinel.write_text("external sentinel", encoding="utf-8")
        moved = []
        links = []

        def replace_script(workspace, package_name, file_path, content, *args, **kwargs):
            script = workspace.root / package_name / "Script"
            original = script.with_name("Script-original")
            script.rename(original)
            moved.append(original)
            try:
                script.symlink_to(outside, target_is_directory=True)
            except OSError:
                original.rename(script)
                self.skipTest("Система не разрешает создание symlink")
            links.append(script)
            raise probe.WorkspaceError("isolated_failure", "Сбой после подмены родителя")

        with patch.object(probe.PackageWorkspace, "write_package_file", new=replace_script):
            with self.assertRaises(probe.WorkspaceError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertTrue(any("Unsafe cleanup was skipped; temporary files were preserved" in note
                            for note in captured.exception.__notes__))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "external sentinel")
        self.assertFalse((self.workspace / probe.PACKAGE_NAME).exists())
        self.assertEqual(len(links), 1)
        self.assertTrue(links[0].is_symlink())
        # Restore the isolated substitution so TemporaryDirectory can clean its own data.
        links[0].unlink()
        moved[0].rename(links[0])

    def test_cleanup_rejects_replaced_file_identity_without_deleting_replacement(self):
        replacement_bytes = b"external descriptor replacement"
        displaced = []

        def replace_descriptor(workspace, package_name, file_path, content, *args, **kwargs):
            descriptor = workspace.root / package_name / "GothicMod.json"
            old = self.repository / "isolated-displaced-descriptor"
            descriptor.rename(old)
            displaced.append(old)
            descriptor.write_bytes(replacement_bytes)
            raise probe.WorkspaceError("isolated_failure", "Сбой после подмены файла")

        with patch.object(probe.PackageWorkspace, "write_package_file", new=replace_descriptor):
            with self.assertRaises(probe.WorkspaceError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertTrue(captured.exception.__notes__)
        self.assertEqual(next(self.workspace.glob(f".g1r-object-probe-*/{probe.PACKAGE_NAME}/GothicMod.json")).read_bytes(), replacement_bytes)

    def test_cleanup_preserves_modified_owned_file_with_same_identity(self):
        replacement_bytes = b"changed descriptor with same identity"

        def modify_descriptor(workspace, package_name, file_path, content, *args, **kwargs):
            (workspace.root / package_name / "GothicMod.json").write_bytes(replacement_bytes)
            raise probe.WorkspaceError("isolated_failure", "Сбой после изменения файла")

        with patch.object(probe.PackageWorkspace, "write_package_file", new=modify_descriptor):
            with self.assertRaises(probe.WorkspaceError) as captured:
                self.prepare()
        self.assertEqual(captured.exception.code, "isolated_failure")
        self.assertTrue(captured.exception.__notes__)
        self.assertEqual(next(self.workspace.glob(f".g1r-object-probe-*/{probe.PACKAGE_NAME}/GothicMod.json")).read_bytes(), replacement_bytes)


if __name__ == "__main__":
    unittest.main()
