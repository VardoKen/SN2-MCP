"""Подготовка const FTransform-входа сохраняет источники и границу runtime-проверки."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("const_transform_preparation_test", ROOT / "tools/prepare_const_transform_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from sn2_mcp.packages import PackageError


class ConstTransformPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_const_input_with_independent_getters(self):
        with tempfile.TemporaryDirectory(prefix="const-transform-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 201)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "AcceptConstTransform", "GetTranslation", "GetScale3D",
                                            "GetRotationEuler", "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["AcceptConstTransform"]["inputs"], [{"name": "Input", "type": "const FTransform&in"}])
            self.assertEqual(methods["AcceptConstTransform"]["result"], "bool")
            self.assertEqual(methods["GetScale3D"]["initial_result"], {"X": 1.0, "Y": 1.0, "Z": 1.0})
            self.assertEqual(methods["GetRotationEuler"]["components"], {"X": "Pitch", "Y": "Yaw", "Z": "Roll"})
            self.assertEqual(methods["ResetProbe"]["resets"], ["CallsCount", "StoredTransform"])
            self.assertIn(b"bool AcceptConstTransform(const FTransform&in Input)", source)
            self.assertIn(b"StoredTransform.Rotator()", source)
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&out", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="const-transform-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught:
                prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
