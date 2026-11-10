"""Unicode FString и сведения об очистке проходят общий HTTP без повторного invoke."""
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


class HTTPStringInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_unicode_input_const_metadata_cleanup_and_nul_error_keep_shared_connection(self):
        handle, token = "obj-http-string-input", "sig-70"
        text = "Готика 🐉 — мир\nстрока"
        text += "🙂" * ((65536 - len(text.encode("utf-16-le")) // 2) // 2)
        text += "я" * (65536 - len(text.encode("utf-16-le")) // 2)
        utf16_units = len(text.encode("utf-16-le")) // 2
        arguments = {"Input": text, "Length": {"out": True}}
        reflection = {"items": [{"name": "EchoWithLength", "signature_token": token,
            "signature_status": "complete", "parameters": [
                {"name": "Input", "type": "StrProperty", "offset": 0, "element_size": 16,
                 "array_dim": 1, "flags": 0x182, "return": False, "out": True,
                 "reference": False, "const": True, "direction": "input", "passing": "const_reference",
                          "argument_constraints": {"max_utf16_units": 2147483646, "embedded_nul": "rejected",
                                          "ownership": "engine_owned_copy"}},
                {"name": "Length", "type": "IntProperty", "element_size": 4, "array_dim": 1,
                 "flags": 0x180, "out": True, "reference": False, "const": False,
                 "return": False, "direction": "output", "argument_marker": {"out": True}},
                {"name": "ReturnValue", "type": "StrProperty", "element_size": 16, "array_dim": 1,
                 "flags": 0x480, "return": True, "out": False, "reference": False, "const": False}]}]}
        result = {"function": "EchoWithLength", "return_type": "StrProperty", "result": text,
                  "out_parameters": {"Length": utf16_units},
                  "out_parameter_metadata": {"Length": {"type": "IntProperty", "element_size": 4, "array_dim": 1}},
                  "executed": True, "thread_id": 456, "cleanup_confirmed": True,
                  "input_cleanup_confirmed": True}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 121}
        malformed = {"Input": "до\x00после", "Length": {"out": True}}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                "function_query": "EchoWithLength"}, "result": reflection},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoWithLength",
                "arguments": arguments, "expected_signature_token": token}, "result": result},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoWithLength",
                "arguments": malformed, "expected_signature_token": token},
             "error": {"code": "invalid_argument", "message": "FString не поддерживает встроенный NUL",
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
                    first_session = await initialize(first, "string-listing-client")
                    second_session = await initialize(second, "string-invoke-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "g1r_object_functions", {
                        "handle": handle, "function_query": "EchoWithLength"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflection)
                    parameter = listed["structuredContent"]["items"][0]["parameters"][0]
                    self.assertEqual(parameter["passing"], "const_reference")
                    self.assertEqual(parameter["direction"], "input")
                    self.assertEqual(parameter["flags"], 0x182)
                    self.assertEqual(parameter["argument_constraints"]["ownership"], "engine_owned_copy")
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    echoed = await call(second, second_session, "g1r_invoke", {
                        "handle": handle, "function": "EchoWithLength", "arguments": arguments,
                        "expected_signature_token": shared_token})
                    self.assertFalse(echoed.get("isError", False), echoed)
                    self.assertEqual(echoed["structuredContent"], result)
                    self.assertEqual(echoed["structuredContent"]["result"], text)
                    self.assertEqual(utf16_units, 65536)
                    self.assertEqual(echoed["structuredContent"]["out_parameters"]["Length"], utf16_units)
                    self.assertIs(echoed["structuredContent"]["cleanup_confirmed"], True)
                    self.assertIs(echoed["structuredContent"]["input_cleanup_confirmed"], True)
                    denied = await call(first, first_session, "g1r_invoke", {
                        "handle": handle, "function": "EchoWithLength", "arguments": malformed,
                        "expected_signature_token": shared_token})
                    self.assertTrue(denied["isError"])
                    message = next(block["text"] for block in denied["content"] if block["type"] == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[2]["id"])
                    checked = await call(second, second_session, "g1r_native_status", {})
                    self.assertFalse(checked.get("isError", False), checked)
                    self.assertEqual(checked["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 4)
            self.assertEqual(native.requests[1]["params"]["arguments"]["Input"], text)
            self.assertEqual(native.requests[1]["params"]["arguments"]["Input"].encode("utf-16-le"), text.encode("utf-16-le"))
            self.assertEqual(native.requests[2]["params"]["arguments"]["Input"], "до\x00после")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
