"""Создание каталогов официального пакета через настоящий MCP stdio."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPDirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def ok(self, client, tool, arguments):
        result = await client.call_tool(tool, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertFalse(result.structured_content["compiled"])
        self.assertEqual(result.structured_content["runtime_validation"], "not_performed")
        return result.structured_content

    async def denied(self, client, tool, arguments, code, root):
        result = await client.call_tool(tool, arguments)
        self.assertTrue(result.is_error, result.content)
        text = next(block.text for block in result.content if block.type == "text")
        error = json.loads(text[text.index("{"):])
        self.assertEqual(error["code"], code)
        self.assertNotIn(str(root), text)

    async def test_nested_directories_exact_source_and_patch_creation_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit = root / "Modkit"
            official = modkit / "Script" / "Official.as"
            official.parent.mkdir(parents=True)
            official.write_bytes(b"class UOfficial {}\r\n")
            original = official.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(workspace),
                # Нет подключения игры: все выполняемые инструменты локальные.
                "--native-port", "65534",
            ])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    annotations = tools["g1r_create_package_directory"].annotations
                    self.assertFalse(annotations.read_only_hint)
                    self.assertFalse(annotations.destructive_hint)
                    self.assertTrue(annotations.idempotent_hint)
                    self.assertFalse(annotations.open_world_hint)
                    metadata = {"name": "Port", "title": "Проверочный перенос", "author": "Fixture",
                                "modVersion": "1.0.0"}
                    await self.ok(client, "g1r_create_package", {"relative_path": "Port", "metadata": metadata})
                    created = await self.ok(client, "g1r_create_package_directory", {
                        "relative_path": "Port", "directory_path": "Script/Port/Modules"})
                    self.assertTrue(created["created"])
                    self.assertEqual(created["created_paths"], ["Script/Port", "Script/Port/Modules"])
                    self.assertEqual(created["created_count"], 2)
                    raw = "\ufeff// точные байты\r\nclass UPortModule {}".encode("utf-8")
                    source = await self.ok(client, "g1r_write_package_file", {
                        "relative_path": "Port", "file_path": "Script/Port/Modules/Logic.as",
                        "content": raw.decode("utf-8")})
                    file = workspace / "Port" / "Script" / "Port" / "Modules" / "Logic.as"
                    self.assertEqual(file.read_bytes(), raw)
                    exact = await self.ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "Port", "file_path": "Script/Port/Modules/Logic.as"})
                    self.assertEqual(exact["text"].encode("utf-8"), raw)
                    self.assertEqual(exact["sha256"], hashlib.sha256(raw).hexdigest())
                    self.assertEqual(exact["sha256"], source["sha256"])
                    self.assertFalse(exact["truncated"])
                    before = {path.relative_to(workspace).as_posix(): path.read_bytes()
                              for path in workspace.rglob("*") if path.is_file()}
                    repeated = await self.ok(client, "g1r_create_package_directory", {
                        "relative_path": "Port", "directory_path": "Script/Port/Modules"})
                    self.assertFalse(repeated["created"])
                    self.assertEqual(repeated["created_count"], 0)
                    self.assertEqual(repeated["created_paths"], [])
                    self.assertEqual(before, {path.relative_to(workspace).as_posix(): path.read_bytes()
                                            for path in workspace.rglob("*") if path.is_file()})
                    patches = await self.ok(client, "g1r_create_package_directory", {
                        "relative_path": "Port", "directory_path": "ScriptPatches"})
                    self.assertEqual(patches["created_paths"], ["ScriptPatches"])
                    patch = "--- a/Script/Test.as\r\n+++ b/Script/Test.as\r\n@@ -1 +1 @@\r\n-old\r\n+new"
                    written_patch = await self.ok(client, "g1r_write_package_file", {
                        "relative_path": "Port", "file_path": "ScriptPatches/Port.patch", "content": patch})
                    read_patch = await self.ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "Port", "file_path": "ScriptPatches/Port.patch"})
                    self.assertEqual(read_patch["text"], patch)
                    self.assertEqual(read_patch["sha256"], written_patch["sha256"])
                    status = await client.call_tool("g1r_status", {})
                    self.assertFalse(status.is_error, status.content)
                    self.assertFalse(status.structured_content["game"]["connection_checked"])
            self.assertEqual(official.read_bytes(), original)
            self.assertFalse(any(path.name.startswith(".g1r-mcp-") for path in workspace.rglob("*")))

    async def test_refused_paths_and_file_collision_preserve_user_and_protected_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit = root / "Modkit"
            official = modkit / "Script" / "Official.as"
            official.parent.mkdir(parents=True)
            official.write_text("// official source", encoding="utf-8")
            original = official.read_bytes()
            # Workspace — общий родитель: защищённый Modkit нельзя обойти через пакетный путь.
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                "--workspace-root", str(root), "--native-port", "65534",
            ])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    await self.ok(client, "g1r_create_package", {"relative_path": "Port", "metadata": {
                        "name": "Port", "title": "Пакет", "author": "Fixture", "modVersion": "1.0.0"}})
                    occupied = root / "Port" / "Script" / "User.as"
                    occupied.write_bytes(b"// preserve user file\r\n")
                    before = {path.relative_to(root).as_posix(): path.read_bytes()
                              for path in root.rglob("*") if path.is_file()}
                    content = await self.ok(client, "g1r_create_package_directory", {
                        "relative_path": "Port", "directory_path": "Content/Cooked"})
                    self.assertTrue(content["created"])
                    self.assertEqual(content["created_paths"], ["Content", "Content/Cooked"])
                    self.assertTrue((root / "Port" / "Content" / "Cooked").is_dir())
                    repeated = await self.ok(client, "g1r_create_package_directory", {
                        "relative_path": "Port", "directory_path": "Content/Cooked"})
                    self.assertFalse(repeated["created"])
                    self.assertEqual(repeated["created_paths"], [])
                    for path, code in (("Script/CON", "unsafe_path"),
                                       ("Script/../Config", "unsafe_path"),
                                       ("Script/User.as/Modules", "conflict")):
                        await self.denied(client, "g1r_create_package_directory", {
                            "relative_path": "Port", "directory_path": path}, code, root)
                    await self.denied(client, "g1r_create_package_directory", {
                        "relative_path": "Modkit", "directory_path": "Script/New"}, "unsafe_path", root)
                    self.assertEqual(before, {path.relative_to(root).as_posix(): path.read_bytes()
                                            for path in root.rglob("*") if path.is_file()})
                    self.assertFalse((modkit / "Script" / "New").exists())
                    self.assertEqual(occupied.read_bytes(), b"// preserve user file\r\n")
            self.assertEqual(official.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
