"""Скалярные pure-out значения сохраняют типы и эффект вызова в MCP/TCP."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPScalarOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_all_scalar_outputs_types_metadata_and_effects_without_retry(self):
        handle = "obj-scalar-out"
        declarations = {
            "Enabled": ("BoolProperty", 1, True),
            "Count": ("IntProperty", 4, -(1 << 31)),
            "LongCount": ("Int64Property", 8, -(1 << 63)),
            "UnsignedCount": ("UInt32Property", 4, (1 << 32) - 1),
            "UnsignedLong": ("UInt64Property", 8, (1 << 64) - 1),
            "Ratio": ("FloatProperty", 4, 1.25),
            "PreciseRatio": ("DoubleProperty", 8, -2.5),
        }
        arguments = {name: {"out": True} for name in declarations}
        values = {name: value for name, (_, _, value) in declarations.items()}
        metadata = {name: {"type": kind, "element_size": size, "array_dim": 1}
                    for name, (kind, size, _) in declarations.items()}
        metadata["Enabled"]["bool_layout"] = {"field_size": 1, "byte_offset": 0,
                                               "byte_mask": 1, "field_mask": 1}
        result = {"function": "WriteScalars", "return_type": "BoolProperty", "result": False,
                  "out_parameters": values, "out_parameter_metadata": metadata,
                  "executed": True, "thread_id": 456}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        unknown_effect = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                          "cause_code": "invalid_scalar_value"}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 99}
        invalid_markers = [False, {"out": False}, {"out": 1}, {"out": True, "value": 0}]
        steps = [{"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
                  "arguments": arguments, "expected_signature_token": "sig-30"}, "result": result}]
        rejected_arguments = []
        for marker in invalid_markers:
            rejected = copy.deepcopy(arguments)
            rejected["LongCount"] = marker
            rejected_arguments.append(rejected)
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
                          "arguments": rejected, "expected_signature_token": "sig-30"},
                          "error": {"code": "invalid_argument", "message": "Pure-out требует только {out:true}",
                                    "details": no_effect}})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "WriteScalarRef",
             "arguments": {"Output": {"out": True}}, "expected_signature_token": "sig-31"},
             "error": {"code": "unsupported_signature", "message": "Mutable ref/inout пока не поддержан",
                       "details": no_effect}},
            {"op": "invoke", "params": {"handle": handle, "function": "WriteScalars",
             "arguments": arguments, "expected_signature_token": "sig-30"},
             "error": {"code": "game_result_error", "message": "Вызов выполнен, но скалярный результат недействителен",
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
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    response = await client.call_tool("g1r_invoke", {"handle": handle, "function": "WriteScalars",
                        "arguments": arguments, "expected_signature_token": "sig-30"})
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, result)
                    self.assertIs(response.structured_content["result"], False)
                    self.assertIs(response.structured_content["executed"], True)
                    self.assertEqual(native.requests[0]["params"]["arguments"], arguments)
                    for name, (_, _, value) in declarations.items():
                        self.assertIs(type(response.structured_content["out_parameters"][name]), type(value))
                        self.assertEqual(response.structured_content["out_parameters"][name], value)
                    self.assertEqual(response.structured_content["out_parameter_metadata"]["Enabled"]["bool_layout"],
                                     metadata["Enabled"]["bool_layout"])
                    for offset, rejected in enumerate(rejected_arguments):
                        failed = await client.call_tool("g1r_invoke", {"handle": handle, "function": "WriteScalars",
                            "arguments": rejected, "expected_signature_token": "sig-30"})
                        self.assertTrue(failed.is_error, failed.content)
                        message = next(block.text for block in failed.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[1 + offset]["id"])
                    failed = await client.call_tool("g1r_invoke", {"handle": handle, "function": "WriteScalarRef",
                        "arguments": {"Output": {"out": True}}, "expected_signature_token": "sig-31"})
                    self.assertTrue(failed.is_error, failed.content)
                    message = next(block.text for block in failed.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "unsupported_signature")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["request_id"], native.requests[5]["id"])
                    failed = await client.call_tool("g1r_invoke", {"handle": handle, "function": "WriteScalars",
                        "arguments": arguments, "expected_signature_token": "sig-30"})
                    self.assertTrue(failed.is_error, failed.content)
                    message = next(block.text for block in failed.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "game_result_error")
                    self.assertEqual(error["effect"], unknown_effect)
                    self.assertIs(error["may_have_executed"], True)
                    self.assertEqual(error["request_id"], native.requests[6]["id"])
                    checked = await client.call_tool("g1r_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke"] * 7 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
