"""Prepare an isolated wide-integer fixture within artifacts without the game."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from g1r_mcp.packages import PackageError
from g1r_mcp.workspace import WorkspaceError

PACKAGE_NAME = "G1RMCPWideIntegerProbe"
SCRIPT_RELATIVE = "Script/G1RMCPWideIntegerProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/wide_integer_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_g1r_wide_integer_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPWideIntegerProbe", "marker": 151,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "EchoInt64", "inputs": [{"name": "Input", "type": "int64"}], "result": "int64", "increments": "CallsCount"},
            {"name": "EchoUInt64", "inputs": [{"name": "Input", "type": "uint64"}], "result": "uint64", "increments": "CallsCount"},
            {"name": "EchoUInt32", "inputs": [{"name": "Input", "type": "uint32"}], "result": "uint32", "increments": "CallsCount"},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void"}],
        "integer_boundaries": {
            "EchoInt64": [-9223372036854775808, -9007199254740993, 0, 9007199254740993, 9223372036854775807],
            "EchoUInt64": [0, 9007199254740993, 18446744073709551615],
            "EchoUInt32": [0, 2147483648, 4294967295]},
        "syntax_sources": {
            "uint64": "Script/Story/Support/DialogImport.as:1",
            "uint32": "Script/AI/States/FightAI/CombatState/AIState_Combat_G1R.as:1267",
            "int64": "Script/Story/G1R/Quest/Quest_ValleyOfMines_VoMCHAPTER4_ULUMULU_ULUMULU_OBJ_ZARR.as:25"}}
    result["limitations"] = [
        "Preparation does not confirm compilation or runtime marshaling of int64/uint64/uint32",
        "The exact Int64Property/UInt64Property/UInt32Property types and sizes must first be confirmed by reflection",
        "Echo changes only CallsCount on the fixture CDO; ResetProbe restores zero",
        "JSON represents integers exactly; client conversion to float is not allowed"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated wide-integer fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/wide-integer-probe-dev15",
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
