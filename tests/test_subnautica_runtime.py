"""Neutral process, loader receipt and native-session checks for Subnautica."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.development import CommandResult, DevelopmentError, RuntimeService
from sn2_mcp.native_client import NativeError
from sn2_mcp.server import create_server


class RuntimeServiceTests(unittest.TestCase):
    def fixture(self, root, replies):
        repository = root / "repository"
        tools = repository / "tools"
        tools.mkdir(parents=True)
        for name in ("game_process_state.ps1", "load_native.ps1"):
            (tools / name).write_text("# Neutral fixed-tool fixture\n", encoding="utf-8")
        game = root / "game"
        executable = game / "Subnautica2" / "Binaries" / "Win64" / "Subnautica2-Win64-Shipping.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"neutral executable identity")
        target = {"pid": 123, "creation_time": "2026-11-10T10:11:12.1234567Z", "executable": str(executable)}
        calls = []

        def execute(arguments, timeout):
            calls.append((arguments, timeout))
            response = replies.pop(0)
            return response if isinstance(response, CommandResult) else CommandResult(0, json.dumps(response))

        service = RuntimeService(repository_root=repository, game_root=game, executor=execute)
        return service, repository, target, calls

    def assert_code(self, code, callback):
        with self.assertRaises(DevelopmentError) as caught:
            callback()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_snapshot_preserves_multiple_instances_and_filetime_precision(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, _, first, calls = self.fixture(Path(temporary), replies)
            second = {**first, "pid": 124, "creation_time": "2026-11-10T10:11:12.1234568Z"}
            replies.append({"processes": [first, second]})
            result = service.process_state()
            self.assertEqual([item["pid"] for item in result["processes"]], [123, 124])
            self.assertEqual(int(result["processes"][1]["creation_time"]) - int(result["processes"][0]["creation_time"]), 1)
            self.assertEqual(result["processes"][0]["creation_time"], "134387790721234567")
            self.assertTrue(result["running"])
            self.assertFalse(result["selected"])
            self.assertEqual(result["source"], "native_process_handle_snapshot")
            self.assertEqual(len(calls), 1)
            self.assertIn("game_process_state.ps1", calls[0][0][calls[0][0].index("-File") + 1])
            self.assertNotIn("-ExpectedPid", calls[0][0])

    def test_target_validation_rejects_pid_reuse_and_wrong_executable(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, _, target, calls = self.fixture(Path(temporary), replies)
            replies.append({"processes": [{**target, "creation_time": "2026-11-10T10:11:12.1234568Z"}]})
            self.assert_code("process_changed", lambda: service.validate_target(target))
            self.assert_code("process_path_mismatch", lambda: service.validate_target({**target, "executable": str(service.bootstrap_executable)}))
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0][calls[0][0].index("-Executable") + 1], target["executable"])

    def test_duplicate_process_and_invalid_filetime_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, _, target, calls = self.fixture(Path(temporary), replies)
            replies.append({"processes": [target, target]})
            self.assert_code("tool_protocol_error", service.process_state)
            for creation in ("0", str(1 << 64), "-1", "invalid"):
                with self.subTest(creation=creation):
                    self.assert_code("invalid_process_identity", lambda: service.validate_target({**target, "creation_time": creation}))
            self.assertEqual(len(calls), 1)

    def test_invalid_calendar_timestamp_is_a_structured_identity_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, _, target, calls = self.fixture(Path(temporary), replies)
            self.assert_code("invalid_process_identity", lambda: service.validate_target({
                **target, "creation_time": "2026-99-10T10:11:12.1234567Z"}))
            self.assertEqual(calls, [])

    def test_loaded_and_already_loaded_receipts_keep_executor_unconfirmed(self):
        for state in ("loaded", "already_loaded"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                replies = []
                service, repository, target, calls = self.fixture(Path(temporary), replies)
                dll = repository / "sn2_mcp_native.dll"
                dll.write_bytes(b"neutral DLL payload")
                digest = hashlib.sha256(dll.read_bytes()).hexdigest()
                normalized = RuntimeService._runtime_identity(target)
                replies.extend([{"processes": [target]}, {"state": state, "process": normalized, "dll_sha256": digest}])
                result = service.load_native(target, str(dll), digest.upper())
                self.assertEqual(result["state"], state)
                self.assertEqual(result["process"], normalized)
                self.assertFalse(result["game_ready"])
                self.assertFalse(result["outcome_unknown"])
                self.assertEqual(len(calls), 2)
                arguments = calls[1][0]
                self.assertTrue(arguments[arguments.index("-File") + 1].endswith("load_native.ps1"))
                for key, value in (("-Executable", target["executable"]), ("-DllPath", str(dll)),
                                   ("-ExpectedPid", "123"), ("-ExpectedCreationTime", normalized["creation_time"]),
                                   ("-ExpectedDllSha256", digest)):
                    self.assertEqual(arguments[arguments.index(key) + 1], value)
                self.assertEqual(arguments[-1], "-Json")
                self.assertEqual(calls[1][1], 30)

    def test_changed_dll_is_rejected_before_loader(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, repository, target, calls = self.fixture(Path(temporary), replies)
            dll = repository / "sn2_mcp_native.dll"
            dll.write_bytes(b"changed payload")
            replies.append({"processes": [target]})
            error = self.assert_code("dll_changed", lambda: service.load_native(target, str(dll), "0" * 64))
            self.assertFalse(error.outcome_unknown)
            self.assertEqual(len(calls), 1)

    def test_unconfirmed_loader_receipt_retains_unknown_effect_without_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, repository, target, calls = self.fixture(Path(temporary), replies)
            dll = repository / "sn2_mcp_native.dll"
            dll.write_bytes(b"neutral DLL payload")
            digest = hashlib.sha256(dll.read_bytes()).hexdigest()
            receipt = {"state": "thread_wait_unknown", "process": target, "dll_sha256": digest, "outcome_unknown": True}
            replies.extend([{"processes": [target]}, receipt])
            error = self.assert_code("native_load_unconfirmed", lambda: service.load_native(target, str(dll), digest))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt, receipt)
            self.assertEqual(len(calls), 2)
            self.assertEqual(replies, [])

    def test_invalid_loader_process_receipt_keeps_mutation_outcome_unknown(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = []
            service, repository, target, calls = self.fixture(Path(temporary), replies)
            dll = repository / "sn2_mcp_native.dll"
            dll.write_bytes(b"neutral DLL payload")
            digest = hashlib.sha256(dll.read_bytes()).hexdigest()
            receipt = {"state": "loaded", "process": {"pid": 123}, "dll_sha256": digest}
            replies.extend([{"processes": [target]}, receipt])
            error = self.assert_code("native_load_unconfirmed", lambda: service.load_native(target, str(dll), digest))
            self.assertTrue(error.outcome_unknown)
            self.assertEqual(error.receipt, receipt)
            self.assertEqual(len(calls), 2)

    def test_nonzero_loader_failure_preserves_known_or_unknown_effect_and_receipt(self):
        for unknown, native_present in ((False, False), (False, True), (True, True)):
            with self.subTest(unknown=unknown, native_present=native_present), tempfile.TemporaryDirectory() as temporary:
                replies = []
                service, repository, target, calls = self.fixture(Path(temporary), replies)
                dll = repository / "SN2_MCP.dll"
                dll.write_bytes(b"neutral DLL payload")
                digest = hashlib.sha256(dll.read_bytes()).hexdigest()
                code = "load_outcome_unknown" if unknown else "process_changed"
                native = {"ok": False, "code": code, "message": "Neutral native failure",
                          "outcome_unknown": unknown} if native_present else None
                receipt = {"state": "outcome_unknown" if unknown else "failed", "code": code,
                           "message": "The selected DLL load was not confirmed.",
                           "native_result": native, "outcome_unknown": unknown}
                replies.extend([{"processes": [target]}, CommandResult(2, "\ufeff" + json.dumps(receipt))])
                error = self.assert_code(code, lambda: service.load_native(target, str(dll), digest))
                self.assertEqual(str(error), receipt["message"])
                self.assertIs(error.outcome_unknown, unknown)
                self.assertEqual(error.receipt, receipt)
                self.assertEqual(len(calls), 2)
                self.assertEqual(replies, [])

    def test_malformed_nonzero_loader_receipt_never_claims_no_effect(self):
        valid = {"state": "failed", "code": "process_changed", "message": "Neutral rejection",
                 "native_result": None, "outcome_unknown": False}
        malformed = ["not JSON", "[]", '{"outcome_unknown":true,"outcome_unknown":false}',
                     json.dumps({**valid, "outcome_unknown": "false"}),
                     json.dumps({key: value for key, value in valid.items() if key != "native_result"}),
                     json.dumps({**valid, "native_result": {"ok": False, "code": "process_changed",
                                                           "outcome_unknown": True}}),
                     json.dumps({**valid, "state": "loaded"})]
        for stdout in malformed:
            with self.subTest(stdout=stdout), tempfile.TemporaryDirectory() as temporary:
                replies = []
                service, repository, target, calls = self.fixture(Path(temporary), replies)
                dll = repository / "SN2_MCP.dll"
                dll.write_bytes(b"neutral DLL payload")
                digest = hashlib.sha256(dll.read_bytes()).hexdigest()
                replies.extend([{"processes": [target]}, CommandResult(2, stdout, "Neutral failed command")])
                error = self.assert_code("tool_failed", lambda: service.load_native(target, str(dll), digest))
                self.assertTrue(error.outcome_unknown)
                self.assertEqual(len(calls), 2)
                self.assertEqual(replies, [])
                if stdout.startswith('{"state"'):
                    self.assertEqual(error.receipt, json.loads(stdout))
                else:
                    self.assertIsNone(error.receipt)

    def test_fixed_tool_failure_has_unknown_effect_only_for_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            replies = [CommandResult(1, "", "fixture failure")]
            service, repository, target, calls = self.fixture(Path(temporary), replies)
            self.assertFalse(self.assert_code("tool_failed", service.process_state).outcome_unknown)
            dll = repository / "sn2_mcp_native.dll"
            dll.write_bytes(b"neutral DLL payload")
            digest = hashlib.sha256(dll.read_bytes()).hexdigest()
            replies.extend([{"processes": [target]}, CommandResult(1, "", "fixture failure")])
            self.assertTrue(self.assert_code("tool_failed", lambda: service.load_native(target, str(dll), digest)).outcome_unknown)
            self.assertEqual(len(calls), 3)


class NativeTargetContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    def roots(self, temporary):
        workspace = Path(temporary) / "workspace"
        workspace.mkdir()
        return workspace

    def target(self):
        return {"pid": 123, "executable": r"E:\Neutral\Subnautica2\Binaries\Win64\Subnautica2-Win64-Shipping.exe",
                "creation_time": "134072430721234567"}

    def status(self, target, **changes):
        return {"session_id": "neutral-session", "process_id": target["pid"],
                "process_creation_filetime": target["creation_time"], "ready": False,
                "abi_state": "unverified", "accepted_types": {}, **changes}

    async def tool_failure(self, server, name, arguments, code):
        with self.assertRaises(ToolError) as caught:
            await server.call_tool(name, arguments)
        message = str(caught.exception)
        error = json.loads(message[message.index("{"):])
        self.assertEqual(error["code"], code)
        return error

    async def test_native_enable_validates_pid_creation_and_session_before_mutation(self):
        for changed in ({"process_id": 124}, {"process_creation_filetime": "134072430721234568"},
                        {"session_id": "other-session"}):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temporary:
                native, runtime, target = Mock(), Mock(), self.target()
                runtime.validate_target.return_value = target
                native.request.return_value = self.status(target, **changed)
                with patch("sn2_mcp.server.NativeClient", return_value=native), \
                        patch("sn2_mcp.server.RuntimeService", return_value=runtime):
                    server = create_server(self.roots(temporary))
                    await self.tool_failure(server, "sn2_native_enable", {
                        "target_process": target, "expected_native_session_id": "neutral-session"}, "native_identity_mismatch")
                native.request.assert_called_once_with("status", {"expected_process_id": 123,
                    "expected_creation_filetime": target["creation_time"]})

    async def test_matched_enable_and_following_calls_forward_exact_process_expectations(self):
        with tempfile.TemporaryDirectory() as temporary:
            native, runtime, target = Mock(), Mock(), self.target()
            runtime.validate_target.return_value = target
            native.request.side_effect = [self.status(target), {"hook_installed": True, "ready": False}, {"items": []}]
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                    patch("sn2_mcp.server.RuntimeService", return_value=runtime):
                server = create_server(self.roots(temporary))
                enabled = await server.call_tool("sn2_native_enable", {
                    "target_process": target, "expected_native_session_id": "neutral-session"})
                self.assertFalse(enabled.structured_content["ready"])
                await server.call_tool("sn2_find_objects", {"query": "Neutral"})
            self.assertEqual([call.args[0] for call in native.request.call_args_list], ["status", "enable_game", "objects"])
            for call in native.request.call_args_list:
                self.assertEqual(call.args[1]["expected_process_id"], target["pid"])
                self.assertEqual(call.args[1]["expected_creation_filetime"], target["creation_time"])
            self.assertEqual(native.request.call_args_list[1].args[1]["expected_session_id"], "neutral-session")

    async def test_status_and_capabilities_reject_another_native_process(self):
        for name in ("sn2_native_status", "sn2_capabilities"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                native, runtime, target = Mock(), Mock(), self.target()
                runtime.validate_target.return_value = target
                native.request.return_value = self.status(target, process_id=124, accepted_types={"BoolProperty": "accepted"})
                with patch("sn2_mcp.server.NativeClient", return_value=native), \
                        patch("sn2_mcp.server.RuntimeService", return_value=runtime):
                    server = create_server(self.roots(temporary))
                    await self.tool_failure(server, name, {"target_process": target}, "native_process_mismatch")
                self.assertEqual(native.request.call_count, 1)

    async def test_native_unknown_effect_is_preserved_and_never_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            native = Mock()
            native.request.side_effect = NativeError("connection_lost", "The response was lost", may_have_executed=True,
                                                    request_id="neutral-request", details={"do_not_retry": True})
            with patch("sn2_mcp.server.NativeClient", return_value=native):
                server = create_server(self.roots(temporary))
                error = await self.tool_failure(server, "sn2_invoke", {"handle": "neutral-handle", "function": "NeutralEffect"}, "connection_lost")
            self.assertTrue(error["may_have_executed"])
            self.assertEqual(error["request_id"], "neutral-request")
            self.assertEqual(error["effect"], {"do_not_retry": True})
            native.request.assert_called_once_with("invoke", {"handle": "neutral-handle", "function": "NeutralEffect", "arguments": {}})

    async def test_load_tool_preserves_failure_receipt_and_effect_certainty(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown), tempfile.TemporaryDirectory() as temporary:
                native, runtime, target = Mock(), Mock(), self.target()
                runtime.validate_target.return_value = target
                receipt = {"state": "outcome_unknown" if unknown else "failed", "code": "neutral_load_failure",
                           "message": "Neutral failure receipt", "native_result": None,
                           "outcome_unknown": unknown}
                runtime.load_native.side_effect = DevelopmentError(receipt["code"], receipt["message"],
                    outcome_unknown=unknown, receipt=receipt)
                with patch("sn2_mcp.server.NativeClient", return_value=native), \
                        patch("sn2_mcp.server.RuntimeService", return_value=runtime):
                    server = create_server(self.roots(temporary))
                    error = await self.tool_failure(server, "sn2_native_load", {
                        "target_process": target, "dll_path": "E:/Neutral/SN2_MCP.dll",
                        "expected_dll_sha256": "0" * 64}, "neutral_load_failure")
                self.assertIs(error["outcome_unknown"], unknown)
                self.assertIs(error["do_not_retry"], unknown)
                self.assertEqual(error["receipt"], receipt)
                runtime.load_native.assert_called_once_with(target, "E:/Neutral/SN2_MCP.dll", "0" * 64)
                native.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
