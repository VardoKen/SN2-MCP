"""Подготовка FLinearColor сохраняет источники и границу runtime-проверки."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("linear_color_preparation_test", ROOT / "tools/prepare_linear_color_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from sn2_mcp.packages import PackageError


class LinearColorPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_color_input_return(self):
        with tempfile.TemporaryDirectory(prefix="linear-color-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 231)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "ReadColorR", "EchoColor", "MakeStoredColor",
                                            "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["ReadColorR"]["inputs"], [{"name": "Input", "type": "FLinearColor"}])
            self.assertEqual(methods["ReadColorR"]["result"], "float")
            self.assertEqual(methods["EchoColor"]["result"], "FLinearColor")
            self.assertEqual(methods["MakeStoredColor"]["expected_result"], {"R": 0.25, "G": -0.5, "B": 2.0, "A": 0.125})
            self.assertIn(b"return Input.R;", source)
            self.assertIn(b"return FLinearColor(0.25f, -0.5f, 2.0f, 0.125f);", source)
            self.assertNotIn(b"&in", source)
            self.assertNotIn(b"&out", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="linear-color-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught:
                prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
