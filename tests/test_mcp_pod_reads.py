"""Страницы массивов и копии структур проходят публичный MCP без изменения формы."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture
from tests.test_subnautica_catalog import PUBLIC_TOOLS


class MCPPODReadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_large_array_page_and_high_offset_keep_exact_values_and_native_connection(self):
        offset, limit = 1_000_001, 9_000
        page = {
            "source": "live_native", "session_id": "fixture-session-1", "handle": "obj-pod",
            "property_name": "LargeNumbers", "consistency": "single_tick_sequential",
            "element_type": "IntProperty", "element_size": 4, "array_dim": 1,
            "total_count": offset + limit + 3, "offset": offset, "next_offset": offset + limit,
            "has_more": True, "truncated": True, "partial": False,
            "items": [{"index": index, "supported": True, "value": index * 3 - 7}
                      for index in range(offset, offset + limit)],
        }
        envelope = {"protocol": 1, "id": "fixture-size-check", "type": "response",
                    "session_id": "fixture-session-1", "result": page}
        response_bytes = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(response_bytes, 64 * 1024)
        self.assertLess(response_bytes, 1024 * 1024)
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 51}
        steps = [
            {"op": "read_array", "params": {"handle": "obj-pod", "property_name": "LargeNumbers",
             "offset": offset, "limit": limit}, "result": page},
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
                    response = await client.call_tool("sn2_read_array", {
                        "handle": "obj-pod", "property_name": "LargeNumbers", "offset": offset, "limit": limit})
                    self.assertFalse(response.is_error, response.content)
                    self.assertEqual(response.structured_content, page)
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["read_array", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 2)

    async def test_paged_array_struct_metadata_and_unsupported_layout_keep_owner_and_values(self):
        common = {"source": "live_native", "session_id": "fixture-session-1", "handle": "obj-pod"}
        page = {**common, "property_name": "Numbers", "consistency": "single_tick_sequential",
                "element_type": "IntProperty", "element_size": 4, "array_dim": 1,
                "total_count": 205, "offset": 200, "next_offset": None, "has_more": False,
                "truncated": False, "partial": False,
                "items": [{"index": index, "supported": True, "value": index * 3 - 7}
                          for index in range(200, 205)]}
        empty = {**common, "property_name": "EmptyNames", "consistency": "single_tick_sequential",
                 "element_type": "NameProperty", "element_size": 8, "array_dim": 1,
                 "total_count": 0, "offset": 0, "next_offset": None, "has_more": False,
                 "truncated": False, "partial": False, "items": []}
        struct_array = {**common, "property_name": "Positions2D", "consistency": "single_tick_sequential",
                        "element_type": "StructProperty", "element_size": 16, "array_dim": 1,
                        "total_count": 5, "offset": 2, "next_offset": 4, "has_more": True,
                        "truncated": True, "partial": False,
                        "element_struct": {"struct_path": "/Script/CoreUObject.Vector2D", "struct_name": "Vector2D",
                                           "properties_size": 16, "min_alignment": 8,
                                           "components": [{"name": "X", "type": "DoubleProperty", "element_size": 8, "offset": 0, "array_dim": 1},
                                                          {"name": "Y", "type": "DoubleProperty", "element_size": 8, "offset": 8, "array_dim": 1}]},
                        "items": [{"index": 2, "supported": True, "value": {"X": 12345.0625, "Y": -9876.125}},
                                  {"index": 3, "supported": True, "value": {"X": -0.03125, "Y": 32768.25}}]}
        struct = {**common, "property_name": "StoredVector",
                  "consistency": "copied_bytes_then_metadata_revalidated",
                  "struct_path": "/Script/ActualReflection.Vector", "properties_size": 24,
                  "min_alignment": 8, "partial": False, "truncated": False,
                  "components": [{"name": "Z", "type": "DoubleProperty", "element_size": 8,
                                  "offset": 16, "value": 3.75},
                                 {"name": "X", "type": "DoubleProperty", "element_size": 8,
                                  "offset": 0, "value": 1.25},
                                 {"name": "Y", "type": "DoubleProperty", "element_size": 8,
                                  "offset": 8, "value": -2.5}]}
        vector2d = {**common, "property_name": "StoredVector2D",
                    "consistency": "copied_bytes_then_metadata_revalidated",
                    "struct_path": "/Script/CoreUObject.Vector2D", "properties_size": 16,
                    "min_alignment": 8, "partial": False, "truncated": False,
                    "components": [{"name": "Y", "type": "DoubleProperty", "element_size": 8,
                                    "offset": 8, "value": -9876.125},
                                   {"name": "X", "type": "DoubleProperty", "element_size": 8,
                                    "offset": 0, "value": 12345.0625}]}
        no_effect = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 50}
        steps = [
            {"op": "read_array", "params": {"handle": "obj-pod", "property_name": "Numbers",
             "offset": 200, "limit": 200}, "result": page},
            {"op": "read_array", "params": {"handle": "obj-pod", "property_name": "EmptyNames",
             "offset": 0, "limit": 64}, "result": empty},
            {"op": "read_array", "params": {"handle": "obj-pod", "property_name": "Positions2D",
             "offset": 2, "limit": 2}, "result": struct_array},
            {"op": "read_struct", "params": {"handle": "obj-pod", "property_name": "StoredVector"}, "result": struct},
            {"op": "read_struct", "params": {"handle": "obj-pod", "property_name": "StoredVector2D"}, "result": vector2d},
            {"op": "read_struct", "params": {"handle": "obj-pod", "property_name": "NestedTransform"},
             "error": {"code": "unsupported_struct_layout", "message": "Структура не входит в подтверждённый POD-контракт",
                       "details": no_effect}},
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
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), len(PUBLIC_TOOLS))
                    struct_schema = tools["sn2_read_struct"].input_schema
                    self.assertEqual(set(struct_schema["properties"]), {"handle", "property_name", "timeout_seconds"})
                    self.assertEqual(set(struct_schema["required"]), {"handle", "property_name"})
                    array_schema = tools["sn2_read_array"].input_schema
                    self.assertEqual(set(array_schema["properties"]), {"handle", "property_name", "offset", "limit", "timeout_seconds"})
                    self.assertEqual(set(array_schema["required"]), {"handle", "property_name"})
                    self.assertEqual(array_schema["properties"]["offset"]["default"], 0)
                    self.assertEqual(array_schema["properties"]["limit"]["default"], 64)
                    capabilities = await client.call_tool("sn2_capabilities", {})
                    self.assertFalse(capabilities.is_error, capabilities.content)
                    self.assertEqual(capabilities.structured_content["accepted_types"], {})
                    self.assertEqual(capabilities.structured_content["capabilities"]["target_abi"]["state"], "unverified")
                    contracts = {entry["name"]: entry for entry in capabilities.structured_content["tool_contracts"]}
                    for name in ("sn2_read_array", "sn2_read_struct"):
                        contract = contracts[name]
                        self.assertIn("current_game_tick", contract["context"]["requires"])
                        self.assertEqual(contract["source"]["verification_kind"], "source")
                        self.assertFalse(contract["availability"]["checked"])
                    for name in ("sn2_read_array", "sn2_read_struct"):
                        annotations = tools[name].annotations
                        self.assertTrue(annotations.read_only_hint)
                        self.assertFalse(annotations.destructive_hint)
                        self.assertTrue(annotations.idempotent_hint)
                    for tool, params, expected in (
                        ("sn2_read_array", {"handle": "obj-pod", "property_name": "Numbers", "offset": 200, "limit": 200}, page),
                        ("sn2_read_array", {"handle": "obj-pod", "property_name": "EmptyNames"}, empty),
                        ("sn2_read_array", {"handle": "obj-pod", "property_name": "Positions2D", "offset": 2, "limit": 2}, struct_array),
                        ("sn2_read_struct", {"handle": "obj-pod", "property_name": "StoredVector"}, struct),
                        ("sn2_read_struct", {"handle": "obj-pod", "property_name": "StoredVector2D"}, vector2d),
                    ):
                        response = await client.call_tool(tool, params)
                        self.assertFalse(response.is_error, response.content)
                        self.assertEqual(response.structured_content, expected)
                    denied = await client.call_tool("sn2_read_struct", {"handle": "obj-pod", "property_name": "NestedTransform"})
                    self.assertTrue(denied.is_error, denied.content)
                    text = next(block.text for block in denied.content if block.type == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "unsupported_struct_layout")
                    self.assertIs(error["may_have_executed"], False)
                    self.assertEqual(error["effect"], no_effect)
                    self.assertEqual(error["request_id"], native.requests[5]["id"])
                    checked = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(checked.is_error, checked.content)
                    self.assertEqual(checked.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests],
                             ["read_array", "read_array", "read_array", "read_struct", "read_struct", "read_struct", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 7)


if __name__ == "__main__":
    unittest.main()
