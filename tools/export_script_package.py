"""Export or verify an official package through the shared implementation.

Sources are not changed. Compilation, installation, and patch application are not performed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from sn2_mcp.package_export import (ExportError, PackageError, WorkspaceError,
                                  _descriptor_metadata, _source_files,
                                  export_package, verify_export)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export or verify an official script package without installing it in the game")
    parser.add_argument("--verify-root", type=Path)
    parser.add_argument("--workspace-root", type=Path)
    parser.add_argument("--package")
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--modkit-root", type=Path)
    parser.add_argument("--game-root", type=Path)
    args = parser.parse_args()
    try:
        if args.verify_root:
            if any((args.workspace_root, args.package, args.destination, args.modkit_root, args.game_root)):
                parser.error("--verify-root must be used alone")
            result = verify_export(args.verify_root)
        else:
            if not all((args.workspace_root, args.package, args.destination, args.modkit_root, args.game_root)):
                parser.error("Export requires workspace-root, package, destination, modkit-root, and game-root")
            result = export_package(args.workspace_root, args.package, args.destination,
                                    modkit_root=args.modkit_root, game_root=args.game_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExportError, WorkspaceError, PackageError, OSError, UnicodeError) as error:
        print(json.dumps({"code": getattr(error, "code", "export_failed"), "message": (f"The file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
