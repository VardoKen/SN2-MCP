"""Explicit MCP runtime probe session with retained request/result receipts.

Requires an already running frontend-compatible native target. Reads JSON probe
requests from stdin; never launches, loads or retries the game automatically.
Each request names an actual tool and arguments. Enter {"quit": true} to close
the frontend/native connection and finish the owned local processes.
"""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


async def run(args):
    workspace = args.workspace_root.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    parameters = StdioServerParameters(command=sys.executable, args=[
        "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace),
        "--runtime-root", str(args.runtime_root.resolve()), "--game-root", str(args.game_root.resolve()),
        "--native-port", str(args.native_port)], cwd=str(workspace))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            await client.initialize()
            catalog = await client.list_tools()
            (output / "catalog.json").write_text(catalog.model_dump_json(indent=2), encoding="utf-8")
            print(json.dumps({"probe_ready": True, "tools": len(catalog.tools)}), flush=True)
            sequence = 0
            while True:
                line = await asyncio.to_thread(sys.stdin.readline)
                if not line:
                    break
                request = json.loads(line)
                if request.get("quit"):
                    break
                sequence += 1
                response = await client.call_tool(request["tool"], request.get("arguments", {}))
                record = {"request": request, "response": response.model_dump(mode="json")}
                path = output / f"{sequence:04d}.json"
                path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
                result = response.structured_content
                if result is None:
                    result = [block.model_dump(mode="json") for block in response.content]
                fields = request.get("fields")
                if fields and isinstance(result, dict):
                    result = {key: result.get(key) for key in fields}
                print(json.dumps({"receipt": str(path), "is_error": response.is_error, "result": result},
                                 ensure_ascii=False), flush=True)
    print(json.dumps({"probe_closed": True}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run explicit MCP probes against a selected native session")
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
