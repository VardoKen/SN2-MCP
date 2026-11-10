"""Точная подготовка scalar-out гипотезы не подтверждает runtime-контракт."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("scalar_out_preparation_test", ROOT / "tools/prepare_scalar_out_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from g1r_mcp.packages import PackageError


class ScalarOutPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_seven_outputs(self):
        with tempfile.TemporaryDirectory(prefix="scalar-out-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 171)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "GetOutputs", "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["GetOutputs"]["inputs"], [])
            self.assertEqual(len(methods["GetOutputs"]["outputs"]), 7)
            out = methods["GetOutputs"]["expected_out_parameters"]
            self.assertEqual(out["OutInt64"], -9007199254740993)
            self.assertEqual(out["OutUInt64"], 18446744073709551615)
            self.assertIs(type(out["OutInt64"]), int)
            self.assertIs(type(out["OutUInt64"]), int)
            self.assertTrue(out["OutBool"])
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&in ", source)
            self.assertNotIn(b"double", source)
            self.assertNotIn(b"float64", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="scalar-out-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught: prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
