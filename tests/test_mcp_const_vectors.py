"""Const-reference FVector сохраняет reflection и точные XYZ в одном MCP/TCP-сеансе."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPConstVectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_reference_metadata_xyz_and_boolean_results_without_retry(self):
        handle = "obj-const-vector"
        struct = {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector",
                  "properties_size": 24, "min_alignment": 8, "components": [
                      {"name": name, "type": "DoubleProperty", "element_size": 8, "offset": index * 8,
                       "array_dim": 1} for index, name in enumerate(("X", "Y", "Z"))]}
        flags = 0x80 | 0x2 | 0x100
        metadata = {"items": [{"name": "VectorIsZero", "parameter_count": 2, "parameter_size": 25,
                     "signature_token": "sig-40", "signature_status": "supported",
                     "parameters": [{"name": "A", "type": "StructProperty", "offset": 0,
                                     "element_size": 24, "array_dim": 1, "flags": flags,
                                     "return": False, "out": True, "reference": False, "const": True,
                                     "direction": "input", "passing": "const_reference", "struct": struct},
                                    {"name": "ReturnValue", "type": "BoolProperty", "offset": 24,
                                     "element_size": 1, "array_dim": 1, "flags": 0x80 | 0x400,
                                     "return": True, "out": False, "reference": False, "const": False}]}],
                    "truncated": False, "parameters_truncated": False}
        vectors = [{"X": 0.0, "Y": 0.0, "Z": 0.0}, {"X": 1.25, "Y": -2.5, "Z": 3.75}]
        values = [True, False]
        results = [{"function": "VectorIsZero", "return_type": "BoolProperty", "result": value,
                    "executed": True, "thread_id": 456} for value in values]
        malformed = {"X": 0.0, "Y": 0.0, "Z": 0.0, "out": True}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 111}
        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "VectorIsZero"},
                  "result": metadata}]
        for vector, result in zip(vectors, results):
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "VectorIsZero",
                          "arguments": {"A": vector}, "expected_signature_token": "sig-40"}, "result": result})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "VectorIsZero",
             "arguments": {"A": malformed}, "expected_signature_token": "sig-40"},
             "error": {"code": "invalid_argument", "message": "FVector требует только конечные X/Y/Z",
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
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    listed = await client.call_tool("g1r_object_functions", {"handle": handle, "function_query": "VectorIsZero"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, metadata)
                    declaration = listed.structured_content["items"][0]
                    parameter = declaration["parameters"][0]
                    self.assertEqual(parameter["passing"], "const_reference")
                    self.assertEqual(parameter["direction"], "input")
                    self.assertIs(parameter["out"], True)
                    self.assertIs(parameter["reference"], False)
                    self.assertIs(parameter["const"], True)
                    self.assertEqual(parameter["flags"], flags)
                    token = declaration["signature_token"]
                    for index, vector in enumerate(vectors):
                        response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "VectorIsZero",
                            "arguments": {"A": vector}, "expected_signature_token": token})
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, results[index])
                        self.assertIs(response.structured_content["result"], values[index])
                        self.assertIs(response.structured_content["executed"], True)
                        self.assertEqual(native.requests[index + 1]["params"]["arguments"]["A"], vector)
                    denied = await client.call_tool("g1r_invoke", {"handle": handle, "function": "VectorIsZero",
                        "arguments": {"A": malformed}, "expected_signature_token": token})
                    self.assertTrue(denied.is_error, denied.content)
                    message = next(block.text for block in denied.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[3]["id"])
                    checked = await client.call_tool("g1r_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
