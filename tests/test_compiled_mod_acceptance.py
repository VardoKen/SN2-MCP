"""Check acceptance-runner cleanup for uncertain launch and existing sessions."""

import argparse
import base64
from contextlib import asynccontextmanager, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("accept_compiled_mod", Path(__file__).parents[1] / "tools/accept_compiled_mod.py")
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)
sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))
CONTINUE_SPEC = importlib.util.spec_from_file_location("continue_compiled_mod_acceptance", Path(__file__).parents[1] / "tools/continue_compiled_mod_acceptance.py")
CONTINUE = importlib.util.module_from_spec(CONTINUE_SPEC)
CONTINUE_SPEC.loader.exec_module(CONTINUE)


class Response:
    def __init__(self, value, *, error=False):
        self.is_error = error
        self.structured_content = None if error else value
        self.content = [types.SimpleNamespace(type="text", text=json.dumps(value))]

    def model_dump(self, mode):
        return {"is_error": self.is_error, "structured_content": self.structured_content,
                "content": [{"type": "text", "text": self.content[0].text}]}


@asynccontextmanager
async def transport(_parameters):
    yield None, None


class LaunchFixture:
    """Neutral process evidence; no OS process or game is ever created."""

    def __init__(self, runtime, *, existing=False, stop_error=False, bootstrap_only=False, native_error=False):
        self.runtime = runtime
        self.existing = existing
        self.stop_error = stop_error
        self.native_error = native_error
        self.stopped = False
        self.calls = []
        self.process_snapshots = 0
        self.process = {"pid": 7123, "executable": str(runtime / ("Subnautica2.exe" if bootstrap_only else "Subnautica2-Win64-Shipping.exe")),
                        "creation_time": "134077200000000001"}
        self.package = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def initialize(self):
        return None

    async def call_tool(self, name, arguments, **kwargs):
        self.calls.append((name, arguments))
        if name in ("sn2_status", "sn2_capabilities"):
            return Response({})
        if name == "sn2_target_processes":
            self.process_snapshots += 1
            running = (self.existing or self.process_snapshots == 2) and not self.stopped
            return Response({"processes": [self.process] if running else []})
        if name == "sn2_build_status":
            return Response({"state": "succeeded", "exact_source_compiled": True,
                "artifacts": [{"path": str(self.runtime / "SN2_MCP.dll"), "sha256": "a" * 64}]})
        if name in ("sn2_create_mod_project", "sn2_validate_mod_project"):
            return Response({"valid": True})
        if name == "sn2_read_source_exact":
            return Response({"sha256": "b" * 64, "text": "constexpr float Opacity = 0.5f;\n"})
        if name == "sn2_build_recipe":
            return Response({"input_manifest_sha256": "c" * 64})
        if name == "sn2_build":
            return Response({"state": "succeeded", "exact_source_compiled": True,
                             "build_id": "d" * 32, "receipt_path": str(self.runtime / "receipt.json")})
        if name == "sn2_package_artifact":
            workspace = next((self.runtime / "artifacts/acceptance/test").glob("workspace"))
            root = workspace / "artifacts/revision-a"
            (root / "bin").mkdir(parents=True)
            (root / "bin/compiled_opacity.dll").write_bytes(b"neutral compiled artifact fixture")
            self.package = {"compiled": True, "valid": True, "package_root": str(root),
                "manifest_sha256": "e" * 64, "source_revision": "c" * 64,
                "artifact": {"package_path": "bin/compiled_opacity.dll", "sha256": "f" * 64}}
            return Response(self.package)
        if name == "sn2_verify_artifact_package":
            if "corrupted" in arguments["package_path"]:
                return Response({"code": "package_changed", "message": "Neutral corrupted artifact fixture"}, error=True)
            return Response(self.package)
        if name == "sn2_development_start":
            return Response({"code": "launch_outcome_unknown", "outcome_unknown": True,
                             "receipt": {"process": self.process}}, error=True)
        if name == "sn2_development_stop":
            self.stopped = True
            if self.stop_error:
                return Response({"code": "tool_unavailable", "outcome_unknown": True,
                    "receipt": {"process": arguments["target_process"]}}, error=True)
            return Response({"state": "stopped", "process": arguments["target_process"],
                             "remaining_processes": [], "outcome_unknown": False})
        if name == "sn2_native_load" and self.native_error:
            return Response({"code": "native_load_failed", "outcome_unknown": False}, error=True)
        raise AssertionError("Unexpected fixture call: " + name)


