"""Prepare a lifecycle fixture within artifacts without launching the game or DLL."""
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

PACKAGE_NAME = "G1RMCPLifecycleProbe"
SCRIPT_RELATIVE = "Script/G1RMCPLifecycleProbe.as"
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "fixtures" / "lifecycle_probe"


def prepare_probe(workspace_root: Path, *, repository_root: Path = REPOSITORY_ROOT) -> dict:
    spec = importlib.util.spec_from_file_location("_g1r_lifecycle_preparation", REPOSITORY_ROOT / "tools" / "prepare_class_probe.py")
    implementation = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(implementation)
    implementation.PACKAGE_NAME = PACKAGE_NAME
    implementation.SCRIPT_RELATIVE = SCRIPT_RELATIVE
    implementation.FIXTURE_ROOT = FIXTURE_ROOT
    result = implementation.prepare_probe(workspace_root, repository_root=repository_root)
    result["startup_parameters"]["IngameTools"] = True
    result["expected"] = {
        "verification": "unverified_until_automatic_callback_observed",
        "tool_class": "G1RMCPIngameLifecycleProbe", "tool_marker": 91,
        "tool_count": "InitializeCount", "tool_method": "ReadInitializeCount", "tool_cdo_count": 0,
        "world_lifecycle": "not_tested_runtime_callback_signature_rejected",
    }
    result["limitations"] = [
        "The MCP probe does not call Initialize manually",
        "Preparation does not prove live instance creation or callback execution",
        "The WorldSubsystem fixture was removed after the current game compiler rejected BlueprintOverride; world lifecycle is not tested",
        "The world, a new game, and saved games are not loaded; the tool's own window is closed",
    ]
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare lifecycle counters in an isolated package")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT / "artifacts" / "lifecycle-probe-dev9")
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
