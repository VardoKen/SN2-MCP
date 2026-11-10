"""Token сигнатуры и выборка свойств проходят MCP без изменения прежних запросов."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPFunctionContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_large_selected_property_names_and_limits_reach_native_unchanged(self):
        handle = "obj-large-selection"
        names = [f"Property{index:02}" for index in range(31)] + ["A" * 257, "Я" * 129]
        self.assertEqual(len(names), 33)
        self.assertGreater(len(names[-1].encode("utf-8")), 256)
        selected = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                    "partial": False, "truncated": False, "items": [
                        {"name": name, "type": "StrProperty", "supported": True, "value": f"value-{index}"}
                        for index, name in enumerate(names)]}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 7}
        limits = (201, 2**64 - 1)
        steps = [{"op": "properties", "params": {"handle": handle, "limit": limit, "property_names": names},
                  "result": selected} for limit in limits]
        steps.append({"op": "status", "params": {}, "result": status})
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            source = root / "Modkit" / "Script" / "Fixture.as"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"class UFixture {}\n")
            original = source.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(source.parent.parent),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tool = next(tool for tool in (await client.list_tools()).tools if tool.name == "g1r_object_properties")
                    schema = tool.input_schema["properties"]
                    self.assertEqual(schema["limit"]["default"], 50)
                    names_schema = schema["property_names"]["anyOf"]
                    self.assertEqual({option["type"] for option in names_schema}, {"array", "null"})
                    self.assertEqual(next(option for option in names_schema if option["type"] == "array")["items"]["type"], "string")
                    self.assertTrue(tool.annotations.read_only_hint)
                    for limit in limits:
                        result = await client.call_tool("g1r_object_properties", {
                            "handle": handle, "limit": limit, "property_names": names})
                        self.assertFalse(result.is_error, result.content)
                        self.assertEqual(result.structured_content, selected)
                    following = await client.call_tool("g1r_native_status", {})
                    self.assertFalse(following.is_error, following.content)
                    self.assertEqual(following.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["properties", "properties", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 3)
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_optional_contracts_keep_legacy_payload_errors_and_connection(self):
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        handle = "obj-contract"
        old_properties = {"source": "live_native", "session_id": "fixture-session-1",
                          "handle": handle, "partial": False, "items": [
                              {"name": "StoredEntry", "type": "NameProperty", "supported": True, "value": "Entry"}]}
        signature = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                     "functions": [{"name": "EchoName", "params_size": 16, "num_params": 2,
                                    "signature_token": "sig-17", "signature_status": "supported",
                                    "parameters": [{"name": "Input", "type": "NameProperty", "offset": 0,
                                                    "element_size": 8, "array_dim": 1, "out": False,
                                                    "reference": False, "return": False},
                                                   {"name": "ReturnValue", "type": "NameProperty", "offset": 8,
                                                    "element_size": 8, "array_dim": 1, "out": True,
                                                    "reference": False, "return": True}]}]}
        old_return = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                      "function": "ReadMarker", "return": 71}
        arguments = {"Input": {"name_from": {"handle": "obj-source", "property_name": "StoredEntry"}}}
        echoed = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                  "function": "EchoName", "return": "Entry", "signature_token": "sig-17"}
        names = ["StoredEntry", "Missing", "StoredNone"]
        selected = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                    "partial": True, "truncated": False, "items": [
                        {"name": "StoredEntry", "type": "NameProperty", "supported": True, "value": "Entry"},
                        {"name": "Missing", "type": None, "supported": False,
                         "error": {"code": "property_not_found", "message": "Свойство не найдено"}},
                        {"name": "StoredNone", "type": "NameProperty", "supported": True, "value": "None"}]}
        status = {"version": "1.0.0-dev.13", "ready": True, "session_id": "fixture-session-1", "tick_count": 50}
        steps = [
            {"op": "properties", "params": {"handle": handle, "limit": 50}, "result": old_properties},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadMarker", "arguments": {}}, "result": old_return},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadMarker", "arguments": {}}, "result": old_return},
            {"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "EchoName"}, "result": signature},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoName", "arguments": arguments,
             "expected_signature_token": "sig-17"}, "result": echoed},
            {"op": "properties", "params": {"handle": handle, "limit": 3, "property_names": names}, "result": selected},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoName", "arguments": arguments,
             "expected_signature_token": "sig-17"},
             "error": {"code": "signature_changed", "message": "Сигнатура изменилась после чтения", "details": no_effect}},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoName", "arguments": arguments,
             "expected_signature_token": ""},
             "error": {"code": "invalid_signature_token", "message": "Некорректный token сигнатуры", "details": no_effect}},
            {"op": "properties", "params": {"handle": handle, "limit": 50, "property_names": []},
             "error": {"code": "invalid_property_names", "message": "Нужно от 1 до 32 уникальных имён", "details": no_effect}},
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
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    properties_tool = tools["g1r_object_properties"]
                    invoke_tool = tools["g1r_invoke"]
                    self.assertTrue(properties_tool.annotations.read_only_hint)
                    self.assertFalse(invoke_tool.annotations.read_only_hint)
                    self.assertTrue(invoke_tool.annotations.destructive_hint)
                    self.assertFalse(invoke_tool.annotations.idempotent_hint)
                    self.assertNotIn("property_names", properties_tool.input_schema.get("required", []))
                    self.assertNotIn("expected_signature_token", invoke_tool.input_schema.get("required", []))
                    token_schema = invoke_tool.input_schema["properties"]["expected_signature_token"]
                    self.assertEqual({option["type"] for option in token_schema["anyOf"]}, {"string", "null"})

                    async def successful(tool, params, expected):
                        response = await client.call_tool(tool, params)
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, expected)
                        return response.structured_content

                    await successful("g1r_object_properties", {"handle": handle}, old_properties)
                    await successful("g1r_invoke", {"handle": handle, "function": "ReadMarker"}, old_return)
                    await successful("g1r_invoke", {"handle": handle, "function": "ReadMarker",
                                     "arguments": None, "expected_signature_token": None}, old_return)
                    read = await successful("g1r_object_functions", {"handle": handle, "function_query": "EchoName"}, signature)
                    token = read["functions"][0]["signature_token"]
                    await successful("g1r_invoke", {"handle": handle, "function": "EchoName", "arguments": arguments,
                                     "expected_signature_token": token}, echoed)
                    selected_response = await successful("g1r_object_properties", {"handle": handle, "limit": 3, "property_names": names}, selected)
                    self.assertEqual([item["name"] for item in selected_response["items"]], names)
                    self.assertNotIn("properties", selected_response)

                    # Неверные типы остаются в schema-validation и не достигают native.
                    before = len(native.requests)
                    for tool, params in (
                        ("g1r_invoke", {"handle": handle, "function": "EchoName", "expected_signature_token": True}),
                        ("g1r_invoke", {"handle": handle, "function": "EchoName", "expected_signature_token": 17}),
                        ("g1r_object_properties", {"handle": handle, "property_names": ["StoredEntry", 42]}),
                        ("g1r_object_properties", {"handle": handle, "property_names": "StoredEntry"}),
                    ):
                        response = await client.call_tool(tool, params)
                        self.assertTrue(response.is_error, response.content)
                    self.assertEqual(len(native.requests), before)

                    for index, tool, params, code in (
                        (6, "g1r_invoke", {"handle": handle, "function": "EchoName", "arguments": arguments,
                                          "expected_signature_token": token}, "signature_changed"),
                        (7, "g1r_invoke", {"handle": handle, "function": "EchoName", "arguments": arguments,
                                          "expected_signature_token": ""}, "invalid_signature_token"),
                        (8, "g1r_object_properties", {"handle": handle, "property_names": []}, "invalid_property_names"),
                    ):
                        response = await client.call_tool(tool, params)
                        self.assertTrue(response.is_error, response.content)
                        text = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(text[text.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["effect"], no_effect)
                        self.assertEqual(error["request_id"], native.requests[index]["id"])
                    await successful("g1r_native_status", {}, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], [step["op"] for step in steps])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
