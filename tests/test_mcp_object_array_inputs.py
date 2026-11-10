"""Const object-array JSON сохраняет handles/порядок; fixture не доказывает игровой API или allocator."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPObjectArrayInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_const_pointer_array_metadata_order_boundaries_and_no_retry(self):
        handle = "obj-array-probe"
        element = {"handle": "obj-source"}
        arrays = [[], [None, element, element, None], [element] * 65]
        array_metadata = {"inner_type": "ObjectProperty", "inner_element_size": 8, "inner_array_dim": 1,
                          "inner_offset": 0, "property_class": {"path": "/Script/CoreUObject.Object", "name": "Object"}}
        constraints = {"max_elements": 2147483647, "element_form": "null_or_handle", "ownership": "engine_owned_pointer_copy"}
        functions = {"items": [{"name": "AcceptObjects", "signature_token": "sig-80", "signature_status": "supported",
                     "parameter_count": 2, "parameter_size": 20, "parameters": [
                         {"name": "Input", "type": "ArrayProperty", "element_size": 16, "array_dim": 1,
                          "offset": 0, "flags": 0x80 | 0x2 | 0x100, "return": False, "out": True,
                          "reference": False, "const": True, "direction": "input", "passing": "const_reference",
                          "array": array_metadata, "argument_constraints": constraints},
                         {"name": "ReturnValue", "type": "IntProperty", "element_size": 4, "array_dim": 1,
                          "offset": 16, "flags": 0x80 | 0x400, "return": True, "out": False,
                          "reference": False, "const": False}]}], "truncated": False}
        results = [{"function": "AcceptObjects", "return_type": "IntProperty", "result": len(array),
                    "executed": True, "thread_id": 456, "cleanup_confirmed": True,
                    "array_input_cleanup_confirmed": True} for array in arrays]
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 150}
        rejected = [(-1, "invalid_argument"), (None, "invalid_argument"),
                    ({"elements": [element]}, "invalid_argument"), ([element, "obj-source"], "invalid_object_argument"),
                    ([element, -1], "invalid_object_argument"), ([True], "invalid_object_argument"),
                    ([{"Handle": "obj-source"}], "invalid_object_argument"),
                    ([{"handle": "obj-source", "path": "ignored"}], "invalid_object_argument"),
                    ([{"handle": "obj-other-session"}], "stale_handle")]
        steps = [{"op": "functions", "params": {"handle": handle, "limit": 50, "function_query": "AcceptObjects"},
                  "result": functions}]
        for array, result in zip(arrays, results):
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "AcceptObjects",
                          "arguments": {"Input": array}, "expected_signature_token": "sig-80"}, "result": result})
        for invalid, code in rejected:
            steps.append({"op": "invoke", "params": {"handle": handle, "function": "AcceptObjects",
                          "arguments": {"Input": invalid}, "expected_signature_token": "sig-80"},
                          "error": {"code": code, "message": "Объектный массив не прошёл проверку",
                                    "details": no_effect}})
        steps.extend([
            {"op": "invoke", "params": {"handle": handle, "function": "AcceptObjectsAndText",
             "arguments": {"Input": [element], "Text": "Несовместимая строковая ветка"}},
             "error": {"code": "unsupported_signature", "message": "Смешанные FString и object-array параметры не поддержаны",
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
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    listed = await client.call_tool("sn2_object_functions", {"handle": handle, "function_query": "AcceptObjects"})
                    self.assertFalse(listed.is_error, listed.content)
                    self.assertEqual(listed.structured_content, functions)
                    declaration = listed.structured_content["items"][0]
                    parameter = declaration["parameters"][0]
                    self.assertEqual(parameter["array"], array_metadata)
                    self.assertEqual(parameter["argument_constraints"], constraints)
                    self.assertEqual(parameter["passing"], "const_reference")
                    token = declaration["signature_token"]
                    for index, array in enumerate(arrays):
                        response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptObjects",
                            "arguments": {"Input": array}, "expected_signature_token": token})
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, results[index])
                        self.assertIs(response.structured_content["cleanup_confirmed"], True)
                        self.assertIs(response.structured_content["array_input_cleanup_confirmed"], True)
                        self.assertEqual(native.requests[index + 1]["params"]["arguments"]["Input"], array)
                        self.assertEqual(response.structured_content["result"], len(array))
                    self.assertEqual(native.requests[2]["params"]["arguments"]["Input"], [None, element, element, None])
                    for offset, (invalid, code) in enumerate(rejected):
                        response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptObjects",
                            "arguments": {"Input": invalid}, "expected_signature_token": token})
                        self.assertTrue(response.is_error, response.content)
                        message = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], native.requests[4 + offset]["id"])
                        self.assertEqual(native.requests[4 + offset]["params"]["arguments"]["Input"], invalid)
                    response = await client.call_tool("sn2_invoke", {"handle": handle, "function": "AcceptObjectsAndText",
                        "arguments": {"Input": [element], "Text": "Несовместимая строковая ветка"}})
                    self.assertTrue(response.is_error, response.content)
                    message = next(block.text for block in response.content if block.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "unsupported_signature")
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[13]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["functions"] + ["invoke"] * 13 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
