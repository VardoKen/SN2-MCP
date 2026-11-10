"""Prepare an isolated const TArray<UObject> input fixture without launching the game."""
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

PACKAGE_NAME = "G1RMCPObjectArrayInputProbe"
SCRIPT_RELATIVE = "Script/G1RMCPObjectArrayInputProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/object_array_input_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_object_array_input_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    inputs = [{"name": "Input", "type": "const TArray<UObject>&in"}]
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPObjectArrayInputProbe", "marker": 221,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "ReadCount", "inputs": inputs, "result": "int", "increments": "CallsCount"},
            {"name": "ReadNullCount", "inputs": inputs, "result": "int", "increments": "CallsCount"},
            {"name": "ContainsSelf", "inputs": inputs, "result": "bool", "increments": "CallsCount"},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void", "resets": ["CallsCount"]}],
        "array_cases": [
            {"elements": [], "count": 0, "null_count": 0, "contains_self": False},
            {"elements": [None], "count": 1, "null_count": 1, "contains_self": False},
            {"elements": ["self"], "count": 1, "null_count": 0, "contains_self": True},
            {"elements": ["self", None, "self"], "count": 3, "null_count": 1, "contains_self": True}],
        "syntax_sources": {
            "ufunction_const_array_in": "Script/AI/States/FightAI/AIGroup_ConflictInstance.as:880-881",
            "const_array_num_and_index": "Script/AI/States/FightAI/AIGroup_ConflictInstance.as:888-889",
            "array_loop_and_index": "Script/AI/States/FightAI/AIGroup_ConflictInstance.as:674-676",
            "self_object_comparison": "Script/AI/States/FightAI/CombatState/AIState_Combat.as:582",
            "indexed_object_null_comparison": "Script/Gameplay/Magic/Components/Creatures/Demon/DemonAreaRegisterComponent.as:21"}}
    result["limitations"] = [
        "Const TArray<UObject>&in in UFUNCTION is an approved runtime composition hypothesis based on current AS examples",
        "Preparation does not confirm compilation, CPF flags, the ArrayProperty/ObjectProperty ABI, or array memory ownership",
        "self in the expected examples denotes only this fixture's CDO; a runtime descriptor is required separately",
        "The input array and objects are not changed, dereferenced, or retained",
        "Array return/out, by-value inputs, and mutable references are not included",
        "Only CallsCount on the fixture CDO changes; ResetProbe resets the counter",
        "World game objects, spawning, and saved games are not used"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated const TArray<UObject> input fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/object-array-input-probe-dev22",
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
