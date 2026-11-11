"""Source revisions and runtime evidence use neutral, isolated workspace data."""

import hashlib
import tempfile
import unittest
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from mcp.server.mcpserver.exceptions import ToolError

from sn2_mcp.workspace import SourceWorkspace, WorkspaceError
from sn2_mcp.server import create_server
from sn2_mcp.package_export import export_sources, verify_source_export, ExportError


class SourceWorkspaceTests(unittest.TestCase):
    def test_source_archive_preserves_bytes_and_detects_corruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = SourceWorkspace(root)
            workspace.write_file("mod.py", "\ufeffvalue = 0.5\r\n")
            exported = export_sources(workspace, ["mod.py"], "artifacts/source-one")
            self.assertTrue(verify_source_export(root / "artifacts/source-one")["verified_files"])
            self.assertFalse(exported["compiled"])
            with self.assertRaises(ExportError):
                export_sources(workspace, ["mod.py"], "artifacts/source-one")
            archive = root / "artifacts/source-one/sources.zip"
            archive.write_bytes(archive.read_bytes() + b"changed")
            with self.assertRaises(ExportError) as caught:
                verify_source_export(archive.parent)
            self.assertEqual(caught.exception.code, "integrity_mismatch")

    def test_exact_revision_conflict_and_search_with_binary_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = SourceWorkspace(root)
            workspace.create_directory("prototype/src")
            content = "\ufeff// neutral interface\r\nvalue = 0.5\r\n// neutral revision"
            written = workspace.write_file("prototype/src/change.py", content)
            read = workspace.read_file_exact("prototype/src/change.py")
            self.assertEqual(read["text"], content)
            self.assertEqual(read["sha256"], hashlib.sha256(content.encode("utf-8")).hexdigest())
            self.assertEqual(root.joinpath(read["path"]).read_bytes(), content.encode("utf-8"))
            with self.assertRaises(WorkspaceError) as caught:
                workspace.write_file(read["path"], "wrong revision", "0" * 64)
            self.assertEqual(caught.exception.code, "conflict")
            workspace.write_file(read["path"], content.replace("0.5", "0.75"), written["sha256"])
            (root / "prototype" / "asset.bin").write_bytes(b"\xff\xfe")
            workspace.create_directory("prototype/data")
            workspace.write_file("prototype/data/other.txt", "neutral")
            first = workspace.search_text("neutral", limit=1)
            second = workspace.search_text("neutral", limit=1, offset=first["next_offset"])
            self.assertNotEqual(first["items"], second["items"])
            self.assertFalse(first["scan_complete"])
            self.assertEqual(first["skipped_files"][0]["error_code"], "invalid_encoding")
            listed = workspace.list_files()
            self.assertIn("prototype/asset.bin", [item["path"] for item in listed["items"]])
            self.assertEqual(workspace.read_file(read["path"], 2, 1)["text"], "value = 0.75")

    def test_competing_revisions_cannot_both_replace_the_same_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = SourceWorkspace(root).write_file("source.txt", "baseline")["sha256"]

            def update(text):
                try:
                    return SourceWorkspace(root).write_file("source.txt", text, digest)["written"]
                except WorkspaceError as error:
                    return error.code

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(update, ["revision one", "revision two"]))
            self.assertCountEqual(results, [True, "conflict"])
            self.assertIn((root / "source.txt").read_text(), ["revision one", "revision two"])

    def test_separate_processes_cannot_both_replace_the_same_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            digest = SourceWorkspace(root).write_file("source.txt", "baseline")["sha256"]
            code = (
                "import sys; from sn2_mcp.workspace import SourceWorkspace,WorkspaceError; "
                "workspace=SourceWorkspace(sys.argv[1]);\n"
                "try: workspace.write_file('source.txt',sys.argv[3],sys.argv[2]); print('written')\n"
                "except WorkspaceError as error: print(error.code)\n"
            )
            processes = [subprocess.Popen([sys.executable, "-B", "-c", code, str(root), digest, revision],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)) for revision in ("one", "two")]
            try:
                outputs = [process.communicate(timeout=15)[0].strip() for process in processes]
                self.assertCountEqual(outputs, ["written", "conflict"])
                self.assertTrue(all(process.returncode == 0 for process in processes))
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)

    def test_independent_exports_share_first_artifacts_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            SourceWorkspace(root).write_file("mod.py", "value = 0.5\n")
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(lambda name: export_sources(SourceWorkspace(root), ["mod.py"],
                    "artifacts/" + name), ("one", "two")))
            self.assertEqual(len(outcomes), 2)
            for name in ("one", "two"):
                self.assertTrue(verify_source_export(root / "artifacts" / name)["verified_files"])

    def test_export_os_error_is_predictable_and_staging_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = SourceWorkspace(root)
            workspace.write_file("mod.py", "value = 0.5\n")
            with patch("sn2_mcp.package_export.verify_source_export", side_effect=OSError("neutral IO failure")):
                with self.assertRaises(ExportError) as caught:
                    export_sources(workspace, ["mod.py"], "artifacts/failed")
            self.assertEqual(caught.exception.code, "access_error")
            self.assertEqual(list((root / "artifacts").iterdir()), [])

    def test_excludes_runtime_outputs_and_protects_game_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            game = root / "installed-game"
            game.mkdir()
            (game / "config.ini").write_text("user configuration")
            (root / "artifacts").mkdir()
            (root / "artifacts" / "receipt.json").write_text("{}")
            (root / "nppBackup").mkdir()
            (root / "nppBackup" / "source.txt").write_text("backup")
            workspace = SourceWorkspace(root, protected_roots=(game,))
            for path in ("../source.txt", "artifacts/output.txt", "installed-game/config.ini"):
                with self.subTest(path=path), self.assertRaises(WorkspaceError):
                    workspace.write_file(path, "new")
            self.assertEqual((game / "config.ini").read_text(), "user configuration")
            with self.assertRaises(WorkspaceError):
                workspace.list_files()
            independent = SourceWorkspace(root)
            listed = independent.list_files()
            self.assertFalse(any("artifacts" in item["path"] or "nppBackup" in item["path"] for item in listed["items"]))


class SourceMCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_recipe_reports_world_transition_and_missing_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            SourceWorkspace(root).write_file("prototype.py", "opacity = 0.5\n")
            for final_world, expected in (
                ({"state": "observed", "generation": 1, "source": "GameEngine.GameViewport.World"}, True),
                ({"state": "observed", "generation": 2, "source": "GameEngine.GameViewport.World"}, False),
                ({"state": "unavailable", "generation": None}, None),
            ):
                with self.subTest(final_world=final_world):
                    identity = {"session_id": "test-session", "process_id": 123,
                                "process_creation_filetime": "456", "reflection_generation": None}
                    before = {**identity, "world": {"state": "observed", "generation": 1,
                              "source": "GameEngine.GameViewport.World"}}
                    after = {**identity, "world": final_world}
                    native = Mock()
                    native.request.side_effect = [before, {"items": []}, after]
                    with patch("sn2_mcp.server.NativeClient", return_value=native), patch("sn2_mcp.server.RuntimeService"):
                        server = create_server(root, game_root=root / "absent-game")
                        async with server.settings.lifespan(server):
                            recipe = await server.call_tool("sn2_development_recipe", {
                                "source_files": ["prototype.py"],
                                "targets": [{"handle": "obj-test", "properties": ["RenderOpacity"]}],
                            })
                            self.assertFalse(recipe.is_error)
                            self.assertIs(recipe.structured_content["runtime_identity_stable"], expected)
                            self.assertTrue(recipe.structured_content["source_stable"])
                    self.assertEqual(native.request.call_count, 3)

    async def test_source_revision_recipe_without_game_and_conflict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            native = Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), patch("sn2_mcp.server.RuntimeService"):
                server = create_server(root, game_root=root / "absent-game")
                async with server.settings.lifespan(server):
                    first = await server.call_tool("sn2_write_source", {"file_path": "prototype.py", "content": "opacity = 0.5\n"})
                    self.assertFalse(first.is_error)
                    read = await server.call_tool("sn2_read_source_exact", {"file_path": "prototype.py"})
                    self.assertEqual(read.structured_content["text"], "opacity = 0.5\n")
                    recipe = await server.call_tool("sn2_development_recipe", {"source_files": ["prototype.py"]})
                    self.assertFalse(recipe.is_error)
                    self.assertTrue(recipe.structured_content["source_stable"])
                    self.assertEqual(recipe.structured_content["source_files"][0]["sha256"], first.structured_content["sha256"])
                    self.assertFalse(recipe.structured_content["mod_execution_checked"])
                    with self.assertRaises(ToolError) as caught:
                        await server.call_tool("sn2_write_source", {"file_path": "prototype.py", "content": "revision", "expected_sha256": "0" * 64})
                    self.assertIn('"code": "conflict"', str(caught.exception))
                native.request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
