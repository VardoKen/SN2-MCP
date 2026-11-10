"""Const-reference FTransform проходит общий HTTP как вложенный вход без out-selector."""
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


class HTTPConstTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_transform_metadata_and_nested_input_keep_shared_connection(self):
        handle, token = "obj-http-const-transform", "sig-61"
        rotation = {"X": 0, "Y": 0, "Z": 0, "W": 1.0000001}
        translation = {"X": 125, "Y": -2.5, "Z": 3.75}
        scale = {"X": 1, "Y": 0.5, "Z": -2}
        transform = {"Rotation": rotation, "Translation": translation, "Scale3D": scale}
        malformed = {**transform, "out": True}
        def structure(name, size, components):
            return {"struct_path": "/Script/CoreUObject." + name,
                    "struct_name": name, "properties_size": size,
                    "min_alignment": 16 if name == "Quat" else 8,
                    "components": [{"name": component, "type": "DoubleProperty", "element_size": 8,
                                    "offset": index * 8, "array_dim": 1}
                                   for index, component in enumerate(components)]}
        transform_metadata = {"struct_path": "/Script/CoreUObject.Transform", "struct_name": "Transform",
            "properties_size": 96, "min_alignment": 16, "members": [
                {"name": "Rotation", "type": "StructProperty", "element_size": 32, "offset": 0,
                 "array_dim": 1, "struct": structure("Quat", 32, ("X", "Y", "Z", "W"))},
                {"name": "Translation", "type": "StructProperty", "element_size": 24, "offset": 32,
                 "array_dim": 1, "struct": structure("Vector", 24, ("X", "Y", "Z"))},
                {"name": "Scale3D", "type": "StructProperty", "element_size": 24, "offset": 64,
                 "array_dim": 1, "struct": structure("Vector", 24, ("X", "Y", "Z"))}],
            "fixture_extra_metadata": {"opaque_layout_marker": 2 ** 63 + 17, "preserve": [False, None]}}
        reflected = {"items": [{"name": "ReadConstTransformProbe", "signature_token": token,
            "signature_status": "complete", "parameters": [
                {"name": "Input", "type": "StructProperty", "element_size": 96, "offset": 0,
                 "array_dim": 1, "flags": 0x182, "const": True, "reference": False, "out": True,
                 "return": False, "direction": "input", "passing": "const_reference", "struct": transform_metadata,
                 "argument_constraints": {"rotation_normalization": "required",
                    "max_norm_squared_deviation": 1e-6, "normalization": "none"}}]}]}
        returned = {"function": "ReadConstTransformProbe", "return_type": "BoolProperty", "result": True,
                    "executed": True, "thread_id": 456}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 119}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                "function_query": "ReadConstTransformProbe"}, "result": reflected},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadConstTransformProbe",
                "arguments": {"Input": transform}, "expected_signature_token": token}, "result": returned},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadConstTransformProbe",
                "arguments": {"Input": malformed}, "expected_signature_token": token},
             "error": {"code": "invalid_argument", "message": "Const Transform требует только Rotation, Translation, Scale3D",
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
                answer, _ = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                return answer

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first, "const-transform-listing-client")
                    second_session = await initialize(second, "const-transform-invoke-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "sn2_object_functions", {
                        "handle": handle, "function_query": "ReadConstTransformProbe"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflected)
                    parameter = listed["structuredContent"]["items"][0]["parameters"][0]
                    self.assertEqual(parameter["passing"], "const_reference")
                    self.assertEqual(parameter["direction"], "input")
                    self.assertEqual(parameter["flags"], 0x182)
                    self.assertIs(parameter["const"], True)
                    self.assertIs(parameter["out"], True)
                    self.assertIs(parameter["reference"], False)
                    self.assertEqual(parameter["struct"], transform_metadata)
                    self.assertEqual(parameter["struct"]["fixture_extra_metadata"]["opaque_layout_marker"], 2 ** 63 + 17)
                    self.assertEqual(parameter["argument_constraints"]["normalization"], "none")
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    answer = await call(second, second_session, "sn2_invoke", {
                        "handle": handle, "function": "ReadConstTransformProbe", "arguments": {"Input": transform},
                        "expected_signature_token": shared_token})
                    self.assertFalse(answer.get("isError", False), answer)
                    self.assertEqual(answer["structuredContent"], returned)
                    self.assertIs(type(native.requests[1]["params"]["arguments"]["Input"]["Translation"]["X"]), int)
                    self.assertIs(type(native.requests[1]["params"]["arguments"]["Input"]["Rotation"]["W"]), float)
                    self.assertEqual(native.requests[1]["params"]["arguments"]["Input"]["Rotation"]["W"], 1.0000001)
                    self.assertNotIn("out", native.requests[1]["params"]["arguments"]["Input"])
                    denied = await call(first, first_session, "sn2_invoke", {
                        "handle": handle, "function": "ReadConstTransformProbe", "arguments": {"Input": malformed},
                        "expected_signature_token": shared_token})
                    self.assertTrue(denied["isError"])
                    text = next(block["text"] for block in denied["content"] if block["type"] == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[2]["id"])
                    self.assertIs(native.requests[2]["params"]["arguments"]["Input"]["out"], True)
                    checked = await call(second, second_session, "sn2_native_status", {})
                    self.assertFalse(checked.get("isError", False), checked)
                    self.assertEqual(checked["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 4)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
