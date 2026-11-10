"""Staged hotreload: CAS, immutable sources, leases and one restore без игры."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest

from tests.test_development_helpers_probe import HelpersFixture

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("staged_hotreload_probe_test", TOOLS / "probe_staged_hotreload.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class HotReloadFixture(HelpersFixture):
    def __init__(self, *, no_reload=False, mutate_source=False, external_edit=False, change_owner=False, cleanup_unknown=False):
        super().__init__()
        self.source = "// precise\r\nFString GetHelperVersion()\r\n{\r\n    return \"1.0.0-dev.26\";\r\n}\r\n"
        self.staged, self.runtime = self.source, "1.0.0-dev.26"
        self.package = "artifacts/development-sessions/session123/packages/" + probe.HOST
        self.process = {"pid": 25, "creation_time": "2026-10-31T00:00:00.0000000Z", "executable": "E:/Game/G1R/Binaries/Win64/G1R-Win64-Shipping.exe"}
        self.writes, self.now = [], 0.0
        self.no_reload, self.mutate_source, self.external_edit, self.change_owner, self.cleanup_unknown = no_reload, mutate_source, external_edit, change_owner, cleanup_unknown
        self.version_reads = 0

    async def call(self, name, arguments, *, expected_error=None):
        self.calls.append((name, copy.deepcopy(arguments), expected_error))
        if name == "g1r_development_processes":
            return {"processes": [self.process], "launch_observation": {"launch_id": "new" if self.change_owner and self.writes else "launch25",
                "stage": "process_running", "outcome_unknown": False, "process": self.process, "development_staging": {
                    "relative_directory": "artifacts/development-sessions/session123", "edit_target": "staged_copy_only", "packages": [{
                        "role": "companion", "package_name": probe.HOST, "source_package_path": probe.SOURCE_PACKAGE, "relative_package": self.package}]}}}
        if name == "g1r_native_status": return {"ready": True, "session_id": "native25", "process_id": 25}
        if name == "g1r_find_objects": return {"items": [self.helper_cdo, self.helper], "truncated": False}
        if name == "g1r_read_package_file_exact":
            source = arguments["relative_path"] == probe.SOURCE_PACKAGE
            text = self.source if source else self.staged
            return {"package_path": arguments["relative_path"], "file_path": probe.FILE, "text": text,
                "text_mode": "exact", "truncated": False, "bytes": len(text.encode()), "sha256": hashlib.sha256(text.encode()).hexdigest()}
        if name == "g1r_write_package_file":
            if arguments["relative_path"] != self.package: raise AssertionError("source edit forbidden")
            if arguments["expected_sha256"] != hashlib.sha256(self.staged.encode()).hexdigest(): raise probe.ProbeError("write_conflict", "CAS conflict")
            self.staged = arguments["content"]
            self.writes.append(copy.deepcopy(arguments))
            if not self.no_reload: self.runtime = probe.VERSION_BODY.search(self.staged).group(2)
            if self.mutate_source: self.source += "// foreignchange\n"
            return {"written": True, "sha256": hashlib.sha256(self.staged.encode()).hexdigest()}
        if name == "g1r_object_functions":
            result = self.helper_functions(arguments["function_query"])
            result["items"][0]["signature_token"] = "sig-" + str(100 + len(self.writes) + self.version_reads)
            return result
        if name == "g1r_invoke":
            if arguments["function"] != "GetHelperVersion" or arguments["handle"] != self.helper["handle"]: raise AssertionError("unexpected invoke")
            self.version_reads += 1
            if self.external_edit and self.writes: self.staged += "// foreignedit\n"
            return {"executed": True, "return_type": "StrProperty", "result": self.runtime,
                    "cleanup_confirmed": not (self.cleanup_unknown and bool(self.writes))}
        raise AssertionError(name)

    async def sleep(self, delay): self.now += delay


class StagedHotReloadProbeTests(unittest.TestCase):
    def test_marker_observed_same_lease_sources_unchanged_restore_once(self):
        fixture = HotReloadFixture()
        source = fixture.source
        report = asyncio.run(probe.exercise(fixture.call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.staged, source)
        self.assertEqual(fixture.source, source)
        self.assertEqual(report["observations"][1]["result"], report["temporary_marker"])
        self.assertEqual(report["observations"][-1]["result"], "1.0.0-dev.26")
        self.assertEqual(report["source_sha256"], report["source_after_sha256"])
        self.assertTrue(report["restore_attempted"])
        self.assertFalse(any(name in {"g1r_development_start", "g1r_development_stop", "g1r_native_load"} for name, _, _ in fixture.calls))

    def test_no_runtime_reload_restores_once_and_retains_primary(self):
        fixture = HotReloadFixture(no_reload=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call, wait_seconds=.5, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "hotreload_not_observed")
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.staged, fixture.source)

    def test_foreign_staging_edit_is_not_overwritten_by_restore(self):
        fixture = HotReloadFixture(external_edit=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(len(fixture.writes), 1)
        self.assertIn("foreignedit", fixture.staged)
        self.assertEqual(caught.exception.code, "hotreload_staging_changed")
        self.assertIn("Staged hotreload report:", "\n".join(caught.exception.__notes__))
        self.assertIn('"lease"', "\n".join(caught.exception.__notes__))
        self.assertIn('"staging_marker_sha256"', "\n".join(caught.exception.__notes__))

    def test_restore_failure_contains_full_probe_report(self):
        fixture = HotReloadFixture()
        async def call(name, arguments, **kwargs):
            if name == "g1r_write_package_file" and fixture.writes:
                raise probe.ProbeError("restore_failure", "restore unavailable")
            return await fixture.call(name, arguments, **kwargs)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "restore_failure")
        notes = "\n".join(caught.exception.__notes__)
        self.assertIn("Staged hotreload report:", notes)
        self.assertIn('"restore_attempted": true', notes)
        self.assertIn('"staging_runtime_sha256"', notes)

    def test_unconfirmed_write_response_does_not_repeat_marker_write(self):
        fixture = HotReloadFixture()
        async def call(name, arguments, **kwargs):
            result = await fixture.call(name, arguments, **kwargs)
            if name == "g1r_write_package_file" and len(fixture.writes) == 1:
                return {"written": False}
            return result
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "hotreload_write_unconfirmed")
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.staged, fixture.source)

    def test_launch_observation_change_in_same_game_does_not_block_reload(self):
        fixture = HotReloadFixture(change_owner=True)
        asyncio.run(probe.exercise(fixture.call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.staged, fixture.source)

    def test_compiler_templates_are_ignored_and_current_helper_is_refreshed(self):
        fixture = HotReloadFixture()
        async def call(name, arguments, **kwargs):
            if name == "g1r_find_objects" and fixture.writes:
                fixture.helper = {**fixture.helper, "handle": "obj-helper-reloaded"}
                result = await fixture.call(name, arguments, **kwargs)
                result["items"].insert(0, {"handle": "obj-template", "class": probe.HOST,
                    "name": "REINST_" + probe.HOST, "path": "/Script/Angelscript.REINST_" + probe.HOST})
                return result
            return await fixture.call(name, arguments, **kwargs)
        report = asyncio.run(probe.exercise(call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(report["observations"][1]["helper"]["handle"], "obj-helper-reloaded")
        self.assertEqual(fixture.staged, fixture.source)

    def test_missing_helper_after_edit_still_restores_file(self):
        fixture = HotReloadFixture()
        async def call(name, arguments, **kwargs):
            if name == "g1r_find_objects" and fixture.writes:
                return {"items": [fixture.helper_cdo], "truncated": False}
            return await fixture.call(name, arguments, **kwargs)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "hotreload_helper_invalid")
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.staged, fixture.source)

    def test_cleanup_unknown_restores_file_but_does_not_repeat_runtime_call(self):
        fixture = HotReloadFixture(cleanup_unknown=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "hotreload_cleanup_unknown")
        self.assertEqual(len(fixture.writes), 2)
        self.assertEqual(fixture.version_reads, 2)
        self.assertEqual(fixture.staged, fixture.source)

    def test_source_mutation_is_detected_after_staging_restore(self):
        fixture = HotReloadFixture(mutate_source=True)
        with self.assertRaises(probe.ProbeError) as caught:
            asyncio.run(probe.exercise(fixture.call, clock=lambda: fixture.now, sleep=fixture.sleep))
        self.assertEqual(caught.exception.code, "hotreload_source_changed")
        self.assertEqual(len(fixture.writes), 2)

    def test_staging_path_outside_active_session_rejects_before_write(self):
        fixture = HotReloadFixture()
        fixture.package = "development/helpers/G1RMCPDevelopmentHelpers"
        with self.assertRaises(probe.ProbeError): asyncio.run(probe.exercise(fixture.call))
        self.assertEqual(fixture.writes, [])


if __name__ == "__main__": unittest.main()
