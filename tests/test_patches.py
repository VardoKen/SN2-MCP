"""Создание diff с подтверждением точных байтов через стандартный Git."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from g1r_mcp.patches import PatchError, ScriptPatchBuilder


class ScriptPatchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.modkit = self.root / "Modkit"
        (self.modkit / "Script").mkdir(parents=True)
        self.builder = ScriptPatchBuilder(self.modkit, revision="fixture-revision")

    def write(self, raw: bytes, relative="Script/Test.as"):
        file = self.modkit / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw)
        return file

    def assert_error(self, code, call):
        with self.assertRaises(PatchError) as error:
            call()
        self.assertEqual(error.exception.code, code)
        self.assertNotIn(str(self.root), str(error.exception))

    def apply_exact(self, before: bytes, after: bytes, *, relative="Script/Test.as", context=3):
        original = self.write(before, relative)
        result = self.builder.build_script_patch(relative, after.decode("utf-8"), hashlib.sha256(before).hexdigest(), context)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual(result["path"], relative)
        self.assertEqual(result["replacement_sha256"], hashlib.sha256(after).hexdigest())
        self.assertEqual(result["source_sha256"], hashlib.sha256(before).hexdigest())
        self.assertEqual(result["changed"], before != after)
        self.assertEqual(result["validation"], "standard_unified_diff")
        self.assertEqual(result["engine_apply"], "not_performed")
        self.assertFalse(result["compiled"])
        self.assertEqual(result["patch_bytes"], len(result["patch_text"].encode("utf-8")))
        if before == after:
            self.assertEqual(result["patch_text"], "")
            self.assertEqual(result["hunks"], 0)
            return result
        patch_relative = relative.removeprefix("Script/")
        self.assertTrue(result["patch_text"].startswith(f"--- a/{patch_relative}\n+++ b/{patch_relative}\n"))
        apply_root = self.root / "apply"
        apply_file = apply_root / relative
        apply_file.parent.mkdir(parents=True, exist_ok=True)
        apply_file.write_bytes(before)
        patch_file = self.root / "Changes.patch"
        patch_file.write_bytes(result["patch_text"].encode("utf-8"))
        options = ["--directory=Script", *(["--unidiff-zero"] if context == 0 else [])]
        checked = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check", *options, str(patch_file)], cwd=apply_root, capture_output=True)
        self.assertEqual(checked.returncode, 0, checked.stderr.decode(errors="replace"))
        applied = subprocess.run(["git", "-c", "core.autocrlf=false", "apply", *options, str(patch_file)], cwd=apply_root, capture_output=True)
        self.assertEqual(applied.returncode, 0, applied.stderr.decode(errors="replace"))
        self.assertEqual(apply_file.read_bytes(), after)
        self.assertEqual(original.read_bytes(), before)
        return result

    def test_deterministic_nested_utf8_patch_and_source_info(self):
        before = "class UTest {\n    int Marker() { return 10; }\n}\n".encode()
        after = "class UTest {\n    // Проверка\n    int Marker() { return 11; }\n}\n".encode()
        relative = "Script/Tests/Пример с пробелом.as"
        result = self.apply_exact(before, after, relative=relative)
        repeated = self.builder.build_script_patch(relative, after.decode(), result["source_sha256"])
        self.assertEqual(result, repeated)
        info = self.builder.source_info(relative)
        self.assertEqual(info["source_sha256"], result["source_sha256"])
        self.assertEqual(info["bytes"], len(before))
        self.assertEqual(info["revision"], "fixture-revision")
        self.assertNotIn(str(self.modkit), repr(info))

    def test_crlf_utf8_bom_and_missing_eof_newlines_reconstruct_exact_bytes(self):
        cases = (
            (b"a\r\nb\r\nc\r\n", b"a\r\nchanged\r\nc\r\n"),
            (b"a\nb", b"a\nchanged"),
            (b"a\nb", b"a\nb\n"),
            (b"a\nb\n", b"a\nb"),
            (b"a\nb", b"a\nb\nextra\n"),
            (b"a\r\nb", b"a\r\nchanged"),
            (b"\xef\xbb\xbfa\nb\n", b"\xef\xbb\xbfa\nchanged\n"),
            (b"a\r\nb\n", b"a\r\nchanged\n"),
            (b"a\nb\n", b"a\r\nb\r\n"),
            (b"first\nsecond\n", b"first\nsecond\n"),
        )
        for before, after in cases:
            with self.subTest(before=before, after=after):
                self.apply_exact(before, after)

    def test_insert_all_delete_all_and_zero_context_are_standard_diffs(self):
        for before, after, context in ((b"", b"new\n", 3), (b"", b"new", 3),
                                      (b"a\nb\n", b"", 3), (b"a\nb", b"", 3),
                                      (b"a\nb\nc\n", b"a\nnew\nc\n", 0)):
            with self.subTest(before=before, after=after):
                self.apply_exact(before, after, context=context)

    def test_stale_hash_and_invalid_inputs_never_change_source(self):
        source = self.write(b"original\n")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assert_error("conflict", lambda: self.builder.build_script_patch("Script/Test.as", "new\n", "0" * 64))
        for digest_argument in ("bad", None, True):
            self.assert_error("invalid_arguments", lambda: self.builder.build_script_patch("Script/Test.as", "new\n", digest_argument))
        for context in (-1, True, 1.5):
            self.assert_error("invalid_arguments", lambda: self.builder.build_script_patch("Script/Test.as", "new\n", digest, context))
        self.assert_error("invalid_arguments", lambda: self.builder.build_script_patch("Script/Test.as", 10, digest))
        self.assertEqual(source.read_bytes(), b"original\n")

    def test_strict_source_allowlist_and_path_boundary(self):
        self.write(b"original\n")
        self.write(b"private\n", "Examples/Sample/Script/Private.as")
        for path in ("../Script/Test.as", "C:/Script/Test.as", "/Script/Test.as", "Script/Test.as:private", "Script/CON.as", "Script/../Test.as"):
            self.assert_error("unsafe_path", lambda: self.builder.source_info(path))
        for path in ("Examples/Sample/Script/Private.as", "Script/Test.txt", "Documentation/Info.as", "script/Test.as"):
            self.assert_error("unsupported_file", lambda: self.builder.source_info(path))
        self.assert_error("access_error", lambda: self.builder.source_info("Script/Missing.as"))

    def test_invalid_utf8_binary_and_cr_are_rejected(self):
        self.write(b"valid\n")
        digest = hashlib.sha256(b"valid\n").hexdigest()
        self.assert_error("invalid_encoding", lambda: self.builder.build_script_patch("Script/Test.as", "\ud800", digest))
        self.assert_error("invalid_text", lambda: self.builder.build_script_patch("Script/Test.as", "a\0b", digest))
        self.assert_error("unsupported_line_endings", lambda: self.builder.build_script_patch("Script/Test.as", "a\rb", digest))
        for raw, code in ((b"\xff", "invalid_encoding"), (b"a\0b", "invalid_text"), (b"a\rb", "unsupported_line_endings")):
            self.write(raw)
            self.assert_error(code, lambda: self.builder.source_info("Script/Test.as"))

    def test_large_utf8_source_replacement_and_diff_reconstruct_and_reverse_exact_bytes(self):
        def script(character):
            lines = ["\ufeffclass ULargePatch {", *(
                f"    // Строка {index:04d} {character * 2048}" for index in range(1025)),
                "    int ReadMarker() { return 42; }", "}"]
            return "\r\n".join(lines).encode("utf-8")

        before, after = script("a"), script("b")
        self.assertGreater(len(before), 2 * 1024 * 1024)
        self.assertGreater(len(after), 2 * 1024 * 1024)
        result = self.apply_exact(before, after, context=64)
        self.assertGreater(result["patch_bytes"], 2 * 1024 * 1024)
        self.assertEqual(result["context_lines"], 64)
        self.assertIsNone(self.builder.source_info("Script/Test.as")["max_source_bytes"])
        reversed_patch = subprocess.run(
            ["git", "-c", "core.autocrlf=false", "apply", "--reverse", "--directory=Script", str(self.root / "Changes.patch")],
            cwd=self.root / "apply", capture_output=True)
        self.assertEqual(reversed_patch.returncode, 0, reversed_patch.stderr.decode(errors="replace"))
        self.assertEqual((self.root / "apply/Script/Test.as").read_bytes(), before)

    def test_explicit_context_larger_than_twenty_includes_requested_source_lines(self):
        before = "".join(f"line {index}\n" for index in range(100)).encode()
        after = before.replace(b"line 50\n", b"changed 50\n")
        result = self.apply_exact(before, after, context=40)
        self.assertIn(" line 10\n", result["patch_text"])
        self.assertIn(" line 90\n", result["patch_text"])
        self.assertNotIn(" line 9\n", result["patch_text"])
        self.assertNotIn(" line 91\n", result["patch_text"])

    def test_symlink_source_never_reads_outside_file(self):
        target = self.root / "Outside.as"
        target.write_text("private", encoding="utf-8")
        link = self.modkit / "Script/Linked.as"
        try:
            link.symlink_to(target)
        except OSError:
            self.skipTest("Система не разрешает изолированный symlink")
        self.assert_error("unsafe_path", lambda: self.builder.source_info("Script/Linked.as"))

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_junction_source_boundary(self):
        target = self.root / "Outside"
        target.mkdir()
        (target / "Private.as").write_text("private", encoding="utf-8")
        link = self.modkit / "Script/Linked"
        operation = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        if operation.returncode:
            self.skipTest("Система не разрешает изолированный junction")
        try:
            self.assert_error("unsafe_path", lambda: self.builder.source_info("Script/Linked/Private.as"))
        finally:
            link.rmdir()


if __name__ == "__main__":
    unittest.main()
