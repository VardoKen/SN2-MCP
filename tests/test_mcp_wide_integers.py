"""Широкие целые проходят настоящий MCP/JSON/TCP без преобразования во float."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPWideIntegerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_exact_int64_uint64_boundaries_and_native_rejections_keep_connection(self):
        signed_min = -(1 << 63)
        signed_max = (1 << 63) - 1
        unsigned_max = (1 << 64) - 1
        above_exact_float = (1 << 53) + 1
        handle = "obj-integer-probe"
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        accepted = [("EchoInt64", signed_min), ("EchoInt64", signed_max),
                    ("EchoUInt64", unsigned_max), ("EchoInt64", above_exact_float),
                    ("EchoUInt32", (1 << 32) - 1)]
        rejected = [("EchoInt64", 1.25), ("EchoInt64", float(above_exact_float)),
                    ("EchoInt64", 1 << 63), ("EchoUInt64", -1), ("EchoUInt64", True)]
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 73}
        steps = []
        for function, value in accepted:
            result = {"source": "live_native", "session_id": "fixture-session-1", "handle": handle,
                      "function": function, "return": value}
            steps.append({"op": "invoke", "params": {"handle": handle, "function": function,
                          "arguments": {"Input": value}}, "result": result})
        for function, value in rejected:
            steps.append({"op": "invoke", "params": {"handle": handle, "function": function,
                          "arguments": {"Input": value}},
                          "error": {"code": "invalid_argument", "message": "Требуется целое в отражённом диапазоне",
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
                    for index, (function, value) in enumerate(accepted):
                        response = await client.call_tool("sn2_invoke", {"handle": handle, "function": function,
                                                          "arguments": {"Input": value}})
                        self.assertFalse(response.is_error, response.content)
                        self.assertIs(type(response.structured_content["return"]), int)
                        self.assertEqual(response.structured_content["return"], value)
                        received = native.requests[index]["params"]["arguments"]["Input"]
                        self.assertIs(type(received), int)
                        self.assertEqual(received, value)
                    for offset, (function, value) in enumerate(rejected):
                        response = await client.call_tool("sn2_invoke", {"handle": handle, "function": function,
                                                          "arguments": {"Input": value}})
                        self.assertTrue(response.is_error, response.content)
                        message = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(message[message.index("{"):])
                        request = native.requests[len(accepted) + offset]
                        self.assertIs(type(request["params"]["arguments"]["Input"]), type(value))
                        self.assertEqual(error["code"], "invalid_argument")
                        self.assertEqual(error["effect"], no_effect)
                        self.assertIs(error["may_have_executed"], False)
                        self.assertEqual(error["request_id"], request["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke"] * 10 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
