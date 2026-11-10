"""Prepare an isolated POD fixture within artifacts without launching the game."""
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

PACKAGE_NAME = "G1RMCPPODReadProbe"
SCRIPT_RELATIVE = "Script/G1RMCPPODReadProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "pod_read_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_sn2_pod_preparation", REPOSITORY_ROOT / "tools" / "prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["expected"] = {
        "verification": "unverified_until_runtime_reflection_and_calls",
        "host_cdo": "Default__G1RMCPPODReadProbe", "marker": 101,
        "initial_arrays": {"Numbers": [], "EmptyNumbers": [], "Names": ["Entry", "None"]},
        "filled_numbers": {"count": 205, "value_formula": "index * 3 - 7", "index_base": 0},
        "structures": {"StoredVector": {"X": 1.25, "Y": -2.5, "Z": 3.75},
                       "StoredRotation": {"Pitch": 10.0, "Yaw": -20.0, "Roll": 30.0}},
        "control_methods": ["ReadMarker", "ReadNumberCount", "ReadEmptyCount", "ReadNameCount", "ReadNumberAt",
                            "ReadNameAt", "ReadVectorX", "ReadVectorY", "ReadVectorZ", "ReadPitch", "ReadYaw", "ReadRoll"],
        "mutations": {"SetupFixture": "populate only Numbers on the fixture CDO",
                      "ClearFixture": "clear only Numbers on the fixture CDO"},
    }
    result["limitations"] = ["Preparation does not prove compilation or the actual FScriptArray/FVector/FRotator layout",
                             "Arrays are read in separate pages; atomicity across pages is not guaranteed",
                             "Fixtures do not create game objects, callbacks, content, or saved games"]
    return result


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            return super().format_help().replace("usage:", "Usage:", 1).replace("options:", "Options:", 1)
    parser = RussianArgumentParser(description="Prepare an isolated POD fixture within artifacts", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts" / "pod-read-probe-dev10",
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
