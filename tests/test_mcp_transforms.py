"""FTransform проходит MCP/TCP вложенным JSON без скрытой нормализации Quaternion."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPTransformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_nested_transform_constraints_exact_forwarding_and_rejections(self):
        handle = "obj-transform-probe"
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
        functions = {"items": [{"name": "AcceptTransform", "signature_token": "sig-50",
                     "signature_status": "supported", "parameter_count": 2, "parameter_size": 97,
                     "parameters": [{"name": "Input", "type": "StructProperty", "element_size": 96,
                                     "offset": 0, "array_dim": 1, "flags": 0x80, "return": False,
                                     "out": False, "reference": False, "const": False,
                                     "direction": "input", "passing": "value", "struct": transform,
                                     "argument_constraints": constraints},
                                    {"name": "ReturnValue", "type": "BoolProperty", "element_size": 1,
                                     "offset": 96, "array_dim": 1, "flags": 0x80 | 0x400, "return": True,
                                     "out": False, "reference": False, "const": False}]}], "truncated": False}
        input_transform = {"Rotation": {"X": 0.0, "Y": 0, "Z": 0.0, "W": 1.0000001},
                           "Translation": {"X": 1.25, "Y": -2.5, "Z": 3},
                           "Scale3D": {"X": 1, "Y": 2.0, "Z": 0.5}}
        accepted = {"function": "AcceptTransform", "return_type": "BoolProperty", "result": True,
                    "executed": True, "thread_id": 456}
        read_translation = {"function": "ReadTranslation", "return_type": "StructProperty",
                            "result": {"X": 1.25, "Y": -2.5, "Z": 3.0},
                            "result_metadata": vector, "executed": True, "thread_id": 456}
        malformed = copy.deepcopy(input_transform)
        del malformed["Rotation"]["W"]
        bad_norm = copy.deepcopy(input_transform)
        bad_norm["Rotation"] = {"X": 0, "Y": 0, "Z": 0, "W": 2}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 120}
        steps = [
            {"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "AcceptTransform"}, "result": functions},
            {"op": "invoke", "params": {"handle": handle, "function": "AcceptTransform",
             "arguments": {"Input": input_transform}, "expected_signature_token": "sig-50"}, "result": accepted},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadTranslation", "arguments": {}}, "result": read_translation},
        ]
        for invalid in (malformed, bad_norm):
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "AcceptTransform",
                          "arguments": {"Input": invalid}, "expected_signature_token": "sig-50"},
                          "error": {"code": "invalid_argument", "message": "Требуется нормализованный Quaternion и точный FTransform",
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
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    listed = await client.call_tool("sn2_object_functions", {"handle": handle, "function_query": "AcceptTransform"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, functions)
                    parameter = listed.structured_content["items"][0]["parameters"][0]
                    self.assertEqual(parameter["struct"], transform)
                    self.assertEqual(parameter["argument_constraints"], constraints)
                    self.assertEqual(parameter["argument_constraints"]["normalization"], "none")
                    token = listed.structured_content["items"][0]["signature_token"]
                    accepted_response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptTransform",
                        "arguments": {"Input": input_transform}, "expected_signature_token": token})
                    self.assertFalse(accepted_response.is_error, accepted_response.content)
                    self.assertEqual(accepted_response.structured_content, accepted)
                    received = native.requests[1]["params"]["arguments"]["Input"]
                    self.assertEqual(received, input_transform)
                    self.assertEqual(received["Rotation"]["W"], 1.0000001)
                    self.assertIs(type(received["Rotation"]["Y"]), int)
                    self.assertIs(type(received["Translation"]["Z"]), int)
                    self.assertIs(type(received["Scale3D"]["Y"]), float)
                    translated = await client.call_tool("sn2_invoke", {"handle": handle, "function": "ReadTranslation"})
                    self.assertFalse(translated.is_error, translated.content)
                    self.assertEqual(translated.structured_content, read_translation)
                    for offset, invalid in enumerate((malformed, bad_norm)):
                        denied = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptTransform",
                            "arguments": {"Input": invalid}, "expected_signature_token": token})
                        self.assertTrue(denied.is_error, denied.content)
                        message = next(block.text for block in denied.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[3 + offset]["id"])
                        self.assertEqual(native.requests[3 + offset]["params"]["arguments"]["Input"], invalid)
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions", "invoke", "invoke", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
