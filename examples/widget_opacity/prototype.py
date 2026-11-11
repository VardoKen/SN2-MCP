"""Prototype a reversible UI modification with the existing native MCP tools.

This is a development client, not a standalone game mod or an official package.
It selects a live start-screen label from fresh reflection, records its interface,
applies two exact source revisions, diagnoses a rejected call, and restores the
original value. It never launches, loads, enables or closes the game.
"""

import argparse
import asyncio
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class PrototypeError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise PrototypeError(message)


async def run(args):
    root = args.workspace_root.resolve()
    output = args.output.resolve()
    require(not output.exists(), "Choose a new receipt directory")
    output.mkdir(parents=True)
    summary = {"kind": "widget_opacity_mod_prototype", "started_utc": datetime.now(timezone.utc).isoformat(),
               "standalone_mod": False, "game_launch": False, "automatic_retries": False,
               "restoration_confirmed": False, "revisions": []}
    parameters = StdioServerParameters(command=sys.executable, args=["-B", "-m", "sn2_mcp.server",
        "--workspace-root", str(root), "--game-root", str(args.game_root.resolve()),
        "--runtime-root", str(args.runtime_root.resolve())], cwd=str(root))
    sequence = 0
    async with stdio_client(parameters) as (incoming, outgoing), ClientSession(incoming, outgoing) as client:
        await client.initialize()

        async def tool(name, arguments, *, rejection=None):
            nonlocal sequence
            sequence += 1
            value = await client.call_tool(name, arguments)
            (output / f"{sequence:04d}.json").write_text(json.dumps({"request": {"tool": name, "arguments": arguments},
                "response": value.model_dump(mode="json")}, ensure_ascii=False, indent=2), encoding="utf-8")
            if rejection is not None:
                require(value.is_error, "The intentional invalid call was accepted")
                text = " ".join(block.text for block in value.content if block.type == "text")
                require(rejection in text, "The rejection was not attributable to the selected precondition")
                if name == "sn2_invoke":
                    error = json.loads(text[text.index("{"):])
                    require(error.get("may_have_executed") is False and error.get("effect", {}).get("executed") is False,
                            "The rejected call has an uncertain execution effect")
                return text
            require(not value.is_error, f"{name} failed; inspect receipt {sequence:04d}")
            require(isinstance(value.structured_content, dict), "The tool did not return structured evidence")
            return value.structured_content

        processes = await tool("sn2_target_processes", {})
        selected = [p for p in processes["processes"] if p["pid"] == args.expected_pid
                    and p["creation_time"] == args.expected_creation_filetime]
        require(len(selected) == 1, "The selected game identity changed")
        state = await tool("sn2_native_status", {"target_process": selected[0]})
        require(state.get("executor_ready") is True, "The game Tick executor is not ready")
        summary["native_status"] = state
        found = await tool("sn2_find_objects", {"query": "TextLabel", "class_query": "TextBlock", "limit": 200})
        candidates = [item for item in found["items"] if "/Engine/Transient." in item.get("path", "")
                      and "WBP_StartScreen_PressStart_C_" in item["path"] and ".ContinueBtn." in item["path"]]
        require(len(candidates) == 1, "No unique live start-screen Continue label is available")
        target = candidates[0]
        summary["target"] = target
        handle = target["handle"]
        functions = await tool("sn2_object_functions", {"handle": handle, "function_query": "RenderOpacity", "limit": 100})
        signatures = {item["name"]: item for item in functions["items"]}
        for name in ("SetRenderOpacity", "GetRenderOpacity"):
            require(signatures.get(name, {}).get("signature_status") == "complete", "The required widget signature is unsupported")

        async def getter():
            value = await tool("sn2_invoke", {"handle": handle, "function": "GetRenderOpacity", "arguments": {},
                "expected_signature_token": signatures["GetRenderOpacity"]["signature_token"]})
            require(value.get("executed") is True and value.get("process_event_returned") is True,
                    "The getter did not return")
            return value["result"]

        async def setter(value):
            result = await tool("sn2_invoke", {"handle": handle, "function": "SetRenderOpacity", "arguments": {"InOpacity": value},
                "expected_signature_token": signatures["SetRenderOpacity"]["signature_token"]})
            require(result.get("executed") is True and result.get("process_event_returned") is True,
                    "The setter did not return; inspect actual state before another action")

        baseline = await getter()
        summary["baseline"] = baseline
        before = await tool("sn2_snapshot", {"targets": [{"handle": handle, "properties": ["RenderOpacity"]}]})
        watch = await tool("sn2_watch_start", {"handle": handle, "properties": ["RenderOpacity"],
            "duration_ms": 60000, "interval_ms": 20, "capacity": 32})
        source_directory = args.source_directory
        await tool("sn2_create_source_directory", {"directory_path": source_directory})
        source_path = source_directory + "/opacity_revision.py"
        revision_source = (
            '"""UI modification prototype; invoke only against the inspected live widget."""\n'
            'OPACITY = {opacity}\n'
            'async def apply(client, handle, signature_token):\n'
            '    return await client.call_tool("sn2_invoke", {{"handle": handle,\n'
            '        "function": "SetRenderOpacity", "arguments": {{"InOpacity": OPACITY}},\n'
            '        "expected_signature_token": signature_token}})\n'
        )
        effect_started = False
        try:
            digest = None
            for number, opacity in enumerate((0.5, 0.75), 1):
                written = await tool("sn2_write_source", {"file_path": source_path,
                    "content": revision_source.format(opacity=opacity), "expected_sha256": digest})
                digest = written["sha256"]
                exact = await tool("sn2_read_source_exact", {"file_path": source_path})
                require(exact["sha256"] == digest, "The authoring revision changed before execution")
                recipe = await tool("sn2_development_recipe", {"source_files": [source_path],
                    "targets": [{"handle": handle, "properties": ["RenderOpacity"],
                                 "functions": ["SetRenderOpacity", "GetRenderOpacity"]}]})
                require(recipe["source_stable"], "The recipe source revision changed")
                require(recipe["runtime_identity_stable"], "Runtime identity changed while capturing the recipe")
                exported = await tool("sn2_export_sources", {"source_files": [source_path],
                    "destination": f"artifacts/widget-source-revision-{number}"})
                verified_export = await tool("sn2_verify_source_export", {
                    "relative_path": f"artifacts/widget-source-revision-{number}"})
                require(verified_export["verified_files"], "The source archive did not verify")
                if number == 1:
                    await tool("sn2_write_source", {"file_path": source_path, "content": "stale revision",
                        "expected_sha256": "0" * 64}, rejection="conflict")
                    await tool("sn2_invoke", {"handle": handle, "function": "SetRenderOpacity",
                        "arguments": {"InOpacity": "invalid float"},
                        "expected_signature_token": signatures["SetRenderOpacity"]["signature_token"]},
                        rejection="invalid_argument")
                    require(math.isclose(await getter(), baseline), "The rejected call changed the baseline")
                # Execute the exact development source locally with the inherited
                # Python client stack. The server is not an arbitrary-code host.
                namespace = {}
                exec(compile(exact["text"], source_path, "exec"), namespace)
                effect_started = True
                response = await namespace["apply"](client, handle, signatures["SetRenderOpacity"]["signature_token"])
                sequence += 1
                (output / f"{sequence:04d}.json").write_text(json.dumps({"source_path": source_path, "source_sha256": digest,
                    "response": response.model_dump(mode="json")}, ensure_ascii=False, indent=2), encoding="utf-8")
                require(not response.is_error and response.structured_content.get("process_event_returned") is True,
                        "The prototype source call did not return")
                require(math.isclose(await getter(), opacity), "The independent getter did not observe the revision")
                after = await tool("sn2_snapshot", {"targets": [{"handle": handle, "properties": ["RenderOpacity"]}]})
                comparison = await tool("sn2_snapshot_compare", {"before": before, "after": after})
                require(comparison.get("comparable") is True and comparison.get("value_changes") == 1
                        and comparison.get("unknown_count") == 0, "The snapshot did not establish the selected change")
                summary["revisions"].append({"revision": number, "source_sha256": digest,
                    "opacity": opacity, "recipe": recipe, "source_export": exported,
                    "snapshot_comparison": comparison})
                before = after
            summary["watch_events"] = await tool("sn2_watch_read", {"watch_id": watch["watch_id"], "limit": 32})
            events = summary["watch_events"]["events"]
            observed_values = [event["snapshot"]["objects"][0]["properties"][0]["value"]
                               for event in events if event.get("kind") in ("baseline", "changed")]
            require(observed_values[:3] == [baseline, 0.5, 0.75], "The Tick watch did not observe both revisions")
        finally:
            if effect_started:
                await setter(baseline)
                summary["restoration_confirmed"] = math.isclose(await getter(), baseline)
            await tool("sn2_watch_stop", {"watch_id": watch["watch_id"]})
            await tool("sn2_watch_forget", {"watch_id": watch["watch_id"]})
            summary["receipt_count"] = sequence
            summary["connection_closed"] = False
            (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        require(summary["restoration_confirmed"], "The original widget opacity was not restored")
        require(summary["revisions"][0]["source_sha256"] != summary["revisions"][1]["source_sha256"], "The revisions are identical")
    summary["connection_closed"] = True
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"state": "passed", "receipts": sequence, "restored": True}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--game-root", type=Path, required=True)
    parser.add_argument("--expected-pid", type=int, required=True)
    parser.add_argument("--expected-creation-filetime", required=True)
    parser.add_argument("--source-directory", required=True, help="New source directory within the workspace")
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
