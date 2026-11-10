"""FVector fixture подготавливается из точных байтов без runtime-обещаний."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("vector_preparation_test", ROOT / "tools/prepare_vector_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from sn2_mcp.packages import PackageError


class VectorPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_direction_contract(self):
        with tempfile.TemporaryDirectory(prefix="vector-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 161)
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "EchoVector", "GetVector", "WriteVector", "ReadCallsCount", "ResetProbe"})
            self.assertEqual(methods["WriteVector"]["inputs"], [])
            self.assertEqual(methods["WriteVector"]["outputs"], [{"name":"Output","type":"FVector"}])
            self.assertEqual(methods["WriteVector"]["expected_out_parameters"], {"Output":{"X":1.25,"Y":-2.5,"Z":3.75}})
            self.assertNotIn(b"&inout", source)
            self.assertNotIn(b"&in ", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="vector-preparation-", dir=ROOT / "artifacts") as temp:
            first = prepare.prepare_probe(Path(temp))
            source = Path(first["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught: prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
