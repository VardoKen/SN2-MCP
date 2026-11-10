"""Prepare an FName fixture within artifacts without launching the game or DLL."""
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

PACKAGE_NAME = "G1RMCPNameProbe"
SCRIPT_RELATIVE = "Script/G1RMCPNameProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "name_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    # Reuse bounded preparation without changing another tool's globals.
    spec = importlib.util.spec_from_file_location("_g1r_name_preparation", REPOSITORY_ROOT / "tools" / "prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPNameProbe", "marker": 71,
        "properties": [{"name": "StoredEntry", "source_type": "FName", "expected_text": "Entry"},
                       {"name": "StoredNone", "source_type": "FName", "expected_text": "None"},
                       {"name": "CallsCount", "source_type": "int", "initial_value": 0}],
        "methods": [{"name": "ReadMarker", "inputs": [], "result": "int"},
                    {"name": "ReadEntry", "inputs": [], "result": "FName", "expected_text": "Entry"},
                    {"name": "ReadNone", "inputs": [], "result": "FName", "expected_text": "None"},
                    {"name": "ReadText", "inputs": [], "result": "FString", "expected_text": "G1R MCP dev7"},
                    {"name": "ReadEmptyText", "inputs": [], "result": "FString", "expected_text": ""},
                    {"name": "EchoName", "inputs": [{"name": "Input", "type": "FName"}], "result": "FName",
                     "increments": "CallsCount"},
                    {"name": "ReadCallsCount", "inputs": [], "result": "int"},
                    {"name": "ResetProbe", "inputs": [], "result": "void"}],
    }
    result["limitations"] = ["Preparation does not prove compilation, class loading, or runtime FName values",
                             "FName inputs are copied from verified NameProperty values on the fixture; FString inputs are not used",
                             "EchoName changes only CallsCount on the fixture CDO; ResetProbe restores it to zero"]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare an isolated FName fixture within artifacts")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts" / "name-probe-dev7")
    args = parser.parse_args()
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
