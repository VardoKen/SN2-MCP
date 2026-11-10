"""Unicode FString проходит настоящий MCP/TCP; fixture не доказывает игровой API или allocator."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPStringInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_value_string_unicode_empty_cleanup_errors_and_no_retry(self):
        handle = "obj-string-probe"
        constraints = {"max_utf16_units": 2147483646, "embedded_nul": "rejected", "ownership": "engine_owned_copy"}
        functions = {"items": [{"name": "EchoString", "signature_token": "sig-70", "signature_status": "supported",
                     "parameter_count": 2, "parameter_size": 32, "parameters": [
                         {"name": "Input", "type": "StrProperty", "element_size": 16, "array_dim": 1,
                "offset": 0, "flags": 0x80, "return": False, "out": False,
                "reference": False, "const": False, "direction": "input", "passing": "value",
                          "argument_constraints": constraints},
                         {"name": "ReturnValue", "type": "StrProperty", "element_size": 16, "array_dim": 1,
                          "offset": 16, "flags": 0x80 | 0x400, "return": True, "out": False,
                          "reference": False, "const": False}]}], "truncated": False}
        strings = ["", "Привет, мир — Ёж", "Мод 🐉🙂", "🙂" * 32768]
        results = [{"function": "EchoString", "return_type": "StrProperty", "result": value,
                    "executed": True, "thread_id": 456, "cleanup_confirmed": True,
                    "input_cleanup_confirmed": True} for value in strings]
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        unclear_cleanup = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                           "cleanup_attempted": True, "cleanup_confirmed": False, "input_cleanup_confirmed": False,
                           "cleanup_state": "unknown", "cause_code": "string_cleanup_fault"}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 140}
        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "EchoString"},
                  "result": functions}]
        for value, result in zip(strings, results):
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "EchoString",
                          "arguments": {"Input": value}, "expected_signature_token": "sig-70"}, "result": result})
        rejected = [("до\x00после", "invalid_argument"), ({"text": "строка"}, "invalid_argument")]
        for value, code in rejected:
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "EchoString",
                          "arguments": {"Input": value}, "expected_signature_token": "sig-70"},
                          "error": {"code": code, "message": "FString input имеет недопустимую форму",
                                    "details": no_effect}})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "EchoString",
             "arguments": {"Input": "Ошибка очистки 🐉"}, "expected_signature_token": "sig-70"},
             "error": {"code": "game_result_error", "message": "Вызов выполнен; освобождение FString не подтверждено",
                       "details": unclear_cleanup}},
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
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    invoke_schema = tools["g1r_invoke"].input_schema
                    self.assertEqual(set(invoke_schema["properties"]),
                                     {"handle", "function", "arguments", "expected_signature_token", "timeout_seconds"})
                    self.assertEqual(set(invoke_schema["required"]), {"handle", "function"})
                    self.assertEqual(invoke_schema["properties"]["timeout_seconds"], {
                        "anyOf": [{"type": "number"}, {"type": "null"}],
                        "default": None, "title": "Timeout Seconds"})
                    self.assertEqual(invoke_schema["properties"]["arguments"], {
                        "anyOf": [{"additionalProperties": True, "type": "object"}, {"type": "null"}],
                        "default": None, "title": "Arguments"})
                    capabilities = await client.call_tool("g1r_capabilities", {})
                    self.assertFalse(capabilities.is_error, capabilities.content)
                    string_input = capabilities.structured_content["invoke_arguments"]["string"]
                    self.assertEqual(string_input["input_shape"], "string")
                    self.assertEqual(string_input["passing"], "value_or_const_reference")
                    self.assertFalse(string_input["const_reference_required"])
                    self.assertTrue(string_input["by_value_input_supported"])
                    self.assertEqual(string_input["runtime_passing"], "normalized_const_reference")
                    self.assertEqual(string_input["runtime_passing_source"],
                                     "current_unreal_angelscript_function_builder")
                    listed = await client.call_tool("g1r_object_functions", {"handle": handle, "function_query": "EchoString"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, functions)
                    declaration = listed.structured_content["items"][0]
                    parameter = declaration["parameters"][0]
                    self.assertEqual(parameter["passing"], "value")
                    self.assertFalse(parameter["const"])
                    self.assertFalse(parameter["out"])
                    self.assertEqual(parameter["flags"], 0x80)
                    self.assertEqual(parameter["direction"], "input")
                    self.assertEqual(parameter["argument_constraints"], constraints)
                    token = declaration["signature_token"]
                    for index, value in enumerate(strings):
                        response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoString",
                            "arguments": {"Input": value}, "expected_signature_token": token})
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, results[index])
                        self.assertIs(type(response.structured_content["result"]), str)
                        self.assertEqual(response.structured_content["result"], value)
                        self.assertIs(response.structured_content["cleanup_confirmed"], True)
                        self.assertIs(response.structured_content["input_cleanup_confirmed"], True)
                        received = native.requests[index + 1]["params"]["arguments"]["Input"]
                        self.assertEqual(received, value)
                        self.assertIs(type(received), str)
                        self.assertEqual(received.encode("utf-16-le"), value.encode("utf-16-le"))
                    self.assertEqual(len(strings[-1].encode("utf-16-le")) // 2, 65536)
                    for offset, (value, code) in enumerate(rejected):
                        response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoString",
                            "arguments": {"Input": value}, "expected_signature_token": token})
                        self.assertTrue(response.is_error, response.content)
                        message = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[5 + offset]["id"])
                        self.assertEqual(native.requests[5 + offset]["params"]["arguments"]["Input"], value)
                    response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoString",
                        "arguments": {"Input": "Ошибка очистки 🐉"}, "expected_signature_token": token})
                    self.assertTrue(response.is_error, response.content)
                    message = next(block.text for block in response.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "game_result_error")
                    self.assertEqual(error["effect"], unclear_cleanup)
                    self.assertIs(error["may_have_executed"], True)
                    self.assertEqual(error["request_id"], native.requests[7]["id"])
                    checked = await client.call_tool("g1r_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions"] + ["invoke"] * 7 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
