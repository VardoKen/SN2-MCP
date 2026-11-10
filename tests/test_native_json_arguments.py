"""Недопустимый JSON отвергается до сокета; отказ не повторяет игровой вызов."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.native_client import NativeClient, NativeError
from sn2_mcp.server import create_server
from tests.test_native_client import FakeServer, frame, request, response


NO_SEND = {"executed": False, "may_have_executed": False, "do_not_retry": False,
           "request_started": False, "stage": "serialization_not_sent"}


class NativeJSONArgumentTests(unittest.TestCase):
    def test_serialization_failures_have_false_effect_without_opening_socket(self):
        cyclic = {}
        cyclic["self"] = cyclic
        inputs = [
            {"Point": {"X": math.nan, "Y": 0, "Z": 0}},
            {"Point": {"X": math.inf, "Y": 0, "Z": 0}},
            {"Point": {"X": -math.inf, "Y": 0, "Z": 0}},
            {"Input": object()},
            {"Input": "\ud800"},
            cyclic,
        ]
        identifiers = set()
        with NativeClient() as client, patch("sn2_mcp.native_client.socket.socket") as opened:
            for arguments in inputs:
                with self.subTest(type=type(arguments.get("Input")).__name__):
                    with self.assertRaises(NativeError) as caught:
                        client.request("invoke", {"arguments": arguments})
                    error = caught.exception
                    self.assertEqual(error.code, "invalid_arguments")
                    self.assertIs(error.may_have_executed, False)
                    self.assertEqual(error.details, NO_SEND)
                    self.assertIsNone(error.__cause__)
                    self.assertRegex(error.request_id, r"^[0-9a-f]{32}$")
                    identifiers.add(error.request_id)
            opened.assert_not_called()
            self.assertFalse(client._request_lock.locked())
        self.assertEqual(len(identifiers), len(inputs))

    def test_invalid_nested_input_does_not_drop_existing_connection(self):
        observed = []

        def handler(stream):
            for _ in range(2):
                value = request(stream)
                observed.append(value)
                stream.sendall(frame(response(value, {"op": value["op"]})))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            self.assertEqual(client.status(), {"op": "status"})
            with self.assertRaises(NativeError) as caught:
                client.request("invoke", {"arguments": {"Input": {"X": math.nan, "Y": 0, "Z": 0}}})
            self.assertEqual(caught.exception.details, NO_SEND)
            self.assertEqual(client.status(), {"op": "status"})
            self.assertEqual(server.accepted, 1)
        self.assertEqual([value["op"] for value in observed], ["status", "status"])
        self.assertEqual(len({value["id"] for value in observed}), 2)
        self.assertNotIn(caught.exception.request_id, {value["id"] for value in observed})


class NativeJSONToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_receives_structured_no_send_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit, workspace = root / "modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            workspace.mkdir()
            (modkit / "Script/Fixture.as").write_text("class UFixture {}", encoding="utf-8")
            server = create_server(modkit, workspace)
            async with server.settings.lifespan(server):
                with patch.object(NativeClient, "_connect") as opened:
                    with self.assertRaises(ToolError) as caught:
                        await server.call_tool("sn2_invoke", {"handle": "obj-fixture", "function": "EchoVector",
                            "arguments": {"Input": {"X": math.nan, "Y": 0, "Z": 0}}})
                    message = str(caught.exception)
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "invalid_arguments")
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], NO_SEND)
                    self.assertRegex(error["request_id"], r"^[0-9a-f]{32}$")
                    opened.assert_not_called()


if __name__ == "__main__":
    unittest.main()
