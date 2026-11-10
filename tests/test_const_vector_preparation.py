"""Подготовка const-vector ref-входа не подтверждает runtime CPF-флаги."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("const_vector_preparation_test", ROOT / "tools/prepare_const_vector_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from g1r_mcp.packages import PackageError


class ConstVectorPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_const_input(self):
        with tempfile.TemporaryDirectory(prefix="const-vector-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 181)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "EchoConstVector", "GetVector", "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["EchoConstVector"]["inputs"], [{"name":"Input","type":"const FVector&in"}])
            self.assertEqual(methods["EchoConstVector"]["result"], "FVector")
            self.assertEqual(methods["GetVector"]["expected_result"], {"X":1.25,"Y":-2.5,"Z":3.75})
            self.assertIn(b"const FVector&in Input", source)
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&out", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="const-vector-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught: prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
