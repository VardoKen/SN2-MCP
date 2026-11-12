"""Mod controls retain exact identities and uncertain effects without replay."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sn2_mcp.development import CommandResult, DevelopmentError
from sn2_mcp.mod_runtime import ModRuntime


class ModRuntimeTests(unittest.TestCase):
    def fixture(self, directory, responder):
        root = Path(directory)
        controller = root / "build/native/Release/sn2_mcp_mod_control.exe"
        controller.parent.mkdir(parents=True)
        controller.write_bytes(b"neutral controller")
        dll = root / "NeutralMod.dll"
        dll.write_bytes(b"neutral compiled payload")
        digest = hashlib.sha256(dll.read_bytes()).hexdigest()
        target = {"pid": 123, "creation_time": "134387790721234567", "executable": str(root / "Shipping.exe")}
        service = Mock(repository=root, _closed=False)
        service.validate_target.return_value = target
        calls = []

        def execute(arguments, timeout):
            calls.append((arguments, timeout))
            return responder(arguments, target, digest)

        service.executor = execute
        return ModRuntime(service), dll, digest, target, calls

    def success(self, arguments, target, digest):
        def arg(name):
            return arguments[arguments.index(name) + 1]
        return CommandResult(0, json.dumps({"ok": True, "outcome_unknown": False,
            "module_loaded": True, "process": target, "dll_sha256": digest,
            "request_id": int(arg("--request-id")), "operation": arg("--operation"),
            "mod_receipt": {"status": {"state": "active", "initialized": True}}}))

    def test_exact_artifact_process_and_timeout_cross_fixed_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, self.success)
            result = service.load(target, str(dll), digest.upper(), timeout_seconds=91.25)
            self.assertTrue(result["mod_receipt"]["status"]["initialized"])
            self.assertFalse(result["hot_unload_supported"])
            self.assertEqual(result["residency"], "process_lifetime")
            self.assertEqual(len(calls), 1)
            arguments, timeout = calls[0]
            self.assertEqual(timeout, 93.25)
            self.assertEqual(arguments[arguments.index("--timeout-ms") + 1], "91250")
            self.assertEqual(arguments[arguments.index("--expected-creation-filetime") + 1], target["creation_time"])
            self.assertEqual(arguments[arguments.index("--expected-sha256") + 1], digest)
            self.assertTrue(arguments[0].endswith("sn2_mcp_mod_control.exe"))

    def test_status_queries_prior_request_without_replaying_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, self.success)
            result = service.status(target, str(dll), digest, request_id=18446744073709551615)
            self.assertEqual(result["request_id"], 18446744073709551615)
            self.assertEqual(calls[0][0][calls[0][0].index("--operation") + 1], "status")
            self.assertEqual(len(calls), 1)

    def test_changed_dll_never_calls_controller(self):
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, self.success)
            dll.write_bytes(b"changed revision")
            with self.assertRaises(DevelopmentError) as caught:
                service.load(target, str(dll), digest)
            self.assertEqual(caught.exception.code, "dll_changed")
            self.assertFalse(caught.exception.outcome_unknown)
            self.assertEqual(calls, [])

    def test_known_and_unknown_native_failures_preserve_receipt(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as directory:
                receipt = {"ok": False, "outcome_unknown": unknown, "code": "command_outcome_unknown" if unknown else "mod_request_rejected",
                           "message": "Neutral controller failure", "module_loaded": True,
                           "mod_receipt": {"command": {"request_id": 17, "state": "pending"}}}
                service, dll, digest, target, calls = self.fixture(directory,
                    lambda *_: CommandResult(3 if unknown else 2, json.dumps(receipt)))
                with self.assertRaises(DevelopmentError) as caught:
                    service.disable(target, str(dll), digest)
                self.assertEqual(caught.exception.receipt, receipt)
                self.assertIs(caught.exception.outcome_unknown, unknown)
                self.assertEqual(len(calls), 1)

    def test_malformed_or_wrong_identity_success_remains_unconfirmed(self):
        malformed = ["not JSON", '[]', '{"ok":true,"outcome_unknown":false,"outcome_unknown":true}',
                     json.dumps({"ok": True, "outcome_unknown": False, "module_loaded": True,
                                 "process": {"pid": 999}, "request_id": 1})]
        for report in malformed:
            with self.subTest(report=report), tempfile.TemporaryDirectory() as directory:
                service, dll, digest, target, calls = self.fixture(directory,
                    lambda *_: CommandResult(0, report))
                with self.assertRaises(DevelopmentError) as caught:
                    service.load(target, str(dll), digest)
                self.assertTrue(caught.exception.outcome_unknown)
                self.assertEqual(len(calls), 1)

    def test_helper_timeout_returns_identifiable_unknown_request(self):
        def failed(*_):
            raise DevelopmentError("operation_timeout", "Neutral timeout", outcome_unknown=True)
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, failed)
            with self.assertRaises(DevelopmentError) as caught:
                service.load(target, str(dll), digest)
            receipt = caught.exception.receipt
            self.assertTrue(receipt["outcome_unknown"])
            self.assertGreater(receipt["request_id"], 0)
            self.assertEqual(receipt["process"], target)
            self.assertEqual(len(calls), 1)

    def test_controller_failure_preserves_original_raw_receipt(self):
        raw = {"state": "tool_output_decode_failed", "returncode": 3,
               "stdout_base64": "/w==", "stderr_base64": "",
               "decoding_errors": [{"stream": "stdout", "start": 0, "end": 1}]}

        def failed(*_):
            raise DevelopmentError("tool_output_decode_failed", "Neutral unreadable output",
                                   outcome_unknown=True, receipt=raw)

        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, failed)
            with self.assertRaises(DevelopmentError) as caught:
                service.load(target, str(dll), digest)
            receipt = caught.exception.receipt
            self.assertEqual(receipt["controller_error_receipt"], raw)
            self.assertEqual(receipt["process"], target)
            self.assertEqual(receipt["dll_sha256"], digest)
            self.assertTrue(caught.exception.outcome_unknown)
            self.assertEqual(len(calls), 1)

    def test_invalid_timeout_or_query_id_has_no_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, self.success)
            for timeout in (False, 0, -1, float("inf"), float("nan"), "30"):
                with self.subTest(timeout=timeout), self.assertRaises(DevelopmentError):
                    service.load(target, str(dll), digest, timeout_seconds=timeout)
            for request_id in (False, 0, -1, 1 << 64, "17"):
                with self.subTest(request_id=request_id), self.assertRaises(DevelopmentError):
                    service.status(target, str(dll), digest, request_id=request_id)
            self.assertEqual(calls, [])

    def test_external_configured_workspace_is_supported_without_game_files(self):
        with tempfile.TemporaryDirectory() as directory:
            service, dll, digest, target, calls = self.fixture(directory, self.success)
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            repository = Path(directory) / "repository"
            repository.mkdir()
            controller_dir = repository / "build/native/Release"
            controller_dir.mkdir(parents=True)
            (controller_dir / "sn2_mcp_mod_control.exe").write_bytes(b"neutral controller")
            service.service.repository = repository
            service = ModRuntime(service.service, workspace)
            external = workspace / "NeutralMod.dll"
            external.write_bytes(dll.read_bytes())
            self.assertTrue(service.load(target, str(external), digest)["module_loaded"])
            self.assertEqual(len(calls), 1)
            with self.assertRaises(DevelopmentError) as caught:
                service.load(target, str(dll), digest)
            self.assertEqual(caught.exception.code, "unsafe_path")
            game = workspace / "installed-game"
            game.mkdir()
            installed = game / "NeutralMod.dll"
            installed.write_bytes(external.read_bytes())
            service.service.game = game
            with self.assertRaises(DevelopmentError) as caught:
                service.load(target, str(installed), digest)
            self.assertEqual(caught.exception.code, "protected_path")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
