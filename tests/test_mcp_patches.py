"""Сквозной stdio: хеш официального исходника → diff → рабочий .patch без игры."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class MCPScriptPatchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def call_ok(self, client, name, arguments):
        response = await client.call_tool(name, arguments)
        self.assertFalse(response.is_error, response.content)
        return response.structured_content

    async def call_error(self, client, name, arguments, code):
        response = await client.call_tool(name, arguments)
        self.assertTrue(response.is_error)
        value = next(block.text for block in response.content if block.type == "text")
        result = json.loads(value[value.index("{"):])
        self.assertEqual(result["code"], code)
        return result

    async def test_read_source_build_and_write_patch_preserve_exact_source_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modkit = root / "Modkit"
            source = modkit / "Script/Tests/Проверка с пробелом.as"
            source.parent.mkdir(parents=True)
            before = b"\xef\xbb\xbf" + "class UTest {\r\n int ReadMarker() { return 10; }\r\n}".encode("utf-8")
            source.write_bytes(before)
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(workspace)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), 64)
                    self.assertTrue(tools["g1r_build_script_patch"].annotations.read_only_hint)
                    self.assertFalse(tools["g1r_build_script_patch"].annotations.destructive_hint)
                    self.assertTrue(tools["g1r_read_source_exact"].annotations.read_only_hint)
                    self.assertTrue(tools["g1r_read_package_file_exact"].annotations.read_only_hint)
                    read = await self.call_ok(client, "g1r_read_source", {
                        "relative_path": "Script/Tests/Проверка с пробелом.as", "line_count": 1,
                    })
                    self.assertEqual(read["sha256"], hashlib.sha256(before).hexdigest())
                    self.assertTrue(read["content_truncated"])
                    self.assertEqual(read["returned_lines"], 1)
                    exact = await self.call_ok(client, "g1r_read_source_exact", {"relative_path": read["path"]})
                    self.assertEqual(exact["sha256"], read["sha256"])
                    self.assertEqual(exact["text"].encode("utf-8"), before)
                    self.assertTrue(exact["contains_bom"])
                    self.assertEqual(exact["text_mode"], "exact")
                    self.assertFalse(exact["content_truncated"])
                    replacement = exact["text"].replace("return 10", "return 11", 1)
                    built = await self.call_ok(client, "g1r_build_script_patch", {
                        "source_path": read["path"], "replacement_content": replacement,
                        "expected_source_sha256": read["sha256"],
                    })
                    self.assertEqual(built["source_sha256"], read["sha256"])
                    self.assertEqual(built["replacement_sha256"], hashlib.sha256(replacement.encode()).hexdigest())
                    self.assertEqual(built["patch_bytes"], len(built["patch_text"].encode("utf-8")))
                    self.assertTrue(built["changed"])
                    self.assertEqual(built["hunks"], 1)
                    self.assertEqual(built["engine_apply"], "not_performed")
                    self.assertFalse(built["compiled"])
                    self.assertIn("\\ No newline at end of file\n", built["patch_text"])
                    await self.call_ok(client, "g1r_create_package", {
                        "relative_path": "PatchExample",
                        "metadata": {"name": "PatchExample", "title": "Проверочная правка", "author": "Test", "modVersion": "1.0.0"},
                    })
                    written = await self.call_ok(client, "g1r_write_package_file", {
                        "relative_path": "PatchExample", "file_path": "ScriptPatches/Marker.patch",
                        "content": built["patch_text"],
                    })
                    patch_file = workspace / "PatchExample/ScriptPatches/Marker.patch"
                    self.assertEqual(patch_file.read_bytes(), built["patch_text"].encode("utf-8"))
                    self.assertEqual(written["sha256"], hashlib.sha256(patch_file.read_bytes()).hexdigest())
                    stored_exact = await self.call_ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "PatchExample", "file_path": "ScriptPatches/Marker.patch",
                    })
                    self.assertEqual(stored_exact["text"], built["patch_text"])
                    self.assertEqual(stored_exact["text"].encode("utf-8"), patch_file.read_bytes())
                    self.assertEqual(stored_exact["sha256"], written["sha256"])
                    self.assertFalse(stored_exact["truncated"])
                    validation = await self.call_ok(client, "g1r_validate_package", {"relative_path": "PatchExample"})
                    self.assertTrue(validation["valid_structure"])
                    self.assertEqual(validation["patch_count"], 1)
                    self.assertEqual(validation["patch_validation"], "not_performed")
                    listing = await self.call_ok(client, "g1r_list_package_files", {"relative_path": "PatchExample"})
                    self.assertEqual([item["path"] for item in listing["items"]],
                                     ["GothicMod.json", "ScriptPatches/Marker.patch"])
                    unchanged = await self.call_ok(client, "g1r_build_script_patch", {
                        "source_path": read["path"], "replacement_content": before.decode("utf-8"),
                        "expected_source_sha256": read["sha256"],
                    })
                    self.assertFalse(unchanged["changed"])
                    self.assertEqual(unchanged["patch_text"], "")
            target = root / "apply" / read["path"]
            target.parent.mkdir(parents=True)
            target.write_bytes(before)
            checked = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check", "--directory=Script", str(patch_file)],
                                     cwd=root / "apply", capture_output=True)
            self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors="replace"))
            applied = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--directory=Script", str(patch_file)],
                                     cwd=root / "apply", capture_output=True)
            self.assertEqual(applied.returncode, 0, applied.stderr.decode(errors="replace"))
            self.assertEqual(target.read_bytes(), replacement.encode("utf-8"))
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(any(path.name.startswith(".g1r-mcp-") for path in root.rglob("*")))

    async def test_large_source_diff_and_workspace_roundtrip_preserve_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modkit = root / "Modkit"
            source = modkit / "Script/Large.as"
            source.parent.mkdir(parents=True)

            def script(character):
                lines = ["\ufeffclass ULargePatch {", *(
                    f"    // Строка {index:04d} {character * 2048}" for index in range(1025)),
                    "    int ReadMarker() { return 42; }", "}"]
                return "\r\n".join(lines).encode("utf-8")

            before, after = script("a"), script("b")
            self.assertGreater(len(before), 2 * 1024 * 1024)
            self.assertGreater(len(after), 2 * 1024 * 1024)
            source.write_bytes(before)
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(workspace)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    exact = await self.call_ok(client, "g1r_read_source_exact", {"relative_path": "Script/Large.as"})
                    self.assertEqual(exact["text"].encode("utf-8"), before)
                    self.assertEqual(exact["sha256"], hashlib.sha256(before).hexdigest())
                    self.assertTrue(exact["contains_bom"])
                    self.assertFalse(exact["content_truncated"])
                    built = await self.call_ok(client, "g1r_build_script_patch", {
                        "source_path": "Script/Large.as", "replacement_content": after.decode("utf-8"),
                        "expected_source_sha256": exact["sha256"], "context_lines": 64})
                    patch_bytes = built["patch_text"].encode("utf-8")
                    self.assertGreater(len(patch_bytes), 2 * 1024 * 1024)
                    self.assertEqual(built["patch_bytes"], len(patch_bytes))
                    self.assertEqual(built["source_bytes"], len(before))
                    self.assertEqual(built["replacement_bytes"], len(after))
                    self.assertEqual(built["replacement_sha256"], hashlib.sha256(after).hexdigest())
                    self.assertEqual(built["context_lines"], 64)
                    await self.call_ok(client, "g1r_create_package", {
                        "relative_path": "LargePatch", "metadata": {
                            "name": "LargePatch", "title": "Большой проверочный патч", "author": "Test", "modVersion": "1.0.0"}})
                    await self.call_ok(client, "g1r_create_package_directory", {"relative_path": "LargePatch", "directory_path": "ScriptPatches"})
                    written = await self.call_ok(client, "g1r_write_package_file", {
                        "relative_path": "LargePatch", "file_path": "ScriptPatches/Large.patch", "content": built["patch_text"]})
                    stored = await self.call_ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "LargePatch", "file_path": "ScriptPatches/Large.patch"})
                    self.assertEqual(stored["text"].encode("utf-8"), patch_bytes)
                    self.assertEqual(written["sha256"], hashlib.sha256(patch_bytes).hexdigest())
                    self.assertEqual(stored["sha256"], written["sha256"])
                    self.assertFalse(stored["truncated"])

            target = root / "apply/Script/Large.as"
            target.parent.mkdir(parents=True)
            target.write_bytes(before)
            patch_file = workspace / "LargePatch/ScriptPatches/Large.patch"
            for options in (["--check"], [], ["--reverse", "--check"], ["--reverse"]):
                result = subprocess.run(
                    ["git", "-c", "core.autocrlf=false", "apply", *options, "--directory=Script", str(patch_file)],
                    cwd=root / "apply", capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                if not options:
                    self.assertEqual(target.read_bytes(), after)
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(source.read_bytes(), before)
            self.assertFalse(any(path.name.startswith(".g1r-mcp-") for path in root.rglob("*")))

    async def test_changed_source_unsafe_paths_and_invalid_text_are_explicit_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            modkit = root / "Modkit"
            source = modkit / "Script/Test.as"
            source.parent.mkdir(parents=True)
            before = b"old\n"
            source.write_bytes(before)
            example = modkit / "Examples/Sample/Script/Private.as"
            example.parent.mkdir(parents=True)
            example.write_bytes(b"example\n")
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable,
                args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(workspace)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    read = await self.call_ok(client, "g1r_read_source", {"relative_path": "Script/Test.as"})
                    source.write_bytes(b"external edit\n")
                    params = {"source_path": "Script/Test.as", "replacement_content": "new\n",
                              "expected_source_sha256": read["sha256"]}
                    await self.call_error(client, "g1r_build_script_patch", params, "conflict")
                    await self.call_error(client, "g1r_build_script_patch",
                                          {**params, "source_path": "../Script/Test.as"}, "unsafe_path")
                    await self.call_error(client, "g1r_build_script_patch",
                                          {**params, "source_path": "Examples/Sample/Script/Private.as"}, "unsupported_file")
                    await self.call_error(client, "g1r_build_script_patch",
                                          {**params, "context_lines": -1}, "invalid_arguments")
                    await self.call_error(client, "g1r_build_script_patch",
                                          {**params, "replacement_content": "x\0y",
                                           "expected_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}, "invalid_text")
                    package = workspace / "Limits"
                    (package / "Script").mkdir(parents=True)
                    large_script = package / "Script/TooLarge.as"
                    large_script.write_bytes(b"x" * (1024 * 1024 + 1))
                    large_descriptor = package / "GothicMod.json"
                    large_descriptor.write_bytes(b" " * (256 * 1024 + 1))
                    large_read = await self.call_ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "Limits", "file_path": "Script/TooLarge.as",
                    })
                    self.assertEqual(large_read["text"].encode("utf-8"), large_script.read_bytes())
                    descriptor_read = await self.call_ok(client, "g1r_read_package_file_exact", {
                        "relative_path": "Limits", "file_path": "GothicMod.json",
                    })
                    self.assertEqual(descriptor_read["text"].encode("utf-8"), large_descriptor.read_bytes())
                    self.assertEqual(descriptor_read["sha256"], hashlib.sha256(large_descriptor.read_bytes()).hexdigest())
                    self.assertFalse(descriptor_read["truncated"])
            self.assertEqual(source.read_bytes(), b"external edit\n")
            self.assertEqual(example.read_bytes(), b"example\n")
            self.assertEqual([path.name for path in workspace.iterdir()], ["Limits"])
            self.assertEqual(large_script.stat().st_size, 1024 * 1024 + 1)
            self.assertEqual(large_descriptor.stat().st_size, 256 * 1024 + 1)


if __name__ == "__main__":
    unittest.main()
