"""AS-обёртка FHitResult сохраняет исходники и не объявляет runtime/world context подтверждёнными."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("hit_result_wrapper_preparation_test", ROOT / "tools/prepare_hit_result_wrapper_probe.py")
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
from g1r_mcp.packages import PackageError


class HitResultWrapperPreparationTests(unittest.TestCase):
    def test_exact_source_and_unverified_wrapper_context(self):
        with tempfile.TemporaryDirectory(prefix="hit-result-wrapper-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = (prepare.FIXTURE_ROOT / prepare.SCRIPT_RELATIVE).read_bytes()
            copied = (Path(result["package_path"]) / prepare.SCRIPT_RELATIVE).read_bytes()
            self.assertEqual(copied, source)
            self.assertEqual(result["files"][prepare.SCRIPT_RELATIVE]["sha256"], hashlib.sha256(source).hexdigest())
            self.assertFalse(result["compiled"])
            self.assertFalse(result["game_launched"])
            self.assertFalse(result["dll_loaded"])
            self.assertEqual(result["runtime_validation"], "not_performed")
            self.assertEqual(result["expected"]["marker"], 251)
            self.assertEqual(result["expected"]["host_selection"], "live_non_cdo_instance")
            self.assertEqual(result["expected"]["source_base_class"], "UScriptWorldSubsystem")
            self.assertFalse(result["expected"]["automatic_instance_verified"])
            self.assertNotIn("host_cdo", result["expected"])
            methods = {item["name"]: item for item in result["expected"]["methods"]}
            self.assertEqual(set(methods), {"ReadMarker", "HasWorld", "TraceImpactPoint", "ReadDefaultImpactPoint",
                                            "ReadCallsCount", "ResetProbe"})
            self.assertIsNone(methods["HasWorld"]["increments"])
            self.assertEqual(methods["HasWorld"]["result"], "bool")
            self.assertFalse(methods["TraceImpactPoint"]["world_context_verified"])
            self.assertEqual(methods["TraceImpactPoint"]["requires"], "HasWorld")
            self.assertEqual(methods["TraceImpactPoint"]["no_world_behavior"], "false_zero_without_trace")
            self.assertEqual(methods["TraceImpactPoint"]["outputs"], [{"name": "ImpactPoint", "type": "FVector&out"}])
            self.assertFalse(methods["ReadDefaultImpactPoint"]["calls_trace"])
            self.assertNotIn("expected_result", methods["ReadDefaultImpactPoint"])
            self.assertEqual(methods["ReadDefaultImpactPoint"]["result_validation"], "finite_xyz")
            self.assertTrue(methods["ReadDefaultImpactPoint"]["record_observed_result"])
            self.assertEqual(result["expected"]["trace_settings"]["miss_impact_point"], {"X": 0.0, "Y": 0.0, "Z": 0.0})
            self.assertEqual(result["expected"]["trace_settings"]["collision_channel"], "ECollisionChannel::ECC_Visibility")
            self.assertIn(b"FHitResult HitResult = FHitResult();", source)
            self.assertIn(b"class UG1RMCPHitResultWrapperProbe : UScriptWorldSubsystem", source)
            self.assertIn(b"UCollisionProfile::ConvertToTraceType(ECollisionChannel::ECC_Visibility)", source)
            self.assertLess(source.index(b"if (GetWorld() == nullptr)"), source.index(b"System::LineTraceSingle("))
            self.assertNotIn(b"FHitResult&out", source)
            self.assertNotIn(b"Spawn", source)

    def test_preparer_never_overwrites_existing_package(self):
        with tempfile.TemporaryDirectory(prefix="hit-result-wrapper-preparation-", dir=ROOT / "artifacts") as temp:
            result = prepare.prepare_probe(Path(temp))
            source = Path(result["package_path"]) / prepare.SCRIPT_RELATIVE
            before = source.read_bytes()
            with self.assertRaises(PackageError) as caught:
                prepare.prepare_probe(Path(temp))
            self.assertEqual(caught.exception.code, "already_exists")
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
