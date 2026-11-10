"""Prepare an isolated const FString input fixture without launching the game."""
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

PACKAGE_NAME = "G1RMCPStringInputProbe"
SCRIPT_RELATIVE = "Script/G1RMCPStringInputProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests/fixtures/string_input_probe"
TEXT_CASES = ("", "Bridge", "Gothic", "A😀B")


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_g1r_string_input_preparation", REPOSITORY_ROOT / "tools/prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPStringInputProbe", "marker": 211,
        "properties": [{"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [
            {"name": "ReadMarker", "inputs": [], "result": "int"},
            {"name": "ReadInputLength", "inputs": [{"name": "Input", "type": "const FString&in"}],
             "result": "int", "increments": "CallsCount"},
            {"name": "ReadEcho", "inputs": [{"name": "Input", "type": "const FString&in"}],
             "result": "FString", "increments": "CallsCount"},
            {"name": "ConcatInputs", "inputs": [{"name": "Left", "type": "const FString&in"},
                                                 {"name": "Right", "type": "const FString&in"}],
             "result": "FString", "increments": "CallsCount"},
            {"name": "ReadCallsCount", "inputs": [], "result": "int"},
            {"name": "ResetProbe", "inputs": [], "result": "void", "resets": ["CallsCount"]}],
        "text_cases": [{"input": value, "utf16_units": len(value.encode("utf-16-le")) // 2,
                        "echo_result": value} for value in TEXT_CASES],
        "concat_case": {"left": "Gothic ", "right": "😀", "expected_result": "Gothic 😀",
                        "utf16_units": len("Gothic 😀".encode("utf-16-le")) // 2},
        "syntax_sources": {
            "const_string_input": "Script/AI/States/FightAI/WaitStates/AIState_WaitForBlockedPath.as:274,290,318",
            "ufunction_const_struct_in": "Script/AI/CharacterAI_Gothic.as:1292",
            "string_concatenation": "Script/AI/AssessmentResponseSystem/EventResponses.as:168",
            "string_length_api": "https://alkimiainteractive.github.io/G1R-Modkit/script-reference/class_f_string.html#a084daa1edcc4d58991ca7dcbb09f42df",
            "string_add_api": "https://alkimiainteractive.github.io/G1R-Modkit/script-reference/class_f_string.html"}}
    result["limitations"] = [
        "Const FString&in in UFUNCTION is an approved runtime composition hypothesis based on current AS examples",
        "Preparation does not confirm compilation, CPF flags, the FString ABI, or string memory ownership",
        "Expected length is calculated in UTF-16 units; the actual Input.Len() requires runtime verification",
        "ReadEcho and ConcatInputs return strings; separate allocator-ledger verification is required for cleanup",
        "By-value inputs, pure out, and mutable references are not included",
        "Only CallsCount on the fixture CDO changes; inputs are not changed or retained",
        "Game objects, the world, and saved games are not used"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated const FString input fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts/string-input-probe-dev21",
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
