"""FLinearColor сохраняет RGBA и отражённую раскладку через общий HTTP MCP."""

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


class HTTPLinearColorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_value_rgba_metadata_hdr_and_invalid_arguments_share_one_connection(self):
        handle, token = "obj-http-linear-color", "sig-83"
        colors = [
            {"R": 0.25, "G": 0.5, "B": 0.75, "A": 1.0},
            {"R": -0.5, "G": 2.0, "B": 8.25, "A": 1.5},
        ]
        metadata = {
            "struct_path": "/Script/CoreUObject.LinearColor", "struct_name": "LinearColor",
            "properties_size": 16, "min_alignment": 4,
            "components": [
                {"name": name, "type": "FloatProperty", "element_size": 4,
                 "offset": offset, "array_dim": 1}
                for name, offset in zip(("R", "G", "B", "A"), (0, 4, 8, 12))
            ],
        }
        reflection = {"items": [{
            "name": "EchoColor", "signature_token": token, "signature_status": "complete",
            "parameters": [
                {"name": "Input", "type": "StructProperty", "offset": 0,
                 "element_size": 16, "array_dim": 1, "flags": 0x80,
                 "return": False, "out": False, "reference": False, "const": False,
                 "direction": "input", "passing": "value", "struct": metadata},
                {"name": "ReturnValue", "type": "StructProperty", "offset": 16,
                 "element_size": 16, "array_dim": 1, "flags": 0x480,
                 "return": True, "out": True, "reference": False, "const": False,
                 "direction": "return", "passing": "value", "struct": metadata},
            ],
        }]}
        results = [{
            "function": "EchoColor", "return_type": "StructProperty", "result": color,
            "result_metadata": metadata, "executed": True, "thread_id": 456,
        } for color in colors]
        malformed_colors = [
            {"R": 1e39, "G": 0.5, "B": 0.75, "A": 1.0},
            {"r": 0.25, "G": 0.5, "B": 0.75, "A": 1.0},
            {"R": True, "G": 0.5, "B": 0.75, "A": 1.0},
        ]
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 123}

        def invoke_parameters(color):
            return {"handle": handle, "function": "EchoColor", "arguments": {"Input": color},
                    "expected_signature_token": token}

        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50,
            "function_query": "EchoColor"}, "result": reflection}]
        for color, result in zip(colors, results):
            steps.append({"op": "invoke", "params": invoke_parameters(color), "result": result})
        for color in malformed_colors:
            steps.append({"op": "invoke", "params": invoke_parameters(color),
                "error": {"code": "invalid_argument", "message": "RGBA отвергнут до выполнения",
                          "details": no_effect}})
        steps.append({"op": "status", "params": {}, "result": status})

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
                host="127.0.0.1", streamable_http_path="/mcp", stateless_http=False,
                json_response=True, transport_security=TransportSecuritySettings(
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
                answer, _ = await rpc(client, "tools/call", {
                    "name": name, "arguments": parameters}, session)
                return answer

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "color-listing-client")
                    second_session = await initialize(second, "color-invoke-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "g1r_object_functions", {
                        "handle": handle, "function_query": "EchoColor"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflection)
                    reflected_function = listed["structuredContent"]["items"][0]
                    for parameter in reflected_function["parameters"]:
                        self.assertEqual(parameter["struct"], metadata)
                        self.assertEqual(parameter["element_size"], 16)
                        self.assertEqual(parameter["array_dim"], 1)
                        self.assertEqual(parameter["passing"], "value")
                        self.assertIs(parameter["const"], False)
                        self.assertIs(parameter["reference"], False)
                    self.assertEqual(reflected_function["parameters"][0]["direction"], "input")
                    self.assertIs(reflected_function["parameters"][0]["out"], False)
                    self.assertEqual(reflected_function["parameters"][1]["direction"], "return")
                    shared_token = reflected_function["signature_token"]
                    for color, expected_result in zip(colors, results):
                        parameters = invoke_parameters(color)
                        parameters["expected_signature_token"] = shared_token
                        invoked = await call(second, second_session, "g1r_invoke", parameters)
                        self.assertFalse(invoked.get("isError", False), invoked)
                        self.assertEqual(invoked["structuredContent"], expected_result)
                        self.assertEqual(invoked["structuredContent"]["result"], color)
                        self.assertEqual(set(invoked["structuredContent"]["result"]), {"R", "G", "B", "A"})
                        self.assertEqual(invoked["structuredContent"]["result_metadata"], metadata)
                        self.assertIs(invoked["structuredContent"]["executed"], True)
                    for index, color in enumerate(malformed_colors, 3):
                        denied = await call(first, first_session, "g1r_invoke", invoke_parameters(color))
                        self.assertTrue(denied["isError"])
                        message = next(block["text"] for block in denied["content"]
                                       if block["type"] == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[index]["id"])
                    checked = await call(second, second_session, "g1r_native_status", {})
                    self.assertFalse(checked.get("isError", False), checked)
                    self.assertEqual(checked["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests],
                             ["functions", "invoke", "invoke", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 7)
            self.assertEqual([request["params"]["arguments"]["Input"]
                              for request in native.requests[1:6]], colors + malformed_colors)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
