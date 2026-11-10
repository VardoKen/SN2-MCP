"""Токен относится к общему native-клиенту, а не к отдельному HTTP-чату."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from g1r_mcp.native_client import NativeClient
from g1r_mcp.server import create_server
from tests.test_mcp_observations import SessionNativeFixture


class HTTPFunctionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_two_http_sessions_use_one_native_contract_without_implicit_retry(self):
        handle, token = "obj-http-contract", "sig-17"
        signature = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                     "items": [{"name": "ReadMarker", "signature_token": token,
                                "signature_status": "complete", "parameters": []}]}
        called = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                  "function": "ReadMarker", "signature_token": token,
                  "signature_verified": True, "return": {"type": "IntProperty", "value": 71}}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"version": "fixture", "session_id": "fixture-session-1", "tick_count": 50}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                                               "function_query": "ReadMarker"}, "result": signature},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadMarker", "arguments": {},
                                            "expected_signature_token": token}, "result": called},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadMarker", "arguments": {},
                                            "expected_signature_token": token},
             "error": {"code": "unknown_signature_token", "message": "Токен отсутствует в текущей native-сессии",
                       "details": no_effect}},
            {"op": "status", "params": {}, "result": status},
        ]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            modkit = root / "Modkit"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_text("class UFixture {}\n", encoding="utf-8")
            original = source.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            with patch("g1r_mcp.server.NativeClient", return_value=backend) as factory:
                server = create_server(modkit, workspace, native_port=native.port,
                                       transport="streamable-http")
            factory.assert_called_once_with(port=native.port)
            app = server.streamable_http_app(
                host="127.0.0.1", streamable_http_path="/mcp", stateless_http=False, json_response=True,
                transport_security=TransportSecuritySettings(
                    enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:27124"],
                    allowed_origins=["http://127.0.0.1:27124"]))
            request_id = 0

            async def rpc(client, method, params, session=None):
                nonlocal request_id
                request_id += 1
                headers = {"Accept": "application/json, text/event-stream"}
                if session is not None:
                    headers["Mcp-Session-Id"] = session
                response = await client.post("/mcp", headers=headers,
                    json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                self.assertEqual(response.status_code, 200, response.text)
                return response

            async def initialize(client, name):
                initialized = await rpc(client, "initialize", {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": name, "version": "1"}})
                session = initialized.headers["mcp-session-id"]
                notification = await client.post("/mcp", headers={
                    "Mcp-Session-Id": session, "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.assertEqual(notification.status_code, 202, notification.text)
                return session

            async def call(client, session, name, arguments):
                response = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                return response.json()["result"]

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "first-contract-client")
                    second_session = await initialize(second, "second-contract-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "g1r_object_functions", {
                        "handle": handle, "function_query": "ReadMarker"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], signature)
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    result = await call(second, second_session, "g1r_invoke", {
                        "handle": handle, "function": "ReadMarker", "expected_signature_token": shared_token})
                    self.assertFalse(result.get("isError", False), result)
                    self.assertEqual(result["structuredContent"], called)
                    self.assertEqual(native.connections, 1)
                    unknown = await call(first, first_session, "g1r_invoke", {
                        "handle": handle, "function": "ReadMarker", "expected_signature_token": shared_token})
                    self.assertTrue(unknown["isError"])
                    payload = next(block["text"] for block in unknown["content"] if block["type"] == "text")
                    error = json.loads(payload[payload.index("{"):])
                    self.assertEqual(error["code"], "unknown_signature_token")
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[2]["id"])
                    after = await call(second, second_session, "g1r_native_status", {})
                    self.assertFalse(after.get("isError", False), after)
                    self.assertEqual(after["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([item["op"] for item in native.requests], ["functions", "invoke", "invoke", "status"])
            self.assertEqual(len({item["id"] for item in native.requests}), 4)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
