"""Вложенные FVector проходят общий HTTP без преобразования и новых прав владения."""
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


class HTTPVectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_nested_vectors_return_and_out_pass_between_shared_http_clients(self):
        handle, token = "obj-http-vector", "sig-23"
        point = {"X": 1.25, "Y": -2.5, "Z": 3.75}
        output = {"X": -4.125, "Y": 0.0, "Z": 8.5}
        parameters = {"Input": point, "Output": {"out": True}}
        reflected = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                     "items": [{"name": "ProjectProbe", "signature_token": token,
                                "signature_status": "complete", "parameters": [
                                    {"name": "Input", "type": "StructProperty", "out": False,
                                     "reference": False, "return": False},
                                    {"name": "Output", "type": "StructProperty", "out": True,
                                     "reference": True, "return": False}]}]}
        metadata = {"struct_path": "/Script/ActualReflection.Vector", "struct_name": "Vector",
                    "properties_size": 24, "min_alignment": 8,
                    "components": [{"name": name, "type": "DoubleProperty", "element_size": 8,
                                    "offset": index * 8, "array_dim": 1}
                                   for index, name in enumerate(("X", "Y", "Z"))]}
        returned = {"function": "ProjectProbe", "return_type": "StructProperty", "result": point,
                    "out_parameters": {"Output": output}, "executed": True, "thread_id": 456,
                    "result_metadata": metadata, "out_parameter_metadata": {"Output": metadata}}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"version": "fixture", "session_id": "fixture-session-1", "tick_count": 50}
        invalid_arguments = {"Input": {"X": True, "Y": -2.5, "Z": 3.75}, "Output": {"out": True}}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                                               "function_query": "ProjectProbe"}, "result": reflected},
            {"op": "invoke", "params": {"handle": handle, "function": "ProjectProbe",
                "arguments": parameters, "expected_signature_token": token}, "result": returned},
            {"op": "invoke", "params": {"handle": handle, "function": "ProjectProbe",
                "arguments": invalid_arguments, "expected_signature_token": token},
             "error": {"code": "invalid_argument", "message": "Компоненты FVector должны быть числами",
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
                result, _ = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                return result

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "vector-listing-client")
                    second_session = await initialize(second, "vector-calling-client")
                    self.assertNotEqual(first_session, second_session)
                    ownership = await call(first, first_session, "sn2_session_status", {})
                    self.assertFalse(ownership.get("isError", False), ownership)
                    self.assertFalse(ownership["structuredContent"]["claimed"])
                    listed = await call(first, first_session, "sn2_object_functions", {
                        "handle": handle, "function_query": "ProjectProbe"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflected)
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    result = await call(second, second_session, "sn2_invoke", {
                        "handle": handle, "function": "ProjectProbe", "arguments": parameters,
                        "expected_signature_token": shared_token})
                    self.assertFalse(result.get("isError", False), result)
                    self.assertEqual(result["structuredContent"], returned)
                    self.assertEqual(result["structuredContent"]["result"], point)
                    self.assertEqual(result["structuredContent"]["out_parameters"]["Output"], output)
                    self.assertEqual(native.connections, 1)
                    invalid = await call(first, first_session, "sn2_invoke", {
                        "handle": handle, "function": "ProjectProbe", "arguments": invalid_arguments,
                        "expected_signature_token": shared_token})
                    self.assertTrue(invalid["isError"])
                    payload = next(block["text"] for block in invalid["content"] if block["type"] == "text")
                    error = json.loads(payload[payload.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[2]["id"])
                    after = await call(second, second_session, "sn2_native_status", {})
                    self.assertFalse(after.get("isError", False), after)
                    self.assertEqual(after["structuredContent"], status)
                    lease = await call(second, second_session, "sn2_session_status", {})
                    self.assertFalse(lease["structuredContent"]["claimed"])
            self.assertEqual(native.connections, 1)
            self.assertEqual([item["op"] for item in native.requests], ["functions", "invoke", "invoke", "status"])
            self.assertEqual(len({item["id"] for item in native.requests}), 4)
            self.assertEqual(native.requests[1]["params"]["arguments"], parameters)
            self.assertIs(native.requests[2]["params"]["arguments"]["Input"]["X"], True)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
