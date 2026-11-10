"""Скалярные pure-out и сведения об эффекте сохраняются между общими HTTP-клиентами."""
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


class HTTPScalarOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_all_scalar_outputs_false_return_and_unknown_effect_keep_one_connection(self):
        handle, token = "obj-http-scalar-out", "sig-30"
        declarations = {
            "Enabled": ("BoolProperty", 1, True),
            "Count": ("IntProperty", 4, -(1 << 31)),
            "LongCount": ("Int64Property", 8, -(1 << 63)),
            "UnsignedCount": ("UInt32Property", 4, (1 << 32) - 1),
            "UnsignedLong": ("UInt64Property", 8, (1 << 64) - 1),
            "Ratio": ("FloatProperty", 4, 1.25),
            "PreciseRatio": ("DoubleProperty", 8, -2.5),
        }
        arguments = {name: {"out": True} for name in declarations}
        values = {name: value for name, (_, _, value) in declarations.items()}
        metadata = {name: {"type": kind, "element_size": size, "array_dim": 1}
                    for name, (kind, size, _) in declarations.items()}
        metadata["Enabled"]["bool_layout"] = {
            "field_size": 1, "byte_offset": 0, "byte_mask": 1, "field_mask": 1}
        result = {"function": "WriteScalars", "return_type": "BoolProperty", "result": False,
                  "out_parameters": values, "out_parameter_metadata": metadata,
                  "executed": True, "thread_id": 456}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        unknown_effect = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                          "cause_code": "invalid_scalar_value"}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 99}
        invalid_arguments = {**arguments, "LongCount": {"out": False}}
        steps = [
            {"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
                "arguments": arguments, "expected_signature_token": token}, "result": result},
            {"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
                "arguments": invalid_arguments, "expected_signature_token": token},
             "error": {"code": "invalid_argument", "message": "Pure-out требует только {out:true}",
                       "details": no_effect}},
            {"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
                "arguments": arguments, "expected_signature_token": token},
             "error": {"code": "game_result_error", "message": "Вызов выполнен, скалярный выход недействителен",
                       "details": unknown_effect}},
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
                return response.json()["result"], response

            async def initialize(client, name):
                _, response = await rpc(client, "initialize", {
                    "protocolVersion": "2025-06-18", "capabilities": {},
                    "clientInfo": {"name": name, "version": "1"}})
                session = response.headers["mcp-session-id"]
                notification = await client.post("/mcp", headers={
                    "Mcp-Session-Id": session, "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.assertEqual(notification.status_code, 202, notification.text)
                return session

            async def call(client, session, name, parameters):
                answer, _ = await rpc(client, "tools/call", {"name": name, "arguments": parameters}, session)
                return answer

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "first-scalar-client")
                    second_session = await initialize(second, "second-scalar-client")
                    self.assertNotEqual(first_session, second_session)
                    lease = await call(first, first_session, "g1r_session_status", {})
                    self.assertFalse(lease["structuredContent"]["claimed"])
                    success = await call(first, first_session, "g1r_invoke", {
                        "handle": handle, "function": "WriteScalars", "arguments": arguments,
                        "expected_signature_token": token})
                    self.assertFalse(success.get("isError", False), success)
                    self.assertEqual(success["structuredContent"], result)
                    self.assertIs(success["structuredContent"]["result"], False)
                    self.assertIs(success["structuredContent"]["out_parameters"]["Enabled"], True)
                    self.assertEqual(success["structuredContent"]["out_parameters"]["LongCount"], -(1 << 63))
                    self.assertEqual(success["structuredContent"]["out_parameters"]["UnsignedLong"], (1 << 64) - 1)
                    self.assertIs(type(success["structuredContent"]["out_parameters"]["LongCount"]), int)
                    self.assertIs(type(success["structuredContent"]["out_parameters"]["UnsignedLong"]), int)
                    self.assertEqual(success["structuredContent"]["out_parameter_metadata"], metadata)
                    for index, client, session, input_arguments, code, effect in (
                        (1, second, second_session, invalid_arguments, "invalid_argument", no_effect),
                        (2, first, first_session, arguments, "game_result_error", unknown_effect),
                    ):
                        failed = await call(client, session, "g1r_invoke", {
                            "handle": handle, "function": "WriteScalars", "arguments": input_arguments,
                            "expected_signature_token": token})
                        self.assertTrue(failed["isError"])
                        message = next(block["text"] for block in failed["content"] if block["type"] == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertEqual(error["effect"], effect)
                        self.assertIs(error["may_have_executed"], effect["may_have_executed"])
                        self.assertEqual(error["request_id"], native.requests[index]["id"])
                    after = await call(second, second_session, "g1r_native_status", {})
                    self.assertFalse(after.get("isError", False), after)
                    self.assertEqual(after["structuredContent"], status)
                    self.assertEqual(native.connections, 1)
                    lease = await call(second, second_session, "g1r_session_status", {})
                    self.assertFalse(lease["structuredContent"]["claimed"])
            self.assertEqual(native.connections, 1)
            self.assertEqual([item["op"] for item in native.requests], ["invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({item["id"] for item in native.requests}), 4)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
