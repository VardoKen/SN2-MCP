"""Неясный эффект нативной команды должен сохраняться в публичном MCP-ответе."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


class MCPEffectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_incomplete_effects_and_faults_stay_uncertain_without_replay(self):
        params = {"handle": "obj-probe", "function": "FixtureAction", "arguments": {}}
        # executed=false само по себе сообщает лишь отсутствие подтверждённого
        # завершения; оно не обещает, что игрового эффекта не было.
        cases = [
            ("invalid_object_argument", {"executed": False, "may_have_executed": False,
                                         "do_not_retry": False}, False),
            ("fixture_unknown", {}, True),
            ("fixture_partial", {"executed": False}, True),
            ("fixture_partial", {"may_have_executed": False}, True),
            ("fixture_partial", {"executed": False, "may_have_executed": False}, False),
            ("fixture_partial", {"cause_code": "result_processing_failed"}, True),
            ("fixture_advisory", {"executed": False, "may_have_executed": False,
                                  "do_not_retry": True}, False),
            ("game_call_fault", {"executed": False, "may_have_executed": True,
                                 "do_not_retry": True}, True),
            ("game_result_error", {"executed": True, "may_have_executed": True,
                                   "do_not_retry": True, "cause_code": "stale_handle"}, True),
        ]
        steps = []
        for index, (code, details, _) in enumerate(cases):
            steps.append({"op": "invoke", "params": params,
                          "error": {"code": code, "message": "Проверочный отказ", "details": details}})
            steps.append({"op": "status", "params": {}, "result": {
                "ready": True, "session_id": "fixture-session-1", "tick_count": index + 1}})
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            root = Path(directory)
            modkit = root / "Modkit"
            script = modkit / "Script" / "Fixture.as"
            script.parent.mkdir(parents=True)
            script.write_text("class UFixture {}\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    for index, (code, details, uncertain) in enumerate(cases):
                        response = await client.call_tool("g1r_invoke", params)
                        self.assertTrue(response.is_error, response.content)
                        text = next(block.text for block in response.content if block.type == "text")
                        error = json.loads(text[text.index("{"):])
                        self.assertEqual(error["code"], code)
                        self.assertIs(error["may_have_executed"], uncertain, details)
                        self.assertEqual(error["effect"], details)
                        self.assertEqual(error["request_id"], native.requests[index * 2]["id"])
                        status = await client.call_tool("g1r_native_status", {})
                        self.assertFalse(status.is_error, status.content)
                        self.assertEqual(status.structured_content["tick_count"], index + 1)
            # После каждого отказа следует только явно запрошенный статус.
            # Не происходит ни повторного invoke, ни смены сессии.
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["invoke", "status"] * len(cases))
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))


if __name__ == "__main__":
    unittest.main()
