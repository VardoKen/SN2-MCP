"""Изолированные проверки чтения и границ статического каталога Modkit."""

import io
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1r_mcp.sources import SourceCatalog, SourceError


class SourceCatalogTests(unittest.TestCase):
    def test_exact_source_preserves_full_utf8_bytes_and_display_mode_is_explicit(self):
        variants = (b"int A = 1;\nint B = 2;\n", b"int A = 1;\r\nint B = 2;\r\n",
                    b"\xef\xbb\xbfint A = 1;\nint B = 2;\n", b"int A = 1;\nint B = 2;")
        for raw in variants:
            with self.subTest(raw=raw):
                self.write("Script/Exact.as", raw)
                result = self.catalog().read_file_exact("Script/Exact.as")
                self.assertEqual(result["text"].encode("utf-8"), raw)
                self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
                self.assertEqual(result["bytes"], len(raw))
                self.assertEqual(result["contains_bom"], raw.startswith(b"\xef\xbb\xbf"))
                self.assertEqual(result["text_mode"], "exact")
                self.assertFalse(result["content_truncated"])
                self.assertEqual(result["revision_source"], "caller_label")
                display = self.catalog().read_file("Script/Exact.as")
                self.assertEqual(display["text_mode"], "display_lines")
                self.assertTrue(display["exact_replacement_available"])

    def test_large_exact_source_and_examples_share_allowlist(self):
        raw = b"// " + b"x" * (2 * 1024 * 1024 + 1) + b"\r\nclass UExample {}\r\n"
        self.write("Examples/Sample/Script/Exact.as", raw)
        result = self.catalog().read_file_exact("Examples/Sample/Script/Exact.as")
        self.assertEqual(result["bytes"], len(raw))
        self.assertEqual(result["text"].encode("utf-8"), raw)
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertIsNone(result["max_text_bytes"])
        self.assertEqual(result["root_kind"], "example_script")
        self.assertEqual(self.catalog().read_file("Examples/Sample/Script/Exact.as")["total_lines"], 2)
        for value in ("../private.as", "Other/Exact.as", "Script\\Exact.as", "C:/private.as"):
            with self.subTest(path=value), self.assertRaises(SourceError):
                self.catalog().read_file_exact(value)

    def test_exact_source_preserves_minimal_patch_lines(self):
        from g1r_mcp.patches import ScriptPatchBuilder
        variants = (b"int A = 1;\nint B = 2;\n", b"int A = 1;\r\nint B = 2;\r\n",
                    b"\xef\xbb\xbfint A = 1;\nint B = 2;\n", b"int A = 1;\nint B = 2;")
        for raw in variants:
            with self.subTest(raw=raw):
                self.write("Script/Patch.as", raw)
                result = self.catalog().read_file_exact("Script/Patch.as")
                replacement = result["text"].replace("A = 1", "A = 3")
                patch_result = ScriptPatchBuilder(self.root).build_script_patch(
                    "Script/Patch.as", replacement, result["sha256"])
                self.assertEqual(patch_result["replacement_sha256"],
                                 hashlib.sha256(raw.replace(b"A = 1", b"A = 3")).hexdigest())
                self.assertNotIn("-int B", patch_result["patch_text"])
                self.assertNotIn("+int B", patch_result["patch_text"])

    def test_source_readers_reject_windows_alternate_data_stream_path(self):
        self.write("Script/Host.txt", "Обычный файл")
        catalog = self.catalog()
        for reader in (catalog.read_file, catalog.read_file_exact):
            with self.subTest(reader=reader.__name__), self.assertRaises(SourceError) as captured:
                reader("Script/Host.txt:Hidden.as")
            self.assertEqual(captured.exception.code, "unsafe_path")

    def test_source_hash_uses_complete_raw_bytes_while_text_is_paged(self):
        raw = b"\xef\xbb\xbf" + "первая\r\nвторая\r\n".encode("utf-8")
        self.write("Script/Hash.as", raw)
        result = self.catalog().read_file("Script/Hash.as", 2, 1)
        self.assertEqual(result["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(result["text"], "вторая")
        self.assertTrue(result["content_truncated"])

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "Modkit"
        (self.root / "Script").mkdir(parents=True)

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))
        return path

    def catalog(self):
        return SourceCatalog(self.root, revision="source-revision")

    def test_allowlist_and_file_search_are_complete_and_bounded(self):
        self.write("Script/Z.as", "class Z {}")
        self.write("Script/A.as", "class A {}")
        self.write("Examples/Sample/Script/A.as", "class Sample {}")
        self.write("Examples/Sample/Content/A.as", "class Forbidden {}")
        self.write("Other/A.as", "class Forbidden {}")
        self.write("Script/A.txt", "class Forbidden {}")
        result = self.catalog().search_files(".as", 2)
        self.assertEqual(result["total_matches"], 3)
        self.assertEqual(result["scanned_files"], 3)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["scan_complete"])
        self.assertEqual([item["path"] for item in result["items"]], ["Examples/Sample/Script/A.as", "Script/A.as"])
        self.assertEqual(result["validation"], "source_only")
        self.assertEqual(result["revision_source"], "caller_label")

    def test_text_search_is_literal_unicode_and_has_real_line_numbers(self):
        self.write("Script/Test.as", "// Мир\nstring pattern = \"a+b.*[x]\";\n// МИР\n")
        result = self.catalog().search_text("a+b.*[x]", 20)
        self.assertEqual(result["total_matches"], 1)
        self.assertEqual(result["items"][0]["line"], 2)
        self.assertEqual(self.catalog().search_text("мир", 1)["total_matches"], 2)
        self.assertEqual(self.catalog().search_text("Мир", 20, True)["total_matches"], 1)

    def test_read_slice_bom_end_and_partial_metadata(self):
        self.write("Script/Test.as", b"\xef\xbb\xbfline1\nline2\nline3\n")
        catalog = self.catalog()
        result = catalog.read_file("Script/Test.as", 2, 1)
        self.assertEqual(result["text"], "line2")
        self.assertEqual(result["total_lines"], 3)
        self.assertTrue(result["content_truncated"])
        self.assertEqual(result["truncation_scope"], "items")
        self.assertEqual(catalog.read_file("Script/Test.as", 4)["text"], "")
        self.assertFalse(catalog.read_file("Script/Test.as")["content_truncated"])
        self.write("Examples/Sample/Script/Test.as", "example")
        self.assertEqual(catalog.read_file("Examples/Sample/Script/Test.as")["root_kind"], "example_script")

    def test_path_traversal_absolute_and_non_allowlisted_rejected(self):
        self.write("Script/Test.as", "test")
        self.write("Other/Test.as", "private")
        catalog = self.catalog()
        for path in ("../Test.as", "/Script/Test.as", "C:/private.as", "C:private.as", "//host/private.as", "Script/../Test.as", "Script\\Test.as"):
            with self.subTest(path=path), self.assertRaises(SourceError) as captured:
                catalog.read_file(path)
            self.assertEqual(captured.exception.code, "unsafe_path")
            self.assertNotIn(str(self.root), str(captured.exception))
        with self.assertRaises(SourceError) as captured:
            catalog.read_file("Other/Test.as")
        self.assertEqual(captured.exception.code, "source_not_found")

    def test_links_never_supply_external_content(self):
        self.write("Script/Local.as", "local")
        outside = self.root.parent / "outside.as"
        outside.write_text("private", encoding="utf-8")
        try:
            (self.root / "Script/Link.as").symlink_to(outside)
        except OSError:
            self.skipTest("Система не разрешает изолированную проверку symlink")
        with self.assertRaises(SourceError) as captured:
            self.catalog().search_text("private")
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_dangling_example_script_link_is_not_silently_omitted(self):
        self.write("Script/Local.as", "local")
        example = self.root / "Examples/Sample"
        example.mkdir(parents=True)
        try:
            (example / "Script").symlink_to(self.root.parent / "missing", target_is_directory=True)
        except OSError:
            self.skipTest("Система не разрешает изолированную проверку symlink")
        with self.assertRaises(SourceError) as captured:
            self.catalog().search_files("Local")
        self.assertEqual(captured.exception.code, "unsafe_path")

    def test_invalid_encoding_is_explicit_for_large_source(self):
        bad = self.write("Script/Test.as", b"\xffprivate")
        with self.assertRaises(SourceError) as captured:
            self.catalog().read_file("Script/Test.as")
        self.assertEqual(captured.exception.code, "invalid_encoding")
        self.assertNotIn("private", str(captured.exception))
        bad.write_bytes(b" " * (2 * 1024 * 1024 + 1) + b"\xffprivate")
        with self.assertRaises(SourceError) as captured:
            self.catalog().read_file("Script/Test.as")
        self.assertEqual(captured.exception.code, "invalid_encoding")
        self.assertNotIn("private", str(captured.exception))

    def test_partial_scans_report_skipped_files_and_lower_bound(self):
        self.write("Script/Good.as", "class Good {}")
        self.write("Script/Bad.as", b"\xffprivate")
        self.write("Script/Large.as", b" " * (2 * 1024 * 1024 + 1))
        for result in (self.catalog().search_text("Good"), self.catalog().list_types("Good")):
            self.assertFalse(result["scan_complete"])
            self.assertIsNone(result["total_matches"])
            self.assertEqual(result["total_matches_lower_bound"], 1)
            self.assertEqual(result["skipped_count"], 1)
            self.assertEqual({item["error_code"] for item in result["skipped_files"]}, {"invalid_encoding"})
            self.assertEqual(result["items"][0]["path"], "Script/Good.as")

    def test_long_unicode_line_is_returned_in_full_with_requested_line_paging(self):
        text = "М" * (64 * 1024 + 1)
        self.write("Script/Long.as", text + "\nsecond")
        result = self.catalog().read_file("Script/Long.as", line_count=1)
        self.assertEqual(result["returned_utf8_bytes"], len(text.encode("utf-8")))
        self.assertTrue(result["content_truncated"])
        self.assertFalse(result["last_line_truncated"])
        self.assertEqual(result["returned_lines"], 1)
        self.assertEqual(result["end_line"], 1)
        self.assertEqual(result["text"], text)
        self.assertIsNone(result["max_text_bytes"])
        self.assertEqual(self.catalog().read_file("Script/Long.as", start_line=2, line_count=1)["text"], "second")

    def test_large_valid_source_search_types_and_exact_preserve_end_marker_and_bytes(self):
        raw = b"\xef\xbb\xbf// " + b"x" * (2 * 1024 * 1024 + 1) + b"\r\nclass ULargeEndMarker {}\r\n"
        path = self.write("Script/Large.as", raw)
        catalog = self.catalog()
        digest = hashlib.sha256(raw).hexdigest()
        searched = catalog.search_text("ULargeEndMarker")
        self.assertTrue(searched["scan_complete"])
        self.assertEqual(searched["skipped_count"], 0)
        self.assertEqual(searched["items"][0]["line"], 2)
        types = catalog.list_types("ULargeEndMarker")
        self.assertTrue(types["scan_complete"])
        self.assertEqual(types["items"][0]["file_sha256"], digest)
        exact = catalog.read_file_exact("Script/Large.as")
        self.assertEqual(exact["text"].encode("utf-8"), raw)
        self.assertEqual(exact["sha256"], digest)
        self.assertTrue(exact["contains_bom"])
        self.assertFalse(exact["content_truncated"])
        self.assertIsNone(exact["max_text_bytes"])
        display = catalog.read_file("Script/Large.as", start_line=2, line_count=1)
        self.assertEqual(display["text"], "class ULargeEndMarker {}")
        self.assertEqual(display["sha256"], digest)
        self.assertEqual(path.read_bytes(), raw)

    def test_requested_line_count_can_exceed_legacy_display_limit(self):
        lines = [f"int Entry{index} = {index};" for index in range(501)]
        self.write("Script/Lines.as", "\r\n".join(lines))
        result = self.catalog().read_file("Script/Lines.as", line_count=501)
        self.assertEqual(result["text"], "\n".join(lines))
        self.assertEqual(result["returned_lines"], 501)
        self.assertFalse(result["content_truncated"])

    def test_types_mask_comments_strings_and_track_nested_namespaces(self):
        self.write("Script/Types.as", '''// class Wrong {}
namespace A {
namespace B::C {
UCLASS() class UReal : UBase { string S = "enum Wrong {}"; }
/* struct Wrong {} */
struct Data {}
enum State { Ready }
}}
class Outside {}
''')
        result = self.catalog().list_types()
        self.assertEqual([item["qualified_name"] for item in result["items"]], ["A::B::C::UReal", "A::B::C::Data", "A::B::C::State", "Outside"])
        self.assertEqual(result["items"][0]["line"], 4)
        self.assertEqual(result["parser_kind"], "lexical")
        self.assertEqual(self.catalog().list_types("data")["total_matches"], 1)

    def test_type_headers_preserve_raw_multiline_bom_crlf_and_unresolved_base(self):
        declaration = "shared\r\nclass UChild\r\n    : Outer::UBase /* inherited source type */"
        text = ('namespace A {\r\nnamespace B::C {\r\nUCLASS()\r\n' + declaration +
                '\r\n{ string S = "class UWrong : Wrong {}"; }\r\n}}\r\n')
        raw = b"\xef\xbb\xbf" + text.encode("utf-8")
        self.write("Script/Inherited.as", raw)
        result = self.catalog().list_types("UChild")
        self.assertEqual(result["validation"], "source_only")
        self.assertEqual(result["parser_kind"], "lexical")
        self.assertEqual(result["items"], [{
            "path": "Script/Inherited.as", "root_kind": "game_script",
            "kind": "class", "name": "UChild", "qualified_name": "A::B::C::UChild", "line": 5,
            "declaration": declaration,
            "base_type": "Outer::UBase", "start_line": 4, "end_line": 6,
            "file_sha256": hashlib.sha256(raw).hexdigest()}])

    def test_type_headers_handle_forward_types_enum_underlying_and_comments(self):
        raw = (b"namespace N {\nclass UForward;\n"
               b"struct FData\n : Types::TBase<FValue> { }\n"
               b"enum EState : uint8 { Ready }\n"
               b"class UComment : /* lead comment */ N::Base /*middle*/ :: Type { }\n"
               b"class Plain {}\n}\n")
        self.write("Examples/Example/Script/Types.as", raw)
        items = self.catalog().list_types()["items"]
        self.assertEqual([item["declaration"] for item in items], [
            "class UForward", "struct FData\n : Types::TBase<FValue>", "enum EState : uint8",
            "class UComment : /* lead comment */ N::Base /*middle*/ :: Type", "class Plain"])
        self.assertEqual([item["base_type"] for item in items], [
            None, "Types::TBase<FValue>", None, "N::Base /*middle*/ :: Type", None])
        self.assertEqual([(item["start_line"], item["end_line"]) for item in items],
                         [(2, 2), (3, 4), (5, 5), (6, 6), (7, 7)])
        self.assertTrue(all(item["root_kind"] == "example_script" for item in items))
        self.assertTrue(all(item["file_sha256"] == hashlib.sha256(raw).hexdigest() for item in items))

    def test_type_headers_hash_current_file_bytes_and_keep_duplicate_declarations(self):
        raw = b"class Same : BaseA {}\nclass Same : BaseB {}\n"
        path = self.write("Script/Changed.as", raw)
        before = self.catalog().list_types("Same")["items"]
        self.assertEqual([item["base_type"] for item in before], ["BaseA", "BaseB"])
        replacement = raw.replace(b"BaseA", b"BaseC")
        path.write_bytes(replacement)
        after = self.catalog().list_types("Same")["items"]
        self.assertEqual(after[0]["declaration"], "class Same : BaseC")
        self.assertEqual(after[0]["file_sha256"], hashlib.sha256(replacement).hexdigest())
        self.assertNotEqual(before[0]["file_sha256"], after[0]["file_sha256"])

    def test_type_file_and_subtree_scopes_precede_pagination_and_preserve_global_items(self):
        self.write("Script/A.as", "class USharedOutside {}\n")
        selected_path = "Script/Sub/A.as"
        self.write(selected_path, b"\xef\xbb\xbfnamespace Local {\r\n"
                   b"class USharedFirst : UBase {}\r\nstruct USharedSecond {}\r\n"
                   b"enum USharedThird { Ready }\r\n}\r\n")
        self.write("Script/Sub/B.as", "class USharedSibling {}\n")
        catalog = self.catalog()
        global_result = catalog.list_types("Shared")
        self.assertEqual(global_result["scanned_files"], 3)
        self.assertEqual(global_result["total_matches"], 5)
        file_items = [item for item in global_result["items"] if item["path"] == selected_path]
        subtree_items = [item for item in global_result["items"] if item["path"].startswith("Script/Sub/")]

        with patch.object(catalog, "_files", side_effect=AssertionError("Точный файл не обходит inventory")):
            first = catalog.list_types("Shared", 2, 0, relative_path=selected_path)
            second = catalog.list_types("Shared", 2, first["next_offset"], relative_path=selected_path)
            eof = catalog.list_types("Shared", 2, len(file_items), relative_path=selected_path)
        self.assertEqual(first["items"], file_items[:2])
        self.assertEqual(second["items"], file_items[2:])
        self.assertEqual(first["next_offset"], 2)
        self.assertTrue(first["has_more"])
        self.assertIsNone(second["next_offset"])
        self.assertFalse(second["has_more"])
        self.assertEqual(eof["items"], [])
        self.assertIsNone(eof["next_offset"])
        for page in (first, second, eof):
            self.assertEqual(page["scanned_files"], 1)
            self.assertEqual(page["total_matches"], len(file_items))
            self.assertTrue(page["scan_complete"])

        first_subtree = catalog.list_types("Shared", 2, 0, relative_path="Script/Sub")
        second_subtree = catalog.list_types("Shared", 2, first_subtree["next_offset"], relative_path="Script/Sub")
        self.assertEqual(first_subtree["items"] + second_subtree["items"], subtree_items)
        self.assertEqual(first_subtree["next_offset"], 2)
        self.assertIsNone(second_subtree["next_offset"])
        for page in (first_subtree, second_subtree):
            self.assertEqual(page["scanned_files"], 2)
            self.assertEqual(page["total_matches"], len(subtree_items))
            self.assertTrue(page["scan_complete"])

    def test_invalid_arguments_and_empty_source_do_not_fake_success(self):
        with self.assertRaises(SourceError) as captured:
            self.catalog().search_files("x")
        self.assertEqual(captured.exception.code, "no_sources")
        self.write("Script/Test.as", "test")
        catalog = self.catalog()
        for operation in (lambda: catalog.search_files(""), lambda: catalog.search_text("x", 0), lambda: catalog.read_file("Script/Test.as", 0), lambda: catalog.read_file("Script/Test.as", 1, 0), lambda: catalog.read_file("Script/Test.as", 1, True), lambda: catalog.search_files("x", True)):
            with self.assertRaises(SourceError):
                operation()

    def test_file_changes_during_read_are_not_reported_as_current(self):
        path = self.write("Script/Test.as", "old")
        real_open = Path.open
        class ChangingStream(io.BytesIO):
            def __exit__(self, *args):
                path.write_text("changed-longer", encoding="utf-8")
                return super().__exit__(*args)
        def opened(file, *args, **kwargs):
            if file == path and args and args[0] == "rb":
                return ChangingStream(b"old")
            return real_open(file, *args, **kwargs)
        with patch.object(Path, "open", opened), self.assertRaises(SourceError) as captured:
            self.catalog().read_file("Script/Test.as")
        self.assertEqual(captured.exception.code, "source_changed")


if __name__ == "__main__":
    unittest.main()
