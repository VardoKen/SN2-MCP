"""One-shot native route check and confirmed read-only AngelScript call."""

import argparse
import json
import sys
import time

from sn2_mcp.native_client import NativeClient, NativeError


def main():
    class RussianParser(argparse.ArgumentParser):
        def format_help(self):
            return super().format_help().replace("usage: ", "Usage: ", 1).replace("options:", "options:")

        def error(self, message):
            self.exit(2, "Invalid arguments; use --help\n")

    parser = RussianParser(description="Check the internal G1R MCP native bridge", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--enable", action="store_true", help="Install the Tick hook after the main menu appears")
    parser.add_argument("--call-verified-as", action="store_true", help="One CM_Attack.ShouldInvalidateAttack call; the source returns false")
    arguments = parser.parse_args()
    client = NativeClient()
    try:
        if arguments.enable:
            print(json.dumps({"enable": client.request("enable_game", {})}, ensure_ascii=False))
        status = client.status()
        deadline = time.monotonic() + 5
        while arguments.enable and status.get("hook_installed") and not status.get("ready") and time.monotonic() < deadline:
            time.sleep(0.1)
            status = client.status()
        print(json.dumps({"status": status}, ensure_ascii=False))
        if not status.get("ready"):
            print(json.dumps({"code": "game_not_ready", "message": "Game Tick queue is not yet confirmed"}, ensure_ascii=False))
            return 2
        found = client.request("objects", {"query": "Default__CM_Attack", "class_query": "CM_Attack", "limit": 50})
        target = next((item for item in found.get("items", []) if item["name"] == "Default__CM_Attack"), None)
        if target is None:
            print(json.dumps({"code": "probe_object_absent", "message": "Probe object was not found"}, ensure_ascii=False))
            return 2
        print(json.dumps({"object": target}, ensure_ascii=False))
        if arguments.call_verified_as:
            result = client.request("invoke", {"handle": target["handle"], "function": "ShouldInvalidateAttack", "arguments": {}})
            print(json.dumps({"angelscript": result}, ensure_ascii=False))
            return 0 if result.get("executed") and result.get("result") is False else 3
        return 0
    except NativeError as error:
        print(json.dumps({"code": error.code, "message": str(error), "may_have_executed": error.may_have_executed}, ensure_ascii=False))
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
