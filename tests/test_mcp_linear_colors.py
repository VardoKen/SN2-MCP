"""FLinearColor проходит MCP/TCP RGBA без clamp/gamma; fixture не доказывает игровой API."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPLinearColorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_rgba_float_metadata_hdr_values_rejections_and_no_retry(self):
        handle = "obj-color-probe"
        metadata = {"struct_path": "/Script/CoreUObject.LinearColor", "struct_name": "LinearColor",
                    "properties_size": 16, "min_alignment": 4, "components": [
                        {"name": name, "type": "FloatProperty", "element_size": 4, "offset": index * 4,
                         "array_dim": 1} for index, name in enumerate(("R", "G", "B", "A"))]}
        functions = {"items": [{"name": "EchoColor", "signature_token": "sig-90", "signature_status": "supported",
                     "parameter_count": 2, "parameter_size": 32, "parameters": [
                         {"name": "Input", "type": "StructProperty", "element_size": 16, "array_dim": 1,
                          "offset": 0, "flags": 0x80, "return": False, "out": False, "reference": False,
                          "const": False, "direction": "input", "passing": "value", "struct": metadata},
                         {"name": "ReturnValue", "type": "StructProperty", "element_size": 16, "array_dim": 1,
                          "offset": 16, "flags": 0x80 | 0x400, "return": True, "out": False,
                          "reference": False, "const": False, "direction": "return", "passing": "value", "struct": metadata}]}],
                     "truncated": False}
        color = {"R": -0.5, "G": 2.0, "B": 8, "A": 1.25}
        result = {"function": "EchoColor", "return_type": "StructProperty",
                  "result": {"R": -0.5, "G": 2.0, "B": 8.0, "A": 1.25},
                  "result_metadata": metadata, "executed": True, "thread_id": 456}
        invalid = [{"R": -0.5, "G": 2, "B": 8}, {**color, "X": 0},
                   {"R": True, "G": 2, "B": 8, "A": 1}, {"R": 1e100, "G": 2, "B": 8, "A": 1}]
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        unknown = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                   "cause_code": "invalid_struct_value"}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 160}
        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "EchoColor"},
                  "result": functions},
                 {"op": "invoke", "params": {"handle": handle, "function": "EchoColor",
                  "arguments": {"Input": color}, "expected_signature_token": "sig-90"}, "result": result}]
        for value in invalid:
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "EchoColor",
                          "arguments": {"Input": value}, "expected_signature_token": "sig-90"},
                          "error": {"code": "invalid_argument", "message": "Требуется точный конечный Float32 RGBA",
                                    "details": no_effect}})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "EchoColor",
             "arguments": {"Input": color}, "expected_signature_token": "sig-90"},
             "error": {"code": "game_result_error", "message": "Вызов выполнен, но FLinearColor содержит недействительные компоненты",
                       "details": unknown}},
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
                    listed = await client.call_tool("g1r_object_functions", {"handle": handle, "function_query": "EchoColor"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, functions)
                    declaration = listed.structured_content["items"][0]
                    self.assertEqual(declaration["parameters"][0]["direction"], "input")
                    self.assertEqual(declaration["parameters"][1]["direction"], "return")
                    self.assertEqual(declaration["parameters"][0]["struct"], metadata)
                    token = declaration["signature_token"]
                    response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoColor",
                        "arguments": {"Input": color}, "expected_signature_token": token})
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, result)
                    self.assertEqual(native.requests[1]["params"]["arguments"]["Input"], color)
                    self.assertIs(type(native.requests[1]["params"]["arguments"]["Input"]["B"]), int)
                    self.assertEqual(response.structured_content["result"]["R"], -0.5)
                    self.assertEqual(response.structured_content["result"]["B"], 8.0)
                    for offset, value in enumerate(invalid):
                        response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoColor",
                            "arguments": {"Input": value}, "expected_signature_token": token})
                        self.assertTrue(response.is_error, response.content)
                        message = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[2 + offset]["id"])
                    response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "EchoColor",
                        "arguments": {"Input": color}, "expected_signature_token": token})
                    self.assertTrue(response.is_error, response.content)
                    message = next(block.text for block in response.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "game_result_error")
                    self.assertEqual(error["effect"], unknown)
                    self.assertIs(error["may_have_executed"], True)
                    self.assertEqual(error["request_id"], native.requests[6]["id"])
                    checked = await client.call_tool("g1r_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions"] + ["invoke"] * 6 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
