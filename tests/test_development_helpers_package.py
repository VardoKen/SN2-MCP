"""Рабочий AS-пакет использует официальный формат и согласованный контракт без probe-состояния."""
import re
from pathlib import Path
import unittest

from sn2_mcp.packages import validate_package

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_RELATIVE = "development/helpers/G1RMCPDevelopmentHelpers"
SCRIPT = ROOT / PACKAGE_RELATIVE / "Script/G1RMCPDevelopmentHelpers.as"


class DevelopmentHelpersPackageTests(unittest.TestCase):
    def test_actual_package_validation_preserves_runtime_boundary(self):
        result = validate_package(ROOT, PACKAGE_RELATIVE)
        self.assertTrue(result["valid_structure"])
        self.assertEqual(result["metadata"]["name"], "G1RMCPDevelopmentHelpers")
        self.assertEqual(result["metadata"]["modVersion"], "1.0.0")
        self.assertEqual(result["script_count"], 1)
        self.assertIn("runtime_not_verified", {item["code"] for item in result["warnings"]})
        source = SCRIPT.read_text(encoding="utf-8")
        version = re.search(r'FString\s+GetHelperVersion\(\)\s*\{\s*return\s+"([^"]+)";', source)
        self.assertIsNotNone(version)
        self.assertEqual(version.group(1), "1.0.0-dev.49")

    def test_public_helper_surface_and_world_guard(self):
        source = SCRIPT.read_text(encoding="utf-8")
        methods = re.findall(r'UFUNCTION\(BlueprintCallable\)\s+(?:FString|bool|ETraceTypeQuery)\s+(\w+)\s*\(', source)
        self.assertEqual(set(methods), {"GetHelperVersion", "ReadText", "HasWorld", "HasGameplayPlayer", "GetVisibilityTraceChannel", "TraceImpactPoint"})
        self.assertIn("class UG1RMCPDevelopmentHelpers : UScriptWorldSubsystem", source)
        self.assertIn("FString ReadText(UTextBlock Widget)", source)
        gameplay_player = source.split("bool HasGameplayPlayer()", 1)[1].split("UFUNCTION(BlueprintCallable)", 1)[0]
        self.assertIn("if (GetWorld() == nullptr)", gameplay_player)
        self.assertLess(gameplay_player.index("if (GetWorld() == nullptr)"), gameplay_player.index("Gameplay::GetPlayerCharacter(0)"))
        self.assertIn("Cast<AGothicCharacter>(Gameplay::GetPlayerCharacter(0))", gameplay_player)
        self.assertIn("return Player != nullptr && Player.IsGameplayReady();", gameplay_player)
        self.assertIn("ETraceTypeQuery Channel", source)
        self.assertIn("UCollisionProfile::ConvertToTraceType(ECollisionChannel::ECC_Visibility)", source)
        self.assertIn("const TArray<AActor>&in ActorsToIgnore", source)
        self.assertIn("FVector&out ImpactPoint", source)
        self.assertLess(source.index("if (GetWorld() == nullptr)"), source.index("System::LineTraceSingle("))
        self.assertIn("EDrawDebugTrace::None", source)
        self.assertNotIn("UPROPERTY", source)
        self.assertNotIn("ResetProbe", source)
        self.assertNotIn("Spawn", source)


if __name__ == "__main__":
    unittest.main()
