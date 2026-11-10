"""Prepare an isolated scalar-out fixture within artifacts without the game."""
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

PACKAGE_NAME = "G1RMCPScalarOutProbe"
SCRIPT_RELATIVE = "Script/G1RMCPScalarOutProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/scalar_out_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_scalar_out_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    outputs = [{"name": "OutBool", "type": "bool"},
               {"name": "OutInt", "type": "int"},
               {"name": "OutInt64", "type": "int64"},
               {"name": "OutUInt32", "type": "uint32"},
               {"name": "OutUInt64", "type": "uint64"},
               {"name": "OutFloat32", "type": "float32"},
               {"name": "OutFloat", "type": "float"}]
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPScalarOutProbe", "marker": 171,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "GetOutputs", "inputs": [], "outputs": outputs, "result": "bool",
             "expected_result": True, "increments": "CallsCount",
             "expected_out_parameters": {"OutBool": True, "OutInt": -7,
                 "OutInt64": -9007199254740993, "OutUInt32": 4294967295,
                 "OutUInt64": 18446744073709551615, "OutFloat32": 1.25, "OutFloat": -2.5}},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void"}],
        "syntax_sources": {
            "int_out": "Script/AI/States/FightAI/CombatState/Positioning/AICombat_Positioning.as:355",
            "float_out": "Script/AI/AssessmentResponseSystem/CrimeProcessingSubsystem/CrimeProcessingSubsystem.as:673",
            "float32_type": "Script/Difficulty/ProgressionDifficulty/ProgressionDifficulty.as:7,12",
            "uint32_type": "Script/AI/States/FightAI/CombatState/AIState_Combat_G1R.as:1267",
            "uint64_type": "Script/Story/Support/DialogImport.as:1",
            "wide_echo_precedent": "tests/fixtures/wide_integer_probe/Script/G1RMCPWideIntegerProbe.as"}}
    result["limitations"] = [
        "Seven pure-out types are an approved isolated runtime hypothesis, not a proven game API",
        "Preparation does not confirm compilation, CPF flags, or numeric out-parameter sizes",
        "For AS float, the expected DoubleProperty/8 must first be confirmed by runtime reflection",
        "AS double/float64, inout, and const-ref are not declared",
        "GetOutputs changes only CallsCount on its CDO; ResetProbe restores zero",
        "JSON wide-integer values are exact; the client must not convert them to float"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated scalar-out fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/scalar-out-probe-dev17",
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
