"""Const-массивы UObject проходят общий HTTP с сохранением формы и очистки."""

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


class HTTPObjectArrayInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_array_metadata_cleanup_and_no_effect_errors_share_one_connection(self):
        handle, peer, token = "obj-http-array-input", "obj-http-array-peer", "sig-82"
        values = [{"handle": handle}, None, {"handle": handle}, {"handle": peer}] + [{"handle": peer}] * 61
        arguments = {"Input": values, "Count": {"out": True}}
        array_metadata = {
            "inner_type": "ObjectProperty", "inner_element_size": 8,
            "inner_array_dim": 1, "inner_offset": 0,
            "property_class": {"path": "/Script/Fixture.Fixture", "name": "Fixture"},
        }
        constraints = {
            "max_elements": 2147483647, "element_form": "null_or_handle",
            "ownership": "engine_owned_pointer_copy",
        }
        reflection = {"items": [{
            "name": "CheckObjects", "signature_token": token,
            "signature_status": "complete", "parameters": [
                {"name": "Input", "type": "ArrayProperty", "offset": 0,
                 "element_size": 16, "array_dim": 1, "flags": 0x182,
                 "return": False, "out": True, "reference": False, "const": True,
                 "direction": "input", "passing": "const_reference",
                 "array": array_metadata, "argument_constraints": constraints},
                {"name": "Count", "type": "IntProperty", "offset": 16,
                 "element_size": 4, "array_dim": 1, "flags": 0x180,
                 "return": False, "out": True, "reference": False, "const": False,
                 "direction": "output", "argument_marker": {"out": True}},
                {"name": "ReturnValue", "type": "BoolProperty", "offset": 20,
                 "element_size": 1, "array_dim": 1, "flags": 0x480,
                 "return": True, "out": True, "reference": False, "const": False,
                 "direction": "return"},
            ],
        }]}
        result = {
            "function": "CheckObjects", "return_type": "BoolProperty", "result": True,
            "out_parameters": {"Count": len(values)},
            "out_parameter_metadata": {
                "Count": {"type": "IntProperty", "element_size": 4, "array_dim": 1},
            },
            "executed": True, "thread_id": 456,
            "cleanup_confirmed": True, "array_input_cleanup_confirmed": True,
        }
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 122}
        refused = [
            ("CheckObjects", {"Input": {"elements": [None]}, "Count": {"out": True}},
             token, "invalid_argument"),
            ("CheckObjects", {"Input": [{"handle": handle}, "raw-string"],
                              "Count": {"out": True}}, token, "invalid_object_argument"),
            ("CheckObjectsAndText", {"Input": values, "Text": "Готика"},
             None, "unsupported_signature"),
        ]
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                "function_query": "CheckObjects"}, "result": reflection},
            {"op": "invoke", "params": {"handle": handle, "function": "CheckObjects",
                "arguments": arguments, "expected_signature_token": token}, "result": result},
        ]
        for function, malformed, expected_token, code in refused:
            parameters = {"handle": handle, "function": function, "arguments": malformed}
            if expected_token is not None:
                parameters["expected_signature_token"] = expected_token
            steps.append({"op": "invoke", "params": parameters,
                "error": {"code": code, "message": "Вход отвергнут до выполнения",
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
                    first_session = await initialize(first, "array-listing-client")
                    second_session = await initialize(second, "array-invoke-client")
                    self.assertNotEqual(first_session, second_session)
                    listed = await call(first, first_session, "g1r_object_functions", {
                        "handle": handle, "function_query": "CheckObjects"})
                    self.assertFalse(listed.get("isError", False), listed)
                    self.assertEqual(listed["structuredContent"], reflection)
                    parameter = listed["structuredContent"]["items"][0]["parameters"][0]
                    self.assertEqual(parameter["array"], array_metadata)
                    self.assertEqual(parameter["argument_constraints"], constraints)
                    self.assertEqual(parameter["direction"], "input")
                    self.assertEqual(parameter["passing"], "const_reference")
                    self.assertIs(parameter["const"], True)
                    shared_token = listed["structuredContent"]["items"][0]["signature_token"]
                    invoked = await call(second, second_session, "g1r_invoke", {
                        "handle": handle, "function": "CheckObjects", "arguments": arguments,
                        "expected_signature_token": shared_token})
                    self.assertFalse(invoked.get("isError", False), invoked)
                    self.assertEqual(invoked["structuredContent"], result)
                    self.assertIs(invoked["structuredContent"]["result"], True)
                    self.assertEqual(invoked["structuredContent"]["out_parameters"]["Count"], 65)
                    self.assertIs(invoked["structuredContent"]["cleanup_confirmed"], True)
                    self.assertIs(invoked["structuredContent"]["array_input_cleanup_confirmed"], True)
                    for index, (function, malformed, expected_token, code) in enumerate(refused, 2):
                        parameters = {"handle": handle, "function": function, "arguments": malformed}
                        if expected_token is not None:
                            parameters["expected_signature_token"] = shared_token
                        denied = await call(first, first_session, "g1r_invoke", parameters)
                        self.assertTrue(denied["isError"])
                        message = next(block["text"] for block in denied["content"]
                                       if block["type"] == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[index]["id"])
                    checked = await call(second, second_session, "g1r_native_status", {})
                    self.assertFalse(checked.get("isError", False), checked)
                    self.assertEqual(checked["structuredContent"], status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests],
                             ["functions", "invoke", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 6)
            transmitted = native.requests[1]["params"]["arguments"]["Input"]
            self.assertEqual(len(transmitted), 65)
            self.assertEqual(transmitted, values)
            self.assertIsNone(transmitted[1])
            self.assertEqual(transmitted[0], transmitted[2])
            self.assertEqual(transmitted[3], {"handle": peer})
            self.assertEqual(native.requests[2]["params"]["arguments"]["Input"], {"elements": [None]})
            self.assertEqual(native.requests[3]["params"]["arguments"]["Input"][1], "raw-string")
            self.assertEqual(native.requests[4]["params"]["arguments"]["Text"], "Готика")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
