"""MCP → TCP проверка фильтра функций и явных границ описанной сигнатуры."""
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_native import FakeNativeServer


class MCPFunctionQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_filter_routes_and_keeps_partial_signature_visible(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        legacy = {"items": [{"name": "Unrelated" + str(index), "owner": "Derived", "parameters": []}
                            for index in range(200)], "truncated": True,
                  "scan_complete": False, "scan_truncated": False}
        filtered = {"items": [
            {"name": "ReadTarget", "owner": "Derived", "parameters": [], "parameters_truncated": False},
            {"name": "ReadTarget", "owner": "Base", "parameters": [], "parameters_truncated": True},
        ], "truncated": True, "scan_complete": False, "scan_truncated": False}
        incomplete = {"items": [], "truncated": True, "scan_complete": False, "scan_truncated": True}
        invalid = {"code": "invalid_query", "message": "Фильтр функций превышает предел"}
        replies = [
            ("functions", {"handle": "obj-1", "limit": 200}, legacy, None),
            ("functions", {"handle": "obj-1", "limit": 2, "function_query": "target"}, filtered, None),
            ("functions", {"handle": "obj-1", "limit": 2, "function_query": "Missing"}, incomplete, None),
            ("functions", {"handle": "obj-1", "limit": 2, "function_query": "x" * 513}, None, invalid),
        ]
        with tempfile.TemporaryDirectory() as directory, FakeNativeServer(replies) as native:
            root = Path(directory)
            modkit = root / "Modkit"
            source = modkit / "Script" / "Probe.as"
            source.parent.mkdir(parents=True)
            source.write_text("class UProbe {}\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)],
            )
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    tool = tools["sn2_object_functions"]
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertEqual(tool.input_schema["properties"]["function_query"]["default"], "")
                    first = await client.call_tool("sn2_object_functions", {"handle": "obj-1", "limit": 200})
                    self.assertFalse(first.is_error)
                    self.assertEqual(first.structured_content, legacy)
                    self.assertTrue(first.structured_content["truncated"])
                    found = await client.call_tool("sn2_object_functions",
                                                   {"handle": "obj-1", "limit": 2, "function_query": "target"})
                    self.assertFalse(found.is_error)
                    self.assertEqual(found.structured_content, filtered)
                    self.assertEqual([item["owner"] for item in found.structured_content["items"]], ["Derived", "Base"])
                    self.assertTrue(found.structured_content["items"][1]["parameters_truncated"])
                    scan = await client.call_tool("sn2_object_functions",
                                                  {"handle": "obj-1", "limit": 2, "function_query": "Missing"})
                    self.assertFalse(scan.is_error)
                    self.assertEqual(scan.structured_content, incomplete)
                    self.assertFalse(scan.structured_content["scan_complete"])
                    failed = await client.call_tool("sn2_object_functions",
                                                    {"handle": "obj-1", "limit": 2, "function_query": "x" * 513})
                    self.assertTrue(failed.is_error)
            self.assertEqual(source.read_text(encoding="utf-8"), "class UProbe {}\n")
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
