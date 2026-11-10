"""Prepare an isolated enum fixture within artifacts without launching the game."""
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

PACKAGE_NAME = "G1RMCPEnumProbe"
SCRIPT_RELATIVE = "Script/G1RMCPEnumProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "enum_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_enum_preparation", REPOSITORY_ROOT / "tools" / "prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPEnumProbe", "marker": 81,
        "enum_name": "EG1RMCPEnumProbeMode", "declared_values": {"First": 0, "Second": 1, "Third": 7},
        "initial_properties": {"StoredMode": 0, "CallsCount": 0},
        "methods": [{"name": "ReadMarker", "inputs": [], "result": "int"},
                    {"name": "ReadMode", "inputs": [], "result": "EG1RMCPEnumProbeMode"},
                    {"name": "EchoMode", "inputs": [{"name": "Input", "type": "EG1RMCPEnumProbeMode"}],
                     "result": "EG1RMCPEnumProbeMode", "increments": "CallsCount"},
                    {"name": "StoreMode", "inputs": [{"name": "Input", "type": "EG1RMCPEnumProbeMode"}],
                     "result": "void", "increments": "CallsCount"},
                    {"name": "ResetProbe", "inputs": [], "result": "void"}],
    }
    result["limitations"] = ["Preparation does not prove compilation or the actual EnumProperty/UnderlyingProp",
                             "Calls change only StoredMode/CallsCount on the fixture CDO",
                             "Separate uint8 calls are not declared; the numeric enum size must be confirmed by runtime reflection"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated enum fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts" / "enum-probe-dev8",
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
    raise SystemExit(main())
