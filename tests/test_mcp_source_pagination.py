"""Реальная stdio-проверка продолжения статического поиска без игрового доступа."""

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPSourcePaginationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def call_ok(self, client, name, arguments):
        response = await client.call_tool(name, arguments)
        self.assertFalse(response.is_error, response.content)
        return response.structured_content

    async def call_error(self, client, name, arguments, code):
        response = await client.call_tool(name, arguments)
        self.assertTrue(response.is_error, response.content)
        text = next(block.text for block in response.content if block.type == "text")
        error = json.loads(text[text.index("{"):])
        self.assertEqual(error["code"], code)
        return error

    async def test_all_three_static_searches_reach_identical_matches_after_first_200(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modkit = root / "Modkit"
            script = modkit / "Script"
            script.mkdir(parents=True)
            raw = b"// repeated needle\nclass UDuplicate {}\n"
            expected_paths = [f"Script/Entry{index:03d}.as" for index in range(205)]
            for relative in expected_paths:
                (modkit / relative).write_bytes(raw)
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(workspace)])
            before = {relative: hashlib.sha256((modkit / relative).read_bytes()).hexdigest()
                      for relative in expected_paths}
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    for name in ("sn2_find_source_files", "sn2_search_source", "sn2_list_source_types"):
                        self.assertIn(name, tools)
                        self.assertTrue(tools[name].annotations.read_only_hint)
                        self.assertFalse(tools[name].annotations.destructive_hint)
                        self.assertIn("offset", tools[name].input_schema["properties"])
                    searches = (
                        ("sn2_find_source_files", {"query": ".as"}),
                        ("sn2_search_source", {"query": "repeated needle", "case_sensitive": True}),
                        ("sn2_list_source_types", {"query": "UDuplicate"}),
                    )
                    for name, arguments in searches:
                        with self.subTest(tool=name):
                            first = await self.call_ok(client, name, {**arguments, "limit": 200})
                            self.assertEqual(len(first["items"]), 200)
                            self.assertEqual(first["offset"], 0)
                            self.assertEqual(first["next_offset"], 200)
                            self.assertTrue(first["has_more"])
                            self.assertTrue(first["truncated"])
                            self.assertEqual(first["total_matches"], 205)
                            self.assertEqual(first["snapshot_kind"], "sequential_file_reads")
                            self.assertTrue(first["scan_complete"])
                            second = await self.call_ok(client, name, {
                                **arguments, "limit": 200, "offset": first["next_offset"],
                            })
                            self.assertEqual(len(second["items"]), 5)
                            self.assertEqual(second["offset"], 200)
                            self.assertIsNone(second["next_offset"])
                            self.assertFalse(second["has_more"])
                            self.assertTrue(second["truncated"])
                            self.assertEqual(second["total_matches"], 205)
                            self.assertTrue(second["scan_complete"])
                            all_items = first["items"] + second["items"]
                            self.assertEqual([item["path"] for item in all_items], expected_paths)
                            self.assertEqual(len({item["path"] for item in all_items}), 205)
                            if name == "sn2_search_source":
                                self.assertTrue(all(item["line"] == 1 and item["text"] == "// repeated needle"
                                                    for item in all_items))
                            if name == "sn2_list_source_types":
                                self.assertTrue(all(item["line"] == 2 and item["qualified_name"] == "UDuplicate"
                                                    for item in all_items))
                            empty = await self.call_ok(client, name, {**arguments, "limit": 200, "offset": 1000})
                            self.assertEqual(empty["items"], [])
                            self.assertFalse(empty["has_more"])
                            self.assertIsNone(empty["next_offset"])
                            distant = await self.call_ok(client, name, {**arguments, "limit": 200, "offset": 1_000_001})
                            self.assertEqual(distant["items"], [])
                            self.assertEqual(distant["offset"], 1_000_001)
                            self.assertEqual(distant["total_matches"], 205)
                            self.assertIsNone(distant["next_offset"])
                            self.assertFalse(distant["has_more"])
                            self.assertTrue(distant["truncated"])
                            self.assertTrue(distant["scan_complete"])
                            self.assertFalse(distant["pagination_limit_reached"])
                            self.assertIsNone(distant["max_offset"])
                            await self.call_error(client, name, {**arguments, "offset": -1}, "invalid_arguments")
            after = {relative: hashlib.sha256((modkit / relative).read_bytes()).hexdigest()
                     for relative in expected_paths}
            self.assertEqual(after, before)
            self.assertEqual(list(workspace.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
