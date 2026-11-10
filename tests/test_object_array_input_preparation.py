"""Подготовка массива UObject сохраняет источники и границу runtime-проверки."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("object_array_input_preparation_test", ROOT / "tools/prepare_object_array_input_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from g1r_mcp.packages import PackageError


class ObjectArrayInputPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_const_array_input(self):
        with tempfile.TemporaryDirectory(prefix="object-array-input-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 221)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "ReadCount", "ReadNullCount", "ContainsSelf",
                                            "ReadCallsCount", "ResetProbe"})
            for name in ("ReadCount", "ReadNullCount", "ContainsSelf"):
                self.assertEqual(methods[name]["inputs"], [{"name": "Input", "type": "const TArray<UObject>&in"}])
                self.assertEqual(methods[name]["increments"], "CallsCount")
            cases = result["expected"]["array_cases"]
            self.assertEqual(cases[0], {"elements": [], "count": 0, "null_count": 0, "contains_self": False})
            self.assertEqual(cases[-1], {"elements": ["self", None, "self"], "count": 3, "null_count": 1, "contains_self": True})
            self.assertIn(b"const TArray<UObject>&in Input", source)
            self.assertIn(b"Input[Index] == this", source)
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&out", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="object-array-input-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught:
                prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
