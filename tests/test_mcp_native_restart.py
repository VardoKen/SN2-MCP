"""Подтверждённая остановка освобождает TCP перед следующим development-сеансом."""
import json
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from g1r_mcp.server import create_server
from tests.test_mcp_observations import SessionNativeFixture


class RestartNativeFixture(SessionNativeFixture):
    def __init__(self, steps, port=None):
        super().__init__(steps)
        if port is not None:
            self.listener.close()
            self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.listener.bind(("127.0.0.1", port))
            self.listener.listen(1)
            self.listener.settimeout(0.1)
            self.port = port


STDIO_FIXTURE = """
import json
from pathlib import Path
import sys
from unittest.mock import patch
from g1r_mcp.development import DevelopmentError
from g1r_mcp.server import create_server

identities = json.loads(sys.argv[4])
class FixtureDevelopment:
    def __init__(self):
        self.current = identities[0]
    def close(self):
        pass
    def stop(self, expected_process):
        if expected_process['pid'] == 998:
            raise DevelopmentError('operation_timeout', 'Исход остановки неизвестен', outcome_unknown=True)
        if expected_process != self.current:
            raise DevelopmentError('process_changed', 'Исходный процесс не совпал')
        process = self.current
        self.current = None
        return {'state': 'stop_result', 'process': process,
                'tool_result': {'state': 'stopped', 'process': process}, 'outcome_unknown': False}
    def start(self, **params):
        self.current = identities[1]
        return {'state': 'launch_requested', 'process': self.current, 'outcome_unknown': False}
service = FixtureDevelopment()
with patch('g1r_mcp.server.DevelopmentService', return_value=service):
    server = create_server(Path(sys.argv[1]), Path(sys.argv[2]), native_port=int(sys.argv[3]))
    server.run(transport='stdio')
"""


class MCPNativeRestartTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    def roots(self, temporary):
        root = Path(temporary)
        modkit = root / "Modkit"
        (modkit / "Script").mkdir(parents=True)
        (modkit / "Script" / "Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        return modkit, workspace

    async def test_same_stdio_session_connects_to_second_listener_after_confirmed_stop(self):
        first_status = {"ready": True, "session_id": "native-owner-first", "tick_count": 51}
        second_status = {"ready": True, "session_id": "native-owner-second", "tick_count": 7}
        identities = [{"pid": pid, "creation_time": f"2026-10-31T00:01:0{index}.1234567Z",
                       "executable": "C:/isolated/Game/G1R-Win64-Shipping.exe"}
                      for index, pid in enumerate((123, 124))]
        first = RestartNativeFixture([{"op": "status", "params": {}, "result": first_status}] * 3)
        first.__enter__()
        second = None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                modkit, workspace = self.roots(temporary)
                parameters = StdioServerParameters(command=sys.executable, args=[
                    "-B", "-c", STDIO_FIXTURE, str(modkit), str(workspace), str(first.port),
                    json.dumps(identities)])
                async with stdio_client(parameters) as (incoming, outgoing):
                    async with ClientSession(incoming, outgoing) as client:
                        await client.initialize()
                        initial = await client.call_tool("g1r_native_status", {})
                        self.assertFalse(initial.is_error, initial.content)
                        self.assertEqual(initial.structured_content, first_status)
                        for pid, expected_code, expected_unknown in (
                                (999, "process_changed", False), (998, "operation_timeout", True)):
                            refused = await client.call_tool("g1r_development_stop", {
                                "expected_process": {**identities[0], "pid": pid}})
                            self.assertTrue(refused.is_error, refused.content)
                            text = next(block.text for block in refused.content if block.type == "text")
                            error = json.loads(text[text.index("{"):])
                            self.assertEqual(error["code"], expected_code)
                            self.assertIs(error["outcome_unknown"], expected_unknown)
                            self.assertEqual(error["retry"], "only_explicit_new_request")
                            retained = await client.call_tool("g1r_native_status", {})
                            self.assertFalse(retained.is_error, retained.content)
                            self.assertEqual(retained.structured_content, first_status)
                        stopped = await client.call_tool("g1r_development_stop", {
                            "expected_process": identities[0]})
                        self.assertFalse(stopped.is_error, stopped.content)
                        self.assertIs(stopped.structured_content["outcome_unknown"], False)
                        first.__exit__(None, None, None)
                        second = RestartNativeFixture([
                            {"op": "status", "params": {}, "result": second_status},
                            {"op": "invoke", "params": {"handle": "obj-first-owner", "function": "ReadMarker", "arguments": {}},
                             "error": {"code": "stale_handle", "message": "Ссылка принадлежит предыдущему владельцу",
                                       "details": {"executed": False, "may_have_executed": False, "do_not_retry": False}}},
                            {"op": "status", "params": {}, "result": second_status},
                        ], port=first.port)
                        second.__enter__()
                        launched = await client.call_tool("g1r_development_start", {
                            "relative_package": "Probe", "expected_manifest_sha256": "a" * 64})
                        self.assertFalse(launched.is_error, launched.content)
                        self.assertEqual(launched.structured_content["process"], identities[1])
                        restarted = await client.call_tool("g1r_native_status", {})
                        self.assertFalse(restarted.is_error, restarted.content)
                        self.assertEqual(restarted.structured_content, second_status)
                        stale = await client.call_tool("g1r_invoke", {
                            "handle": "obj-first-owner", "function": "ReadMarker"})
                        self.assertTrue(stale.is_error, stale.content)
                        text = next(block.text for block in stale.content if block.type == "text")
                        error = json.loads(text[text.index("{"):])
                        self.assertEqual(error["code"], "stale_handle")
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], second.requests[1]["id"])
                        following = await client.call_tool("g1r_native_status", {})
                        self.assertFalse(following.is_error, following.content)
                        self.assertEqual(following.structured_content, second_status)
                        final_stop = await client.call_tool("g1r_development_stop", {
                            "expected_process": identities[1]})
                        self.assertFalse(final_stop.is_error, final_stop.content)
                self.assertEqual(first.connections, 1)
                self.assertEqual(second.connections, 1)
                self.assertEqual([request["op"] for request in first.requests], ["status"] * 3)
                self.assertEqual([request["op"] for request in second.requests], ["status", "invoke", "status"])
                self.assertEqual(len({request["id"] for request in first.requests + second.requests}), 6)
        finally:
            if first.thread.is_alive() or first.stream is not None:
                first.__exit__(*sys.exc_info())
            if second is not None:
                second.__exit__(*sys.exc_info())

    async def test_unconfirmed_stop_results_do_not_replace_or_close_native_client(self):
        process = {"pid": 123, "creation_time": "2026-10-31T00:01:02.1234567Z",
                   "executable": "C:/isolated/Game/G1R-Win64-Shipping.exe"}
        confirmed = {"state": "stop_result", "process": process,
                     "tool_result": {"state": "stopped", "process": process}, "outcome_unknown": False}
        with tempfile.TemporaryDirectory() as temporary:
            modkit, workspace = self.roots(temporary)
            old_native = Mock()
            new_native = Mock()
            service = Mock()
            with patch("g1r_mcp.server.NativeClient", side_effect=[old_native, new_native]) as factory, \
                    patch("g1r_mcp.server.DevelopmentService", return_value=service):
                server = create_server(modkit, workspace, native_port=28123)
                for result in (
                        {**confirmed, "outcome_unknown": True},
                        {**confirmed, "outcome_unknown": 0},
                        {**confirmed, "state": "not_running"},
                        {**confirmed, "process": {**process, "pid": 124},
                         "tool_result": {"state": "stopped", "process": {**process, "pid": 124}}},
                        {**confirmed, "tool_result": {"state": "stopped", "process": {**process, "pid": 124}}},
                        {**confirmed, "tool_result": {"state": "unknown", "process": process}}):
                    service.stop.return_value = result
                    await server.call_tool("g1r_development_stop", {"expected_process": process})
                    old_native.close.assert_not_called()
                    self.assertEqual(factory.call_count, 1)
                service.stop.return_value = confirmed
                await server.call_tool("g1r_development_stop", {"expected_process": {**process, "extra": "ignored_by_identity"}})
                old_native.close.assert_called_once_with()
                self.assertEqual(factory.call_args_list, [unittest.mock.call(port=28123)] * 2)
                new_native.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
