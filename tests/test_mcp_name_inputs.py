"""Источник FName проходит публичный MCP без преобразования аргумента."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPNameInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_name_source_is_forwarded_exactly_and_rejected_input_keeps_connection(self):
        argument = {"name_from": {"handle": "obj-name-source", "property_name": "StoredEntry"}}
        accepted_params = {"handle": "obj-name-target", "function": "AcceptName",
                           "arguments": {"Input": argument}}
        rejected_params = {"handle": "obj-name-target", "function": "AcceptName",
                           "arguments": {"Input": "Имя_42"}}
        returned_name = {"source": "live_native", "session_id": "fixture-session-1",
                         "handle": "obj-name-target", "function": "AcceptName",
                         "return": "Имя_42"}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 51}
        message = "FName требует только {name_from:{handle,property_name}}"
        steps = [
            {"op": "invoke", "params": accepted_params, "result": returned_name},
            {"op": "invoke", "params": rejected_params,
             "error": {"code": "invalid_name_argument", "message": message, "details": no_effect}},
            {"op": "status", "params": {}, "result": status},
        ]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            modkit = root / "Modkit"
            (modkit / "Script").mkdir(parents=True)
            script = modkit / "Script" / "Fixture.as"
            script.write_text("class UFixture {}\n", encoding="utf-8")
            before = script.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), 33)
                    invoke = tools["sn2_invoke"]
                    self.assertFalse(invoke.annotations.read_only_hint)
                    self.assertFalse(invoke.annotations.idempotent_hint)
                    self.assertTrue(invoke.annotations.destructive_hint)
                    response = await client.call_tool("sn2_invoke", accepted_params)
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, returned_name)
                    denied = await client.call_tool("sn2_invoke", rejected_params)
                    self.assertTrue(denied.is_error, denied.content)
                    text = next(block.text for block in denied.content if block.type == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "invalid_name_argument")
                    self.assertEqual(error["message"], message)
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[1]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke", "invoke", "status"])
            self.assertEqual(native.requests[0]["params"], accepted_params)
            self.assertEqual(native.requests[1]["params"], rejected_params)
            self.assertEqual(len({request["id"] for request in native.requests}), 3)
            self.assertEqual(script.read_bytes(), before)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