class CompiledAcceptanceCleanupTests(unittest.IsolatedAsyncioTestCase):
    def options(self, runtime, game):
        return argparse.Namespace(runtime_root=runtime, game_root=game,
            output=runtime / "artifacts/acceptance/test", bridge_build_id="1" * 32,
            build_timeout_seconds=900, readiness_timeout_seconds=180,
            control_timeout_seconds=30, parallel=4)

    async def test_unknown_launch_is_not_replayed_and_actual_session_is_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime)
            args = self.options(runtime, game)
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(args), 1)
            names = [name for name, _ in fixture.calls]
            self.assertEqual(names.count("sn2_development_start"), 1)
            self.assertEqual(names.count("sn2_development_stop"), 1)
            self.assertNotIn("sn2_native_load", names)
            stopped = next(value for name, value in fixture.calls if name == "sn2_development_stop")
            self.assertEqual(stopped["target_process"], fixture.process)
            summary = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["remaining_processes_confirmed"])
            self.assertTrue(summary["connection_closed"])
            self.assertEqual(summary["state"], "failed")
            self.assertTrue(summary["failure"]["tool_error"]["outcome_unknown"])

    async def test_preexisting_session_is_not_launched_or_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime, existing=True)
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(self.options(runtime, game)), 1)
            names = [name for name, _ in fixture.calls]
            self.assertNotIn("sn2_development_start", names)
            self.assertNotIn("sn2_development_stop", names)
            self.assertEqual(fixture.process_snapshots, 1)

    async def test_failed_stop_receipt_retains_error_and_final_absence_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime, stop_error=True)
            args = self.options(runtime, game)
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(args), 1)
            names = [name for name, _ in fixture.calls]
            self.assertEqual(names.count("sn2_development_stop"), 1)
            self.assertEqual(fixture.process_snapshots, 3)
            self.assertEqual(names[-1], "sn2_target_processes")
            summary = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["remaining_processes_confirmed"])
            self.assertEqual(summary["final_processes"]["processes"], [])
            failure = next(item for item in summary["cleanup"] if "game_stop_unconfirmed" in item)
            self.assertEqual(failure["tool_error"]["code"], "tool_unavailable")
            self.assertTrue(failure["tool_error"]["outcome_unknown"])
            self.assertEqual(summary["state"], "failed")

    async def test_failed_launch_bootstrap_only_is_closed_by_exact_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime, bootstrap_only=True)
            args = self.options(runtime, game)
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(args), 1)
            stopped = [arguments for name, arguments in fixture.calls if name == "sn2_development_stop"]
            self.assertEqual(stopped, [{"target_process": fixture.process}])
            summary = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["remaining_processes_confirmed"])
            self.assertTrue(summary["connection_closed"])

    async def test_explicit_existing_identity_is_reused_without_launch_then_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime, existing=True, native_error=True)
            args = self.options(runtime, game)
            args.expected_pid = fixture.process["pid"]
            args.expected_creation_filetime = fixture.process["creation_time"]
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(args), 1)
            names = [name for name, _ in fixture.calls]
            self.assertNotIn("sn2_development_start", names)
            loaded = next(arguments for name, arguments in fixture.calls if name == "sn2_native_load")
            self.assertEqual(loaded["target_process"], fixture.process)
            summary = json.loads((args.output / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(summary["game_already_running"])
            self.assertEqual(summary["selected_existing_process"], fixture.process)
            self.assertTrue(summary["remaining_processes_confirmed"])

    async def test_mismatched_existing_identity_is_not_used_or_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime"
            game = Path(directory) / "game"
            runtime.mkdir()
            game.mkdir()
            fixture = LaunchFixture(runtime, existing=True)
            args = self.options(runtime, game)
            args.expected_pid = fixture.process["pid"]
            args.expected_creation_filetime = "134077200000000002"
            with patch.object(RUNNER, "stdio_client", transport), \
                 patch.object(RUNNER, "ClientSession", return_value=fixture), redirect_stdout(io.StringIO()):
                self.assertEqual(await RUNNER.run(args), 1)
            names = [name for name, _ in fixture.calls]
            self.assertNotIn("sn2_development_start", names)
            self.assertNotIn("sn2_development_stop", names)
            self.assertNotIn("sn2_native_load", names)


class ContinuedAcceptanceBoundaryTests(unittest.TestCase):
    def prior(self, stderr: bytes):
        return {"kind": "compiled_widget_opacity_mod_acceptance", "state": "failed", "failure": {
            "type": "ToolFailure", "message": "sn2_mod_reload failed; inspect MCP receipt 0046",
            "tool_error": {"code": "tool_output_decode_failed", "receipt": {
                "helper_completed": True, "output_complete": True,
                "stderr_base64": base64.b64encode(stderr).decode("ascii")}}}}

    def test_only_complete_confirm_preentry_failure_is_accepted(self):
        summary = self.prior(b"stop_sn2_development.ps1 Confirm CannotConvertArgument System.Management.Automation.SwitchParameter")
        CONTINUE.validate_confirm_failure(summary)
        summary["failure"]["tool_error"]["receipt"]["helper_completed"] = False
        with self.assertRaises(CONTINUE.AcceptanceError):
            CONTINUE.validate_confirm_failure(summary)

    def test_other_unknown_runtime_failure_cannot_authorize_reactivation(self):
        summary = self.prior(b"sn2_mod_load timed out, START may have executed")
        with self.assertRaises(CONTINUE.AcceptanceError):
            CONTINUE.validate_confirm_failure(summary)

    def test_prior_receipt_inventory_detects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            file = root / "0001.json"
            file.write_text('{"neutral":1}', encoding="utf-8")
            before = CONTINUE.receipt_inventory(root)
            file.write_text('{"neutral":2}', encoding="utf-8")
            self.assertNotEqual(CONTINUE.receipt_inventory(root), before)

    def test_established_terminal_mod_state_preserves_complete_receipt(self):
        for state in ("faulted", "worker_failed", "effect_unknown", "disabled_unrestored", "restore_outcome_unknown"):
            with self.subTest(state=state):
                receipt = {"mod_receipt": {"status": {"state": state, "source_revision": "a" * 64,
                    "last_error": {"message": "Neutral established fault"}},
                    "command": {"state": "completed", "request_id": 12}}, "dll_sha256": "b" * 64}
                with self.assertRaises(RUNNER.ModStateFailure) as failure:
                    RUNNER.require_mod_progress(receipt)
                self.assertEqual(failure.exception.receipt, receipt)

    def test_mod_initialization_and_active_states_can_continue(self):
        for state in ("initializing", "waiting_runtime", "waiting_target", "active", "disabled"):
            RUNNER.require_mod_progress({"mod_receipt": {"status": {"state": state}}})


if __name__ == "__main__":
    unittest.main()
