"""Record explicit native content/lifecycle probes in one retained session.

This utility does not launch, load, install or retry the game. Enter JSON objects
with op/params, or {"quit": true}. Each exact response or error is saved locally.
"""
import argparse
import json
from pathlib import Path
import sys

from sn2_mcp.native_client import NativeClient, NativeError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=27223)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with NativeClient(port=args.port, timeout=30) as client:
        sequence = 0
        print(json.dumps({"probe_ready": True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("quit"):
                break
            sequence += 1
            try:
                result = client.request(request["op"], request.get("params", {}))
                record = {"request": request, "result": result, "is_error": False}
            except NativeError as error:
                result = {"code": error.code, "message": error.message,
                          "may_have_executed": error.may_have_executed,
                          "request_id": error.request_id, "details": error.details}
                record = {"request": request, "error": result, "is_error": True}
            path = args.output / f"{sequence:04d}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            if request.get("fields") and not record["is_error"]:
                result = {key: result.get(key) for key in request["fields"]}
            print(json.dumps({"receipt": str(path), "is_error": record["is_error"],
                              "result": result}, ensure_ascii=False), flush=True)
    print(json.dumps({"probe_closed": True}), flush=True)


if __name__ == "__main__":
    main()
