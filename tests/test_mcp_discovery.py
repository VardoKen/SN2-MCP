"""Сквозная MCP → TCP проверка точного пути и прежнего поиска без игры."""
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from test_mcp_native import FakeNativeServer


class MCPDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_path_routes_without_changing_legacy_params(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0
        path = "/Script/CoreUObject.Class"
        found = {"items": [{"handle": "obj-1", "name": "Class", "class": "Class", "path": path}],
                 "truncated": False}
        empty = {"items": [], "truncated": False}
        bad = {"code": "invalid_query", "message": "Точный путь превышает допустимый размер"}
        replies = [
            ("objects", {"query": "Class", "class_query": "Class", "limit": 2}, found, None),
            ("objects", {"query": "", "class_query": "Class", "limit": 2, "exact_path": path}, found, None),
            ("objects", {"query": path, "class_query": "", "limit": 2}, found, None),
            ("objects", {"query": "", "class_query": "", "limit": 2,
                         "exact_path": "/script/CoreUObject.Class"}, empty, None),
            ("objects", {"query": "", "class_query": "", "limit": 2,
                         "exact_path": "x" * 8193}, None, bad),
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
                args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(workspace), "--native-port", str(native.port)],
            )
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    tool = tools["g1r_find_objects"]
                    self.assertTrue(tool.annotations.read_only_hint)
                    self.assertEqual(tool.input_schema["properties"]["exact_path"]["default"], "")
                    cases = [
                        ({"query": "Class", "class_query": "Class", "limit": 2}, found),
                        ({"class_query": "Class", "limit": 2, "exact_path": path}, found),
                        ({"query": path, "limit": 2}, found),
                        ({"limit": 2, "exact_path": "/script/CoreUObject.Class"}, empty),
                    ]
                    for arguments, expected in cases:
                        result = await client.call_tool("g1r_find_objects", arguments)
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content, expected)
                    result = await client.call_tool("g1r_find_objects", {"limit": 2, "exact_path": "x" * 8193})
                    self.assertTrue(result.is_error)
            self.assertEqual(source.read_text(encoding="utf-8"), "class UProbe {}\n")
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
