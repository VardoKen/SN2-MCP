"""Const-reference FTransform сохраняет JSON и metadata в MCP; fixture не доказывает игровой API."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPConstTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_transform_flags_nested_values_and_known_failure_keep_connection(self):
        handle = "obj-const-transform"
        vector = {"struct_path": "/Script/CoreUObject.Vector", "struct_name": "Vector",
                  "properties_size": 24, "min_alignment": 8, "components": [
                      {"name": name, "type": "DoubleProperty", "element_size": 8, "offset": index * 8,
                       "array_dim": 1} for index, name in enumerate(("X", "Y", "Z"))]}
        quat = {"struct_path": "/Script/CoreUObject.Quat", "struct_name": "Quat",
                "properties_size": 32, "min_alignment": 16, "components": [
                    {"name": name, "type": "DoubleProperty", "element_size": 8, "offset": index * 8,
                     "array_dim": 1} for index, name in enumerate(("X", "Y", "Z", "W"))]}
        transform = {"struct_path": "/Script/CoreUObject.Transform", "struct_name": "Transform",
                     "properties_size": 96, "min_alignment": 16, "members": [
                         {"name": "Rotation", "type": "StructProperty", "element_size": 32, "offset": 0,
                          "array_dim": 1, "struct": quat},
                         {"name": "Translation", "type": "StructProperty", "element_size": 24, "offset": 32,
                          "array_dim": 1, "struct": vector},
                         {"name": "Scale3D", "type": "StructProperty", "element_size": 24, "offset": 64,
                          "array_dim": 1, "struct": vector}]}
        constraints = {"rotation_normalization": "required", "max_norm_squared_deviation": 1e-6,
                       "normalization": "none"}
        flags = 0x80 | 0x2 | 0x100
        parameter = {"name": "Input", "type": "StructProperty", "element_size": 96, "offset": 0,
                     "array_dim": 1, "flags": flags, "return": False, "out": True,
                     "reference": False, "const": True, "direction": "input", "passing": "const_reference",
                     "struct": transform, "argument_constraints": constraints}
        referenced = copy.deepcopy(parameter)
        referenced.update(flags=0x80 | 0x2 | 0x08000000, out=False, reference=True)
        functions = {"items": [{"name": "AcceptConstTransform", "signature_token": "sig-60",
                     "signature_status": "supported", "parameters": [parameter]},
                    {"name": "TransformLocation", "signature_token": "sig-61", "signature_status": "supported",
                     "parameters": [referenced]}], "truncated": False}
        value = {"Rotation": {"X": 0, "Y": 0.0, "Z": 0.6, "W": 0.8},
                 "Translation": {"X": 1.25, "Y": -2.5, "Z": 3},
                 "Scale3D": {"X": 1, "Y": 2.0, "Z": 0.5}}
        accepted = {"function": "AcceptConstTransform", "return_type": "BoolProperty", "result": True,
                    "executed": True, "thread_id": 456}
        location = {"function": "TransformLocation", "return_type": "StructProperty",
                    "result": {"X": 1.25, "Y": -2.5, "Z": 3.0}, "result_metadata": vector,
                    "executed": True, "thread_id": 456}
        malformed = copy.deepcopy(value)
        malformed["Rotation"]["out"] = True
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 130}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "Transform"}, "result": functions},
            {"op": "invoke", "params": {"handle": handle, "function": "AcceptConstTransform",
             "arguments": {"Input": value}, "expected_signature_token": "sig-60"}, "result": accepted},
            {"op": "invoke", "params": {"handle": handle, "function": "TransformLocation",
             "arguments": {"Input": value}, "expected_signature_token": "sig-61"}, "result": location},
            {"op": "invoke", "params": {"handle": handle, "function": "AcceptConstTransform",
             "arguments": {"Input": malformed}, "expected_signature_token": "sig-60"},
             "error": {"code": "invalid_argument", "message": "Rotation требует только X/Y/Z/W",
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
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    listed = await client.call_tool("sn2_object_functions", {"handle": handle, "function_query": "Transform"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, functions)
                    declarations = listed.structured_content["items"]
                    for declaration in declarations:
                        field = declaration["parameters"][0]
                        self.assertEqual(field["direction"], "input")
                        self.assertEqual(field["passing"], "const_reference")
                        self.assertIs(field["const"], True)
                        self.assertEqual(field["argument_constraints"], constraints)
                    self.assertEqual(declarations[0]["parameters"][0]["flags"], flags)
                    self.assertIs(declarations[0]["parameters"][0]["out"], True)
                    self.assertIs(declarations[1]["parameters"][0]["reference"], True)
                    for index, expected in enumerate((accepted, location)):
                        declaration = declarations[index]
                        response = await client.call_tool("sn2_invoke", {"handle": handle,
                            "function": declaration["name"], "arguments": {"Input": value},
                            "expected_signature_token": declaration["signature_token"]})
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, expected)
                        received = native.requests[index + 1]["params"]["arguments"]["Input"]
                        self.assertEqual(received, value)
                        self.assertIs(type(received["Rotation"]["X"]), int)
                        self.assertIs(type(received["Translation"]["Z"]), int)
                        self.assertEqual(received["Rotation"]["Z"], 0.6)
                        self.assertEqual(received["Rotation"]["W"], 0.8)
                    denied = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptConstTransform",
                        "arguments": {"Input": malformed}, "expected_signature_token": declarations[0]["signature_token"]})
                    self.assertTrue(denied.is_error, denied.content)
                    message = next(block.text for block in denied.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "invalid_argument")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[3]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
