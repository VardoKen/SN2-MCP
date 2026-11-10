"""Prepare an isolated AS wrapper for FHitResult without launching the game."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from sn2_mcp.packages import PackageError
from sn2_mcp.workspace import WorkspaceError

PACKAGE_NAME = "G1RMCPHitResultWrapperProbe"
SCRIPT_RELATIVE = "Script/G1RMCPHitResultWrapperProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/hit_result_wrapper_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_hit_result_wrapper_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_class": "G1RMCPHitResultWrapperProbe", "marker": 251,
        "host_selection": "live_non_cdo_instance",
        "source_base_class": "UScriptWorldSubsystem", "automatic_instance_verified": False,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "HasWorld", "inputs": [], "result": "bool", "increments": None,
             "checks": "GetWorld() != nullptr"},
            {"name": "TraceImpactPoint", "inputs": [{"name": "Start", "type": "FVector"},
                                                     {"name": "End", "type": "FVector"},
                                                     {"name": "ActorsToIgnore", "type": "const TArray<AActor>&in"}],
             "outputs": [{"name": "ImpactPoint", "type": "FVector&out"}],
             "result": "bool", "increments": "CallsCount", "world_context_verified": False,
             "requires": "HasWorld", "no_world_behavior": "false_zero_without_trace"},
            {"name": "ReadDefaultImpactPoint", "inputs": [], "result": "FVector",
             "result_validation": "finite_xyz", "record_observed_result": True, "increments": "CallsCount",
             "calls_trace": False},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void", "resets": ["CallsCount"]}],
        "trace_settings": {"collision_channel": "ECollisionChannel::ECC_Visibility",
                           "trace_type_conversion": "UCollisionProfile::ConvertToTraceType",
                           "trace_complex": False, "draw_debug": "EDrawDebugTrace::None", "ignore_self": True,
                           "miss_impact_point": {"X": 0.0, "Y": 0.0, "Z": 0.0}},
        "syntax_sources": {
            "uobject_get_world_api": "https://alkimiainteractive.github.io/G1R-Modkit/script-reference/class_u_object.html",
            "get_world_usage": "Script/CheatMode/Commands/ChangeTimeCommand.as:32",
            "world_subsystem_class": "Script/AI/States/FightAI/ManagedFightSubsystem.as:13",
            "own_world_subsystem_fixture": "tests/fixtures/world_lifecycle_probe/Script/G1RMCPWorldLifecycleProbe.as:3",
            "line_trace_single_arguments": "Script/AI/States/AIState_FollowCharacterAggressive.as:56-59",
            "visibility_trace_conversion": "Script/AI/States/AIState_FollowCharacterAggressive.as:47",
            "actor_array_declaration": "Script/AI/States/AIState_FollowCharacterAggressive.as:50",
            "ufunction_const_array_in": "Script/AI/States/FightAI/AIGroup_ConflictInstance.as:880-881",
            "ufunction_vector_out": "Script/AI/States/FightAI/CombatState/CombatMoves.as:1876-1877",
            "hit_result_default_constructor": "Script/Regions/Traits/GameplayEffect/Damage/Base/DamageTraits.as:29",
            "hit_result_impact_point": "Script/Gameplay/Magic/Components/Spell/Summon/SummonManagerComponent.as:53",
            "vector_zero": "Script/AI/States/FightAI/WatchFightState/AIState_WatchFight.as:245"}}
    result["limitations"] = [
        "TraceImpactPoint UFUNCTION is an approved runtime composition hypothesis based on current AS examples",
        "Preparation does not confirm compilation, CPF flags, creation of a live subsystem, its world context, or trace execution",
        "HasWorld reads GetWorld without changing the counter; when false, the probe must skip TraceImpactPoint",
        "Without a world, TraceImpactPoint returns false and ZeroVector before calling System::LineTraceSingle",
        "FHitResult is created and destroyed within AngelScript; its layout is not marshaled through MCP",
        "ReadDefaultImpactPoint creates a local FHitResult without tracing; finite XYZ are checked and the actual result is retained",
        "TraceImpactPoint queries world collision and returns FVector; actors are neither created nor changed",
        "ActorsToIgnore contains only runtime AActor descriptors; runtime type compatibility requires verification",
        "Select a live instance of the fixture UScriptWorldSubsystem and check HasWorld; a CDO does not prove the world context",
        "Only CallsCount on the fixture subsystem changes; ResetProbe resets the counter",
        "Installation, game launch, saved games, and user mods are outside the preparation scope"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated AS wrapper for FHitResult within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/hit-result-wrapper-world-probe-dev25",
                        help="A separate directory within artifacts")
    args = parser.parse_args(argv)
    try:
        result = prepare_probe(args.workspace_root)
    except (PackageError, WorkspaceError, OSError) as error:
        print(json.dumps({"code": getattr(error, "code", "access_error"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
