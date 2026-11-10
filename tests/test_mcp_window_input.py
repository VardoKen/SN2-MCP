"""Shared HTTP input and native sessions without accessing Windows or the game."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from g1r_mcp.development import CommandResult, DevelopmentService
from g1r_mcp.native_client import NativeClient
from g1r_mcp.server import create_server
from g1r_mcp.window_input import WindowInputError
from tests.test_mcp_observations import SessionNativeFixture
from tests.test_mcp_server import DropCompletedStopResponse


class MCPHTTPWindowInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_movieplayer_finish_needs_no_claim_and_lost_http_response_is_not_retried(self):
        cached = {"source": "native_movieplayer_cache", "active_wait": True, "wait_id": "wait-59",
                  "wait_returned": False, "thread_id": 456, "caller_allow_engine_tick": False,
                  "entered_ms": 590, "last_sample_ms": 591, "sample_wait_id": "wait-59",
                  "loading_finished": True, "movie_playing": True, "finish_call_completed": False,
                  "last_returned_wait_id": None, "last_returned_ms": None}
        completed = {"source": "live_native_movieplayer", "wait_id": "wait-59", "thread_id": 456,
                     "loading_finished": True, "finish_call_completed": True}
        after_finish = {**cached, "finish_call_completed": True, "last_sample_ms": 592}
        after_return = {**after_finish, "active_wait": False, "wait_id": None, "wait_returned": True,
                        "last_returned_wait_id": "wait-59", "last_returned_ms": 593}
        steps = [{"op": "movieplayer_status", "params": {}, "result": cached},
                 {"op": "movieplayer_finish", "params": {"expected_wait_id": "wait-59"}, "result": completed},
                 {"op": "movieplayer_status", "params": {}, "result": after_finish},
                 {"op": "movieplayer_status", "params": {}, "result": after_return}]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_bytes(b"class UFixture {}\r\n")
            original = source.read_bytes()
            workspace.mkdir()
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            with patch("g1r_mcp.server.NativeClient", return_value=backend) as factory, \
                 patch("g1r_mcp.server.DevelopmentService") as development_factory:
                server = create_server(modkit, workspace, native_port=native.port, transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, max_request_body_size=sys.maxsize,
                    transport_security=security)
                lossy = DropCompletedStopResponse(app, "g1r_movieplayer_finish")
                counter = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response

                async def initialize(client, name):
                    response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                               "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    return session

                async def tool(client, session, name, arguments=None):
                    result = (await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session)).json()["result"]
                    self.assertFalse(result.get("isError", False), result)
                    return result["structuredContent"]

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=lossy, base_url="http://127.0.0.1:27124") as first, \
                               httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as second:
                        owner, other = await initialize(first, "movie-finish-client"), await initialize(second, "movie-status-reader")
                        self.assertNotEqual(owner, other)
                        listed = (await rpc(second, "tools/list", {}, other)).json()["result"]["tools"]
                        self.assertEqual(len(listed), 64)
                        tools = {item["name"]: item for item in listed}
                        self.assertTrue(tools["g1r_movieplayer_status"]["annotations"]["readOnlyHint"])
                        self.assertFalse(tools["g1r_movieplayer_finish"]["annotations"]["readOnlyHint"])
                        self.assertFalse(tools["g1r_movieplayer_finish"]["annotations"]["idempotentHint"])
                        schema = tools["g1r_movieplayer_finish"]["inputSchema"]
                        self.assertEqual(set(schema["properties"]), {"expected_wait_id", "timeout_seconds"})
                        self.assertEqual(schema["properties"]["expected_wait_id"]["type"], "string")
                        self.assertEqual(set(schema["required"]), {"expected_wait_id"})
                        self.assertEqual(schema["properties"]["timeout_seconds"], {
                            "anyOf": [{"type": "number"}, {"type": "null"}],
                            "default": None, "title": "Timeout Seconds"})
                        observed = await tool(second, other, "g1r_movieplayer_status")
                        self.assertEqual(observed, cached)
                        self.assertEqual(len(native.requests), 1)
                        with self.assertRaises(httpx2.ReadError):
                            await tool(first, owner, "g1r_movieplayer_finish", {"expected_wait_id": observed["wait_id"]})
                        self.assertEqual(lossy.discarded, completed)
                        self.assertTrue(lossy.discarded["finish_call_completed"])
                        self.assertNotIn("game_ready", lossy.discarded)
                        self.assertEqual(len(native.requests), 2)
                        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                     base_url="http://127.0.0.1:27124") as resumed:
                            still_waiting = await tool(resumed, owner, "g1r_movieplayer_status")
                            self.assertEqual(still_waiting, after_finish)
                            self.assertTrue(still_waiting["finish_call_completed"])
                            self.assertTrue(still_waiting["active_wait"])
                            self.assertFalse(still_waiting["wait_returned"])
                            returned = await tool(second, other, "g1r_movieplayer_status")
                            self.assertEqual(returned, after_return)
                            self.assertFalse(returned["active_wait"])
                            self.assertTrue(returned["wait_returned"])
                            self.assertNotIn("game_ready", returned)
                            for client, session in ((resumed, owner), (second, other)):
                                deleted = await client.delete("/mcp", headers={"Mcp-Session-Id": session,
                                    "Accept": "application/json, text/event-stream"})
                                self.assertEqual(deleted.status_code, 200, deleted.text)
                        factory.assert_called_once_with(port=native.port)
                        development_factory.assert_not_called()
                self.assertEqual(native.connections, 1)
                self.assertEqual([request["op"] for request in native.requests],
                                 ["movieplayer_status", "movieplayer_finish", "movieplayer_status", "movieplayer_status"])
                self.assertEqual(len({request["id"] for request in native.requests}), 4)
                self.assertEqual(native.response_sessions, ["fixture-session-1"] * 4)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(list(workspace.iterdir()), [])

    async def test_key_cross_client_stale_record_strict_arguments_and_partial_receipt(self):
        native_status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 48}
        steps = [{"op": "status", "params": {}, "result": native_status} for _ in range(4)]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            repository, modkit, workspace, game = (root / name for name in
                                                    ("repository", "Modkit", "workspace", "Game"))
            for directory in (repository / "tools", modkit / "Script", workspace, game):
                directory.mkdir(parents=True)
            (repository / "tools" / "game_process_state.ps1").write_text("# fixture", encoding="utf-8")
            source = modkit / "Script" / "Fixture.as"
            source.write_bytes(b"class UFixture {}\r\n")
            original = source.read_bytes()
            identity = {"pid": 123, "creation_time": "2026-11-01T00:01:02.1234567Z",
                        "executable": str(game / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe")}
            process_calls = []

            def execute(arguments, timeout):
                script = Path(arguments[arguments.index("-File") + 1]).name
                self.assertEqual(script, "game_process_state.ps1")
                process_calls.append((list(arguments), timeout))
                return CommandResult(0, json.dumps({"processes": [identity]}))

            service = DevelopmentService(repository_root=repository, workspace_root=workspace,
                                         modkit_root=modkit, game_root=game,
                                         executor=execute, powershell="fixture-powershell")
            self.addCleanup(service.close)
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            receipt = {"status": "input_queued", "process": identity, "selected_hwnd": 1024,
                       "window_thread_id": 456, "virtual_key": 120,
                       "messages": [{"message": "WM_KEYDOWN", "message_id": 256, "wparam": 120,
                                     "lparam": 1, "queued": True},
                                    {"message": "WM_KEYUP", "message_id": 257, "wparam": 120,
                                     "lparam": 1 | (1 << 30) | (1 << 31), "queued": True}],
                       "handling_confirmed": False, "outcome_unknown": False, "do_not_retry": False}
            partial = copy.deepcopy(receipt)
            partial.update(status="input_queue_failed", outcome_unknown=True, do_not_retry=True,
                           error={"code": "window_message_failed", "message": "fixture keyup failed"})
            partial["messages"][1].update(queued=False, win32_error=5)
            windows_error = WindowInputError("window_message_failed", "fixture keyup failed", partial)

            with patch("g1r_mcp.server.NativeClient", return_value=backend) as native_factory, \
                 patch("g1r_mcp.server.DevelopmentService", return_value=service) as development_factory, \
                 patch("g1r_mcp.development.queue_key", side_effect=[receipt, windows_error, receipt,
                                                                  receipt, receipt, receipt]) as queue:
                server = create_server(modkit, workspace, game_root=game,
                                       native_port=native.port, transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, transport_security=security)
                counter = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()["result"]

                async def initialize(client, name):
                    nonlocal counter
                    counter += 1
                    response = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": counter, "method": "initialize", "params": {
                            "protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": name, "version": "1"}}})
                    self.assertEqual(response.status_code, 200, response.text)
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    return session

                async def tool(client, session, name, arguments=None):
                    result = await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session)
                    self.assertFalse(result.get("isError", False), result)
                    return result["structuredContent"]

                async def rejected(client, session, arguments, code=None):
                    result = await rpc(client, "tools/call", {"name": "g1r_development_key",
                                                            "arguments": arguments}, session)
                    self.assertTrue(result.get("isError", False), result)
                    text = "\n".join(item.get("text", "") for item in result["content"])
                    if code is not None:
                        payload = json.loads(text[text.index("{"):])
                        self.assertEqual(payload["code"], code)
                        return payload
                    return text

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as first, \
                               httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as second:
                        owner = await initialize(first, "input-first")
                        other = await initialize(second, "input-other")
                        self.assertNotEqual(owner, other)
                        self.assertEqual(await tool(first, owner, "g1r_native_status"), native_status)
                        arguments = {"expected_process": identity, "virtual_key": 120, "expected_window": 1024}
                        before = len(process_calls)
                        for invalid in ({"virtual_key": 120.5}, {"virtual_key": True}, {"expected_window": 1024.5}):
                            with self.subTest(arguments=invalid):
                                await rejected(first, owner, {**arguments, **invalid})
                        self.assertEqual(len(process_calls), before)
                        queue.assert_not_called()
                        queued = await tool(second, other, "g1r_development_key", arguments)
                        self.assertEqual(queued, receipt)
                        self.assertEqual([item["message"] for item in queued["messages"]], ["WM_KEYDOWN", "WM_KEYUP"])
                        self.assertFalse(queued["handling_confirmed"])
                        queue.assert_called_once_with(identity, 120, expected_window=1024)
                        stale_process = {**identity, "pid": 99, "creation_time": "2026-10-31T00:01:02.1234567Z"}
                        stale_record = await tool(first, owner, "g1r_session_acquire", {
                            "reason": "Retained information about an exited process", "expected_process": stale_process})
                        self.assertEqual(stale_record["claim"]["process"], stale_process)
                        self.assertFalse(stale_record["control_required"])
                        await rejected(second, other, {**arguments, "expected_process": {
                            **identity, "creation_time": "2026-11-01T00:01:03.1234567Z"}}, "process_changed")
                        self.assertEqual(queue.call_count, 1)
                        failed = await rejected(first, owner, arguments, "window_message_failed")
                        self.assertEqual(failed["receipt"], partial)
                        self.assertTrue(failed["outcome_unknown"])
                        self.assertTrue(failed["do_not_retry"])
                        self.assertEqual(failed["retry"], "only_explicit_new_request")
                        self.assertEqual(queue.call_count, 2)
                        self.assertEqual(await tool(second, other, "g1r_native_status"), native_status)
                        terminated = await first.delete("/mcp", headers={"Mcp-Session-Id": owner,
                                                        "Accept": "application/json, text/event-stream"})
                        self.assertEqual(terminated.status_code, 200, terminated.text)
                        remaining = await tool(second, other, "g1r_session_status")
                        self.assertEqual(remaining["claim"], stale_record["claim"])
                        self.assertEqual(remaining["scope"], "informational")
                        self.assertFalse(remaining["ownership_enforced"])
                        self.assertEqual(queue.call_count, 2)
                        self.assertEqual(len(process_calls), before + 3)
                        self.assertEqual(await tool(second, other, "g1r_native_status"), native_status)
                        restored_input = await tool(second, other, "g1r_development_key", arguments)
                        self.assertEqual(restored_input, receipt)
                        self.assertEqual(queue.call_count, 3)
                        self.assertEqual(queue.call_args.args, (identity, 120))
                        self.assertEqual(queue.call_args.kwargs, {"expected_window": 1024})
                        revoked = await first.post("/mcp", headers={"Mcp-Session-Id": owner,
                            "Accept": "application/json, text/event-stream"}, json={"jsonrpc": "2.0",
                            "id": 9999, "method": "tools/call", "params": {
                                "name": "g1r_development_key", "arguments": arguments}})
                        self.assertEqual(revoked.status_code, 404, revoked.text)
                        self.assertEqual(queue.call_count, 3)
                        self.assertEqual(await tool(second, other, "g1r_native_status"), native_status)
                        third = await initialize(first, "input-explicit-protection-other")
                        self.assertNotEqual(third, other)
                        protected = await tool(second, other, "g1r_session_acquire", {
                            "reason": "Protect only keyboard input", "expected_process": stale_process,
                            "protected_tools": ["g1r_development_key"]})
                        self.assertEqual(protected["scope"], "optional_tool_protection")
                        self.assertEqual(protected["protected_tools"], ["g1r_development_key"])
                        self.assertTrue(protected["caller_owns_protection"])
                        self.assertFalse(protected["control_required"])
                        before_protection = len(process_calls)
                        blocked = await rejected(first, third, arguments, "session_protected")
                        self.assertFalse(blocked["outcome_unknown"])
                        self.assertEqual(queue.call_count, 3)
                        self.assertEqual(len(process_calls), before_protection)
                        observed = await tool(first, third, "g1r_session_status")
                        self.assertEqual(observed["protection"], protected["protection"])
                        self.assertFalse(observed["caller_owns_protection"])
                        self.assertEqual(await tool(second, other, "g1r_development_key", arguments), receipt)
                        self.assertEqual(queue.call_count, 4)
                        before_takeover = len(process_calls)
                        replacement = await tool(first, third, "g1r_session_acquire", {
                            "reason": "Explicit input takeover", "protected_tools": ["g1r_development_key"],
                            "takeover": True})
                        self.assertTrue(replacement["caller_owns_protection"])
                        self.assertNotEqual(replacement["protection"]["claim_id"], protected["protection"]["claim_id"])
                        await rejected(second, other, arguments, "session_protected")
                        self.assertEqual(queue.call_count, 4)
                        self.assertEqual(len(process_calls), before_takeover)
                        self.assertEqual(await tool(first, third, "g1r_development_key", arguments), receipt)
                        self.assertEqual(queue.call_count, 5)
                        deleted = await first.delete("/mcp", headers={"Mcp-Session-Id": third,
                            "Accept": "application/json, text/event-stream"})
                        self.assertEqual(deleted.status_code, 200, deleted.text)
                        before_release = len(process_calls)
                        await rejected(second, other, arguments, "session_protected")
                        self.assertEqual(queue.call_count, 5)
                        self.assertEqual(len(process_calls), before_release)
                        remaining_protection = await tool(second, other, "g1r_session_status")
                        self.assertEqual(remaining_protection["protection"], replacement["protection"])
                        self.assertFalse(remaining_protection["disconnect_releases_claim"])
                        self.assertFalse(remaining_protection["automatic_game_stop"])
                        released = await tool(second, other, "g1r_session_release")
                        self.assertTrue(released["released"])
                        self.assertFalse(released["game_stopped"])
                        self.assertEqual(await tool(second, other, "g1r_development_key", arguments), receipt)
                        self.assertEqual(queue.call_count, 6)
                        self.assertEqual(len(process_calls), before_protection + 3)
                        self.assertFalse((await tool(second, other, "g1r_session_status"))["claimed"])
                        self.assertIsNone((await tool(second, other, "g1r_session_status"))["protection"])
                        native_factory.assert_called_once_with(port=native.port)
                        development_factory.assert_called_once()
                self.assertEqual(native.connections, 1)
                self.assertEqual([item["op"] for item in native.requests], ["status"] * 4)
                self.assertEqual(len({item["id"] for item in native.requests}), 4)
                self.assertEqual(source.read_bytes(), original)
                self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
