"""FVector-аргументы, выходы и результаты проходят настоящий MCP/TCP без смены формы."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPVectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_vectors_outputs_and_known_or_unknown_effects_keep_connection(self):
        handle = "obj-vector-probe"
        point = {"X": 1.25, "Y": -2.5, "Z": 3.75}
        projected = {"X": 10.5, "Y": 20.25, "Z": -30.75}
        output_selector = {"out": True}
        metadata = {"struct_path": "/Script/ActualReflection.Vector", "struct_name": "Vector", "properties_size": 24,
                    "min_alignment": 8, "components": [
                        {"name": "X", "type": "DoubleProperty", "offset": 0, "element_size": 8, "array_dim": 1},
                        {"name": "Y", "type": "DoubleProperty", "offset": 8, "element_size": 8, "array_dim": 1},
                        {"name": "Z", "type": "DoubleProperty", "offset": 16, "element_size": 8, "array_dim": 1}]}
        returned = {"function": "EchoVector", "return_type": "StructProperty", "result": point,
                    "result_metadata": metadata, "executed": True, "thread_id": 456}
        written = {"function": "WriteVector", "return_type": "BoolProperty", "result": True,
                   "out_parameters": {"Output": projected}, "out_parameter_metadata": {"Output": metadata},
                   "executed": True, "thread_id": 456}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        unknown_effect = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                          "cause_code": "invalid_vector_result"}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 80}
        invalid_points = [{"x": 1.25, "y": -2.5, "z": 3.75},
                          {"X": 1.25, "Y": -2.5}, {**point, "W": 1},
                          {"X": True, "Y": -2.5, "Z": 3.75}]
        steps = [
            {"op": "invoke", "params": {"handle": handle, "function": "EchoVector", "arguments": {"Input": point},
             "expected_signature_token": "sig-20"}, "result": returned},
            {"op": "invoke", "params": {"handle": handle, "function": "WriteVector",
             "arguments": {"Input": point, "Output": output_selector}, "expected_signature_token": "sig-21"}, "result": written},
        ]
        for value in invalid_points:
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "EchoVector",
                          "arguments": {"Input": value}, "expected_signature_token": "sig-20"},
                          "error": {"code": "invalid_argument", "message": "FVector требует точные конечные X/Y/Z",
                                    "details": no_effect}})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "EchoVector",
             "arguments": {"Input": point}, "expected_signature_token": "sig-20"},
             "error": {"code": "game_result_error", "message": "Вызов выполнен, но результат FVector недействителен",
                       "details": unknown_effect}},
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
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "EchoVector",
                                                      "arguments": {"Input": point}, "expected_signature_token": "sig-20"})
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, returned)
                    self.assertEqual(response.structured_content["result"], point)
                    response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "WriteVector",
                        "arguments": {"Input": point, "Output": output_selector}, "expected_signature_token": "sig-21"})
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, written)
                    self.assertEqual(response.structured_content["out_parameters"]["Output"], projected)
                    self.assertEqual(native.requests[1]["params"]["arguments"], {"Input": point, "Output": output_selector})
                    for offset, value in enumerate(invalid_points):
                        response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "EchoVector",
                            "arguments": {"Input": value}, "expected_signature_token": "sig-20"})
                        self.assertTrue(response.is_error, response.content)
                        message = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[2 + offset]["id"])
                        self.assertEqual(native.requests[2 + offset]["params"]["arguments"]["Input"], value)
                    response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "EchoVector",
                        "arguments": {"Input": point}, "expected_signature_token": "sig-20"})
                    self.assertTrue(response.is_error, response.content)
                    message = next(block.text for block in response.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "game_result_error")
                    self.assertEqual(error["effect"], unknown_effect)
                    self.assertIs(error["may_have_executed"], True)
                    self.assertEqual(error["request_id"], native.requests[6]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke"] * 7 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
