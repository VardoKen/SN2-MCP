"""Enum-результаты и сведения об отказе сохраняются через настоящий MCP stdio."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPEnumTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_numeric_returns_do_not_depend_on_labels_and_unknown_argument_is_not_replayed(self):
        params = {"handle": "obj-enum-probe", "function": "EchoMode", "arguments": {"Input": 7}}
        aliases = {"path": "/Script/Angelscript.EFixtureMode", "underlying_type": "ByteProperty",
                   "name_status": "matched", "value_name": "EFixtureMode::AliasOne",
                   "matched_count": 2, "ambiguous": True, "reason": None}
        unavailable = {"path": "/Script/Angelscript.EFixtureMode", "underlying_type": "ByteProperty",
                       "name_status": "unavailable", "value_name": None,
                       "matched_count": 0, "ambiguous": False, "reason": "invalid_enum_table"}
        def result(metadata):
            return {"function": "EchoMode", "return_type": "EnumProperty", "result": 7,
                    "return_enum": metadata, "executed": True, "thread_id": 456}
        raw_params = {"handle": "obj-enum-probe", "function": "EchoRawByte", "arguments": {"Input": 255}}
        raw_return = {"function": "EchoRawByte", "return_type": "ByteProperty", "result": 255,
                      "executed": True, "thread_id": 456}
        invalid_params = {"handle": "obj-enum-probe", "function": "EchoMode", "arguments": {"Input": 3}}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 50}
        steps = [
            {"op": "invoke", "params": params, "result": result(aliases)},
            {"op": "invoke", "params": params, "result": result(unavailable)},
            {"op": "invoke", "params": raw_params, "result": raw_return},
            {"op": "invoke", "params": invalid_params, "error": {"code": "unknown_enum_value",
                "message": "Число не объявлено в UEnum", "details": no_effect}},
            {"op": "status", "params": {}, "result": status},
        ]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            modkit = root / "Modkit"
            (modkit / "Script").mkdir(parents=True)
            (modkit / "Script" / "Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    for metadata in (aliases, unavailable):
                        returned = await client.call_tool("sn2_invoke", params)
                        self.assertFalse(returned.is_error, returned.content)
                        self.assertEqual(returned.structured_content, result(metadata))
                        self.assertEqual(returned.structured_content["result"], 7)
                    raw = await client.call_tool("sn2_invoke", raw_params)
                    self.assertFalse(raw.is_error, raw.content)
                    self.assertEqual(raw.structured_content, raw_return)
                    self.assertNotIn("return_enum", raw.structured_content)
                    rejected = await client.call_tool("sn2_invoke", invalid_params)
                    self.assertTrue(rejected.is_error, rejected.content)
                    text = next(block.text for block in rejected.content if block.type == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "unknown_enum_value")
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[3]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke"] * 4 + ["status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 5)


if __name__ == "__main__":
    unittest.main()
