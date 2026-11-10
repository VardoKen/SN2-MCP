"""Verify copied snapshots and shared HTTP observation lifecycle on a selected game.

Requires an already loaded, enabled native bridge. Does not launch, load, stop,
or configure the game. The owned HTTP frontends and their native connections are
closed after the checks; request/result receipts are retained in the output root.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, contextmanager
import copy
import json
from pathlib import Path
import socket

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import uvicorn

from sn2_mcp.server import create_server


class OwnedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


@asynccontextmanager
async def frontend(args):
    server = create_server(args.workspace_root, args.native_port, args.game_root,
                           transport="streamable-http", runtime_root=args.runtime_root)
    app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                                     stateless_http=False, json_response=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    listener.setblocking(False)
    port = listener.getsockname()[1]
    worker = OwnedServer(uvicorn.Config(app, host="127.0.0.1", port=port,
                                       log_level="error", lifespan="on"))
    task = asyncio.create_task(worker.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(10):
            while not worker.started:
                if task.done():
                    await task
                    raise RuntimeError("HTTP frontend ended before readiness")
                await asyncio.sleep(0.02)
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        worker.should_exit = True
        try:
            async with asyncio.timeout(15):
                await task
        finally:
            listener.close()
        if not task.done():
            raise RuntimeError("Owned HTTP frontend shutdown was not confirmed")


@asynccontextmanager
async def client(url):
    async with httpx2.AsyncClient(timeout=20) as http:
        async with streamable_http_client(url, http_client=http,
                                          max_sse_event_size=None) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


class Receipts:
    def __init__(self, root):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.sequence = 0

    async def call(self, session, label, tool, arguments=None, *, error_code=None):
        response = await session.call_tool(tool, arguments or {})
        self.sequence += 1
        record = {"label": label, "tool": tool, "arguments": arguments or {},
                  "response": response.model_dump(mode="json")}
        (self.root / f"{self.sequence:03d}-{label}.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")
        if error_code is None:
            assert not response.is_error, (label, response.content)
            assert isinstance(response.structured_content, dict), label
            return response.structured_content
        assert response.is_error, (label, "Expected rejection")
        texts = [entry.text for entry in response.content if entry.type == "text"]
        assert texts, label
        message = texts[0]
        error = json.loads(message[message.index("{"):])
        assert error.get("code") == error_code, (label, error)
        return error


def load_snapshot(path):
    record = json.loads(path.read_text(encoding="utf-8"))
    return record["response"]["structured_content"]


async def comparisons(session, receipts, args):
    before = load_snapshot(args.snapshot_receipts / "0107.json")
    changed = load_snapshot(args.snapshot_receipts / "0087.json")["events"][0]["snapshot"]
    same = await receipts.call(session, "compare-identical", "sn2_snapshot_compare",
                               {"before": before, "after": copy.deepcopy(before)})
    assert same["comparable"] and same["equal_selected_values"] is True
    assert same["value_changes"] == same["unknown_count"] == 0
    different = await receipts.call(session, "compare-changed", "sn2_snapshot_compare",
                                    {"before": before, "after": changed})
    assert different["comparable"] and different["value_changes"] == 1
    assert different["equal_selected_values"] is False
    uncertain = copy.deepcopy(before)
    uncertain["source"] = "client_copy"
    unknown = await receipts.call(session, "compare-unknown-provenance", "sn2_snapshot_compare",
                                  {"before": before, "after": uncertain})
    assert unknown["unknown_count"] > 0 and unknown["equal_selected_values"] is None
    return before["objects"][0]["path"]


async def typed_checks(session, receipts, viewport):
    found = await receipts.call(session, "string-library", "sn2_find_objects",
                                 {"exact_path": "/Script/Engine.Default__KismetStringLibrary"})
    handle = found["items"][0]["handle"]
    descriptors = await receipts.call(session, "split-descriptor", "sn2_object_functions",
                                       {"handle": handle, "function_query": "Split"})
    function = next(item for item in descriptors["items"] if item["name"] == "Split")
    assert function["signature_status"] == "complete"
    result = await receipts.call(session, "split-unicode-output", "sn2_invoke",
                                  {"handle": handle, "function": "Split",
                                   "expected_signature_token": function["signature_token"],
                                   "arguments": {"SourceString": "alpha|名字", "InStr": "|",
                                                 "LeftS": {"out": True}, "RightS": {"out": True},
                                                 "SearchCase": 0, "SearchDir": 0}})
    assert result["result"] is True
    assert result["out_parameters"] == {"LeftS": "alpha", "RightS": "名字"}
    assert result["cleanup_confirmed"]
    meshes = await receipts.call(session, "mesh-components", "sn2_find_objects",
                                  {"class_query": "StaticMeshComponent", "query": "StaticMeshComponent0",
                                   "limit": 20})
    matching = [item for item in meshes["items"] if "L_ClientLobby" in item["path"]]
    assert matching, "A current lobby StaticMeshComponent is required for return checks"
    mesh = matching[0]["handle"]
    for name in ("GetMaterials", "GetMaterialSlotNames"):
        descriptors = await receipts.call(session, f"{name}-descriptor", "sn2_object_functions",
                                           {"handle": mesh, "function_query": name})
        function = next(item for item in descriptors["items"] if item["name"] == name)
        assert function["signature_status"] == "complete"
        result = await receipts.call(session, f"{name}-return", "sn2_invoke",
                                      {"handle": mesh, "function": name,
                                       "expected_signature_token": function["signature_token"]})
        assert result["executed"] and result["cleanup_confirmed"]
        assert result["result"]["total_count"] > 0 and not result["result"]["partial"]


async def run(args):
    receipts = Receipts(args.output)
    old_handle = old_watch = old_session = None
    async with frontend(args) as url:
        async with client(url) as second:
            viewport_path = await comparisons(second, receipts, args)
            processes = await receipts.call(second, "processes", "sn2_target_processes")
            selected = [item for item in processes["processes"] if item["pid"] == args.expected_pid]
            assert len(selected) == 1, "Select one exact current game process"
            status = await receipts.call(second, "native-status", "sn2_native_status",
                                          {"target_process": selected[0]})
            assert status["ready"] and status["executor_ready"]
            old_session = status["session_id"]
            found = await receipts.call(second, "viewport", "sn2_find_objects",
                                         {"exact_path": viewport_path})
            assert len(found["items"]) == 1
            old_handle = found["items"][0]["handle"]
            await typed_checks(second, receipts, old_handle)
            async with client(url) as first:
                watch = await receipts.call(first, "watch-start-first-client", "sn2_watch_start",
                                             {"handle": old_handle, "properties": ["MaxSplitscreenPlayers"],
                                              "duration_ms": 300000, "interval_ms": 50, "capacity": 3,
                                              "expected_session_id": old_session})
                old_watch = watch["watch_id"]
                read = await receipts.call(second, "watch-read-second-client", "sn2_watch_read",
                                            {"watch_id": old_watch, "expected_session_id": old_session})
                assert read["events"][0]["kind"] == "baseline"
            listing = await receipts.call(second, "watch-list-after-client-disconnect", "sn2_watch_list",
                                           {"expected_session_id": old_session})
            assert any(item["watch_id"] == old_watch and item["state"] == "running"
                       for item in listing["items"])
            resumed = await receipts.call(second, "watch-resume-other-owner", "sn2_watch_resume",
                                           {"watch_id": old_watch, "expected_session_id": old_session,
                                            "expected_client_owner": "another_http_client"})
            assert resumed["state"] == "running" and resumed["watch_id"] == old_watch
            stopped = await receipts.call(second, "watch-stop", "sn2_watch_stop",
                                           {"watch_id": old_watch, "expected_session_id": old_session})
            assert stopped["cleanup_complete"] and stopped["state"] == "stopped"
            terminal = await receipts.call(second, "watch-terminal", "sn2_watch_read",
                                            {"watch_id": old_watch, "expected_session_id": old_session})
            assert any(event["kind"] == "terminal" for event in terminal["events"])
            forgotten = await receipts.call(second, "watch-forget", "sn2_watch_forget",
                                             {"watch_id": old_watch, "expected_session_id": old_session})
            assert forgotten["forgotten"] and forgotten["cleanup_complete"]
            active = await receipts.call(second, "watch-active-at-shutdown", "sn2_watch_start",
                                         {"handle": old_handle, "properties": ["MaxSplitscreenPlayers"],
                                          "duration_ms": 300000, "interval_ms": 50, "capacity": 3})
            old_watch = active["watch_id"]
    async with frontend(args) as url:
        async with client(url) as fresh:
            status = await receipts.call(fresh, "reconnected-status", "sn2_native_status",
                                          {"target_process": selected[0]})
            assert status["session_id"] != old_session and status["ready"]
            await receipts.call(fresh, "old-handle-rejected", "sn2_object_properties",
                                {"handle": old_handle}, error_code="stale_handle")
            await receipts.call(fresh, "old-watch-session-rejected", "sn2_watch_resume",
                                {"watch_id": old_watch, "expected_session_id": old_session},
                                error_code="native_session_mismatch")
            listing = await receipts.call(fresh, "reconnected-watch-list", "sn2_watch_list")
            assert not listing["items"]
            found = await receipts.call(fresh, "fresh-handle", "sn2_find_objects",
                                         {"exact_path": viewport_path})
            assert len(found["items"]) == 1 and found["items"][0]["handle"] != old_handle
    summary = {"passed": True, "expected_pid": args.expected_pid,
               "first_native_session": old_session, "second_native_session": status["session_id"],
               "receipts": receipts.sequence, "owned_http_frontends_closed": True,
               "game_launch_load_stop": False}
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--snapshot-receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for name in ("workspace_root", "runtime_root", "game_root", "snapshot_receipts", "output"):
        setattr(args, name, getattr(args, name).resolve())
    asyncio.run(run(args))
