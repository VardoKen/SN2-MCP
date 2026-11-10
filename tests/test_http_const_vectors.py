"""Const-reference FVector с OutParm передаётся как XYZ-вход общей HTTP-сессии."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from sn2_mcp.native_client import NativeClient
from sn2_mcp.server import create_server
from tests.test_mcp_observations import SessionNativeFixture


class HTTPConstVectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_reference_flags_xyz_and_bool_results_survive_shared_http(self):
        handle, token = "obj-http-const-vector", "sig-40"
        flags = 0x80 | 0x2 | 0x100
        struct = {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector",
                  "properties_size": 24, "min_alignment": 8,
                  "components": [{"name": name, "type": "DoubleProperty", "element_size": 8,
                                  "offset": index * 8, "array_dim": 1}
                                 for index, name in enumerate(("X", "Y", "Z"))]}
        reflection = {"items": [{"name": "VectorIsZero", "signature_token": token,
            "signature_status": "supported", "parameter_count": 2, "parameter_size": 25,
            "parameters": [{"name": "A", "type": "StructProperty", "offset": 0,
                            "element_size": 24, "array_dim": 1, "flags": flags,
                            "out": True, "reference": False, "return": False, "const": True,
                            "direction": "input", "passing": "const_reference", "struct": struct},
                           {"name": "ReturnValue", "type": "BoolProperty", "offset": 24,
                            "element_size": 1, "array_dim": 1, "flags": 0x80 | 0x400,
                            "out": False, "reference": False, "return": True, "const": False}]}],
            "truncated": False, "parameters_truncated": False}
        vectors = [{"X": 0, "Y": 0, "Z": 0}, {"X": 1.25, "Y": -2.5, "Z": 3.75}]
        expected_values = [True, False]
        results = [{"function": "VectorIsZero", "return_type": "BoolProperty", "result": value,
                    "executed": True, "thread_id": 456} for value in expected_values]
        malformed = {"X": 0, "Y": 0, "Z": 0, "out": True}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 111}
        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50,
            "function_query": "VectorIsZero"}, "result": reflection}]
        for vector, result in zip(vectors, results):
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "VectorIsZero",
                "arguments": {"A": vector}, "expected_signature_token": token}, "result": result})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "VectorIsZero",
                "arguments": {"A": malformed}, "expected_signature_token": token},
             "error": {"code": "invalid_argument", "message": "Const FVector требует только конечные X/Y/Z",
                       "details": no_effect}},
            {"op": "status", "params": {}, "result": status},
        ])
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
            with patch("sn2_mcp.server.NativeClient", return_value=backend) as factory:
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

            async def call(client, session, name, arguments):
                answer, _ = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                return answer

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "const-vector-listing-client")
                    second_session = await initialize(second, "const-vector-invoke-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "sn2_object_functions", {
                        "handle": handle, "function_query": "VectorIsZero"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflection)
                    parameter = listed["structuredContent"]["items"][0]["parameters"][0]
                    self.assertIs(parameter["out"], True)
                    self.assertIs(parameter["reference"], False)
                    self.assertIs(parameter["const"], True)
                    self.assertEqual(parameter["passing"], "const_reference")
                    self.assertEqual(parameter["direction"], "input")
                    self.assertEqual(parameter["flags"], flags)
                    self.assertEqual(parameter["struct"], struct)
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    for index, vector in enumerate(vectors):
                        client, session = (second, second_session) if index == 0 else (first, first_session)
                        answer = await call(client, session, "sn2_invoke", {
                            "handle": handle, "function": "VectorIsZero", "arguments": {"A": vector},
                            "expected_signature_token": shared_token})
                        self.assertFalse(answer.get("isError", False), answer)
                        self.assertEqual(answer["structuredContent"], results[index])
                        self.assertIs(answer["structuredContent"]["result"], expected_values[index])
                        self.assertEqual(native.requests[index + 1]["params"]["arguments"]["A"], vector)
                    self.assertIs(type(native.requests[1]["params"]["arguments"]["A"]["X"]), int)
                    self.assertIs(type(native.requests[2]["params"]["arguments"]["A"]["X"]), float)
                    denied = await call(second, second_session, "sn2_invoke", {
                        "handle": handle, "function": "VectorIsZero", "arguments": {"A": malformed},
                        "expected_signature_token": shared_token})
                    self.assertTrue(denied["isError"])
                    text = next(block["text"] for block in denied["content"] if block["type"] == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[3]["id"])
                    after = await call(first, first_session, "sn2_native_status", {})
                    self.assertFalse(after.get("isError", False), after)
                    self.assertEqual(after["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 5)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
