"""CLI for inspecting the explicitly selected game and Modkit; logic is available from the shared module."""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from sn2_mcp.game_environment import (
    InspectionError, InspectionOptions, inspect_environment, parse_manifest,
)


class JsonArgumentParser(argparse.ArgumentParser):
    """Do not expose supplied paths or values when arguments are invalid."""

    def format_help(self):
        return super().format_help().replace("usage: ", "Usage: ", 1)

    def error(self, message: str) -> None:
        raise InspectionError("invalid_arguments", "Invalid arguments; use --help")


def main(argv: list[str] | None = None) -> int:
    parser = JsonArgumentParser(
        description="Inspect the explicitly selected Steam installation, game, and Modkit without changing files",
        add_help=False, usage="%(prog)s [options]",
    )
    parser._optionals.title = "Options"
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--game-root", type=Path, required=True, help="Game installation directory")
    parser.add_argument("--modkit-root", type=Path, required=True, help="Official Modkit directory")
    parser.add_argument("--steam-manifest", type=Path, required=True, help="Specific Steam appmanifest .acf file")
    parser.add_argument("--hash-executable", action="store_true", help="Stream the SHA-256 calculation for the known game EXE")
    parser.add_argument("--max-list-entries", type=int, default=100, help="Maximum entries in each difference list; default: 100")
    try:
        result = inspect_environment(InspectionOptions(**vars(parser.parse_args(argv))))
    except InspectionError as error:
        result = {"ok": False, "error": {"code": error.code, "message": str(error)}}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
