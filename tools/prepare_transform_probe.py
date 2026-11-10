"""Prepare an isolated FTransform input fixture without launching the game."""
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

PACKAGE_NAME = "G1RMCPTransformProbe"
SCRIPT_RELATIVE = "Script/G1RMCPTransformProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/transform_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_transform_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    zero = {"X": 0.0, "Y": 0.0, "Z": 0.0}
    one = {"X": 1.0, "Y": 1.0, "Z": 1.0}
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPTransformProbe", "marker": 191,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0},
                       {"name": "StoredTransform", "source_type": "FTransform", "initial_value": "identity"}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "AcceptTransform", "inputs": [{"name": "Input", "type": "FTransform"}],
             "result": "bool", "expected_result": True, "increments": "CallsCount"},
            {"name": "GetTranslation", "inputs": [], "result": "FVector", "initial_result": zero},
            {"name": "GetScale3D", "inputs": [], "result": "FVector", "initial_result": one},
            {"name": "GetRotationEuler", "inputs": [], "result": "FVector", "initial_result": zero,
             "components": {"X": "Pitch", "Y": "Yaw", "Z": "Roll"}},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void",
             "resets": ["CallsCount", "StoredTransform"]}],
        "rotation_probe": {"source_quaternion": {"X": 0.0, "Y": 0.0, "Z": 0.7071067811865476, "W": 0.7071067811865476},
                           "expected_euler": {"X": 0.0, "Y": 90.0, "Z": 0.0}, "absolute_tolerance": 0.0001},
        "syntax_sources": {
            "transform_constructor": "Script/IngameTools/IngameTool_SelectedActorProperties.as:323",
            "translation_getter": "Script/IngameTools/IngameTool_SelectedActorProperties.as:292",
            "scale_getter": "Script/IngameTools/IngameTool_SelectedActorProperties.as:294",
            "rotator_getter": "Script/IngameTools/IngameTool_SelectedActorProperties.as:293",
            "rotator_components_as_vector": "Script/IngameTools/IngameTool_SelectedActorProperties.as:305",
            "ufunction_value_struct_input": "Script/GAS/GASCharacterMixins.as:1363-1364"}}
    result["limitations"] = [
        "An FTransform input in UFUNCTION is an approved runtime composition hypothesis based on current AS examples",
        "Preparation does not confirm compilation, CPF flags, or the FTransform/FQuat ABI",
        "Input verification uses independent AS getters; FTransform return/out and mutable references are not included",
        "AcceptTransform changes only StoredTransform and CallsCount on the fixture CDO; ResetProbe resets both",
        "Game objects, the world, and saved games are not used"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated FTransform input fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/transform-probe-dev19",
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
