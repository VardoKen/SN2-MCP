"""Полный обход статических совпадений после предела одной страницы, без игры."""

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g1r_mcp.sources import SourceCatalog, SourceError


class SourcePaginationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Modkit"
        (self.root / "Script").mkdir(parents=True)
        self.catalog = SourceCatalog(self.root, revision="pagination-fixture")

    def write(self, relative, content):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
        return path

    def file_hashes(self):
        return {path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def operations(self):
        return (
            ("files", lambda limit, offset: self.catalog.search_files(".as", limit, offset)),
            ("text", lambda limit, offset: self.catalog.search_text("shared needle", limit, False, offset)),
            ("types", lambda limit, offset: self.catalog.list_types("SharedType", limit, offset)),
        )

    def test_all_three_searches_recover_identical_matches_beyond_first_200(self):
        content = "class USharedType {}\n// shared needle\n"
        for index in reversed(range(205)):
            self.write(f"Script/Entry{index:03}.as", content)
        before = self.file_hashes()
        expected = [f"Script/Entry{index:03}.as" for index in range(205)]
        for operation, request in self.operations():
            with self.subTest(operation=operation):
                first = request(200, 0)
                final = request(200, first["next_offset"])
                self.assertEqual(first["offset"], 0)
                self.assertEqual(first["next_offset"], 200)
                self.assertTrue(first["has_more"])
                self.assertTrue(first["truncated"])
                self.assertEqual(final["offset"], 200)
                self.assertIsNone(final["next_offset"])
                self.assertFalse(final["has_more"])
                self.assertEqual([len(first["items"]), len(final["items"])], [200, 5])
                paths = [item["path"] for page in (first, final) for item in page["items"]]
                self.assertEqual(paths, expected)
                self.assertEqual(len(set(paths)), 205)
                for page in (first, final):
                    self.assertEqual(page["total_matches"], 205)
                    self.assertEqual(page["scanned_files"], 205)
                    self.assertTrue(page["scan_complete"])
                    self.assertEqual(page["snapshot_kind"], "sequential_file_reads")
                    self.assertEqual(page["validation"], "source_only")
                    self.assertEqual(page["revision_source"], "caller_label")
                if operation == "text":
                    self.assertTrue(all(item["line"] == 2 for item in first["items"] + final["items"]))
                elif operation == "types":
                    self.assertTrue(all(item["qualified_name"] == "USharedType" for item in first["items"] + final["items"]))
                exhausted = request(200, 205)
                self.assertEqual(exhausted["items"], [])
                self.assertEqual(exhausted["total_matches"], 205)
                self.assertFalse(exhausted["has_more"])
                self.assertIsNone(exhausted["next_offset"])
        self.assertEqual(self.file_hashes(), before)

    def test_line_and_declaration_order_is_preserved_across_page_boundaries(self):
        self.write("Script/Order.as", "\n".join(
            f"class USharedType{index:03} {{}} // shared needle" for index in range(205)))
        for label, request in self.operations()[1:]:
            with self.subTest(operation=label):
                pages = [request(100, offset) for offset in (0, 100, 200)]
                items = [item for page in pages for item in page["items"]]
                self.assertEqual([item["line"] for item in items], list(range(1, 206)))
                self.assertEqual([page["next_offset"] for page in pages], [100, 200, None])
                self.assertEqual([len(page["items"]) for page in pages], [100, 100, 5])
                self.assertTrue(all(item["path"] == "Script/Order.as" for item in items))

    def test_offset_invalid_values_are_rejected_before_searching(self):
        self.write("Script/Valid.as", "class USharedType {} // shared needle\n")
        before = self.file_hashes()
        for label, request in self.operations():
            for value in (True, False, -1, 1.5, "0", None):
                with self.subTest(operation=label, offset=value), self.assertRaises(SourceError) as caught:
                    request(10, value)
                self.assertEqual(caught.exception.code, "invalid_arguments")
        self.assertEqual(self.file_hashes(), before)

    def test_partial_scan_pages_preserve_unknown_total_and_known_match_continuation(self):
        for index in range(205):
            self.write(f"Script/Good{index:03}.as", "class USharedType {}\n// shared needle\n")
        self.write("Script/Bad.as", b"\xff")
        self.write("Script/Large.as", b"// " + b"x" * (2 * 1024 * 1024 + 1))
        before = self.file_hashes()
        for operation, request in self.operations()[1:]:
            with self.subTest(operation=operation):
                first = request(200, 0)
                final = request(200, first["next_offset"])
                self.assertEqual(first["next_offset"], 200)
                self.assertTrue(first["has_more"])
                self.assertFalse(final["has_more"])
                self.assertIsNone(final["next_offset"])
                self.assertEqual([len(first["items"]), len(final["items"])], [200, 5])
                for page in (first, final):
                    self.assertFalse(page["scan_complete"])
                    self.assertIsNone(page["total_matches"])
                    self.assertEqual(page["total_matches_lower_bound"], 205)
                    self.assertEqual(page["skipped_count"], 1)
                    self.assertEqual(page["scanned_files"], 207)
                    self.assertEqual(page["snapshot_kind"], "sequential_file_reads")
                    self.assertEqual({item["error_code"] for item in page["skipped_files"]},
                                     {"invalid_encoding"})
                self.assertEqual([item["path"] for page in (first, final) for item in page["items"]],
                                 [f"Script/Good{index:03}.as" for index in range(205)])
        self.assertEqual(self.file_hashes(), before)


if __name__ == "__main__":
    unittest.main()
