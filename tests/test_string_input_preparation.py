"""Подготовка FString сохраняет источники и ожидаемую длину в единицах UTF-16."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("string_input_preparation_test", ROOT / "tools/prepare_string_input_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from sn2_mcp.packages import PackageError


class StringInputPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_input_with_utf16_cases(self):
        with tempfile.TemporaryDirectory(prefix="string-input-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 211)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "ReadInputLength", "ReadEcho", "ConcatInputs",
                                            "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["ReadInputLength"]["inputs"], [{"name": "Input", "type": "const FString&in"}])
            self.assertEqual(methods["ReadEcho"]["result"], "FString")
            self.assertEqual(methods["ConcatInputs"]["inputs"], [{"name": "Left", "type": "const FString&in"},
                                                                {"name": "Right", "type": "const FString&in"}])
            cases = {item["input"]: item for item in result["expected"]["text_cases"]}
            self.assertEqual(cases[""]["utf16_units"], 0)
            self.assertEqual(cases["A😀B"]["utf16_units"], 4)
            self.assertEqual(result["expected"]["concat_case"]["utf16_units"], 9)
            self.assertIn(b"return Input.Len();", source)
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&out", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="string-input-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught:
                prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
