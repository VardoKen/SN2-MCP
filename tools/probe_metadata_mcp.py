"""Read confirmed FName/enum through MCP without starting the game or DLL."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROBE_OBJECTS = {
    "light": {"name": "Default__GA_Spell_Light", "class": "GA_Spell_Light",
              "path": "/Script/Angelscript.Default__GA_Spell_Light"},
    "attack": {"name": "Default__CM_Attack", "class": "CM_Attack",
               "path": "/Script/Angelscript.Default__CM_Attack"},
}
PROPERTY_NAMES = {"light": ["m_SocketName"], "attack": ["AttackDirectionRequirement", "UsageType"]}
ENUM_EXPECTATIONS = {
    "AttackDirectionRequirement": {"value": 0, "value_name": "EAttackDirectionRequirement::None",
                                   "path": "/Script/Angelscript.EAttackDirectionRequirement"},
    "UsageType": {"value": 1, "value_name": "EAttackUsageType::Regular",
                  "path": "/Script/Angelscript.EAttackUsageType"},
}
INTEGER_RANGES = {
    "ByteProperty": (0, 255), "IntProperty": (-(2**31), 2**31 - 1),
    "UInt32Property": (0, 2**32 - 1), "Int64Property": (-(2**63), 2**63 - 1),
    "UInt64Property": (0, 2**64 - 1),
}


class ProbeError(Exception):
    def __init__(self, code: str, message: str, context: dict | None = None):
        super().__init__(message)
        self.code = code
        self.context = context


def _require(condition: bool, code: str, message: str) -> None:
    if not condition:
        raise ProbeError(code, message)


def check_target(result: dict, expected: dict) -> dict:
    diagnostics = ({key: result.get(key) for key in ("scanned_slots", "total_slots", "skipped", "truncated")}
                   if isinstance(result, dict) else {"response_type": type(result).__name__})
    if isinstance(result, dict) and isinstance(result.get("items"), list):
        diagnostics["item_count"] = len(result["items"])
    if not (isinstance(result, dict) and result.get("truncated") is False and
            isinstance(result.get("items"), list) and
            all(type(result.get(key)) is int and result[key] >= 0
                for key in ("scanned_slots", "total_slots", "skipped")) and
            result["scanned_slots"] == result["total_slots"] and
            result["total_slots"] >= len(result["items"]) and
            result["skipped"] <= result["scanned_slots"]):
        raise ProbeError("probe_discovery_incomplete", "Object search did not confirm a complete registry scan", diagnostics)
    items = result["items"]
    _require(len(items) == 1 and isinstance(items[0], dict), "probe_object_ambiguous",
             "The exact probe CDO must be the only result")
    target = items[0]
    _require(all(target.get(key) == value for key, value in expected.items()) and
             isinstance(target.get("handle"), str) and bool(target["handle"]),
             "probe_object_mismatch", "The probe CDO name, class, or path did not match")
    return target


def check_enum(prop: dict, expected: dict) -> dict:
    _require(prop.get("type") in {"EnumProperty", "ByteProperty"} and prop.get("supported") is True and
             type(prop.get("value")) is int and prop["value"] == expected["value"] and "error" not in prop,
             "probe_enum_value_mismatch", "The reflected enum type or numeric value did not match")
    metadata = prop.get("enum")
    keys = {"path", "underlying_type", "name_status", "value_name", "matched_count", "ambiguous", "reason"}
    _require(isinstance(metadata, dict) and set(metadata) == keys,
             "probe_enum_metadata_invalid", "The enum must contain a complete confirmed metadata set")
    underlying = metadata["underlying_type"]
    _require(isinstance(underlying, str) and underlying in INTEGER_RANGES,
             "probe_enum_underlying_invalid", "The enum numeric representation type is not supported")
    low, high = INTEGER_RANGES[underlying]
    _require(low <= prop["value"] <= high and
             (prop["type"] != "ByteProperty" or underlying == "ByteProperty"),
             "probe_enum_underlying_invalid", "The number does not match the reflected underlying type")
    _require(metadata["path"] == expected["path"] and metadata["name_status"] == "matched" and
             metadata["value_name"] == expected["value_name"] and type(metadata["matched_count"]) is int and
             metadata["matched_count"] == 1 and metadata["ambiguous"] is False and metadata["reason"] is None,
             "probe_enum_label_mismatch", "The enum value name was not uniquely found by its numeric value")
    return {"type": prop["type"], "value": prop["value"], "underlying_type": underlying,
            "enum_path": metadata["path"], "value_name": metadata["value_name"]}


def check_snapshot(snapshot: dict, targets: dict[str, dict]) -> dict:
    _require(isinstance(snapshot, dict) and snapshot.get("source") == "live_native" and
             isinstance(snapshot.get("snapshot_id"), str) and bool(snapshot["snapshot_id"]) and
             isinstance(snapshot.get("session_id"), str) and bool(snapshot["session_id"]) and
             type(snapshot.get("process_id")) is int and snapshot["process_id"] > 0 and
             type(snapshot.get("thread_id")) is int and snapshot["thread_id"] > 0 and
             type(snapshot.get("tick_count")) is int and snapshot["tick_count"] > 0 and
             snapshot.get("consistency") == "single_tick_sequential" and snapshot.get("partial") is False and
             snapshot.get("truncated") is False and isinstance(snapshot.get("objects"), list),
             "probe_snapshot_incomplete", "The snapshot must be a complete confirmed read on game Tick")
    objects = snapshot["objects"]
    _require(len(objects) == len(targets) and all(isinstance(item, dict) and
                                               isinstance(item.get("handle"), str) for item in objects),
             "probe_snapshot_object_mismatch", "The snapshot does not contain the complete probe object set")
    by_handle = {item.get("handle"): item for item in objects}
    _require(len(by_handle) == len(objects) and set(by_handle) == {item["handle"] for item in targets.values()},
             "probe_snapshot_object_mismatch", "Snapshot object references are ambiguous or incomplete")
    values = {}
    for label, target in targets.items():
        obj = by_handle[target["handle"]]
        _require(obj.get("path") == target["path"] and obj.get("class") == target["class"] and
                 obj.get("partial") is False and obj.get("truncated") is False and "error" not in obj and
                 isinstance(obj.get("properties"), list), "probe_snapshot_object_mismatch",
                 "Object identity or property completeness was not confirmed")
        properties = obj["properties"]
        _require(all(isinstance(item, dict) and isinstance(item.get("name"), str) for item in properties),
                 "probe_property_invalid", "A snapshot property must contain a name")
        by_name = {item["name"]: item for item in properties}
        _require(len(by_name) == len(properties) and set(by_name) == set(PROPERTY_NAMES[label]),
                 "probe_property_absent", "The probe property set is incomplete or contains duplicates")
        for name, prop in by_name.items():
            if label == "light":
                _require(prop.get("type") == "NameProperty" and prop.get("supported") is True and
                         prop.get("value") == "spine_03" and "error" not in prop and "enum" not in prop,
                         "probe_name_mismatch", "FName m_SocketName must be the reflected spine_03 value")
                values[name] = {"type": "NameProperty", "value": prop["value"]}
            else:
                values[name] = check_enum(prop, ENUM_EXPECTATIONS[name])
    return values


def check_comparison(result: dict, before: dict, after: dict) -> None:
    _require(isinstance(result, dict) and result.get("source") == "copied_snapshots" and
             result.get("validation") == "client_supplied_copies" and result.get("provenance") == "unverified" and
             result.get("before_snapshot_id") == before.get("snapshot_id") and
             result.get("after_snapshot_id") == after.get("snapshot_id") and result.get("comparable") is True and
             result.get("reason") == "same_session_process" and result.get("equal_selected_values") is True and
             result.get("items") == [] and all(type(result.get(key)) is int and result[key] == 0
                                               for key in ("unknown_count", "value_changes", "type_changes",
                                                           "coverage_changes", "metadata_annotation_count")) and
             result.get("truncated") is False and isinstance(result.get("coverage"), dict) and
             result["coverage"].get("complete") is True and result.get("metadata_annotations") == [],
             "probe_comparison_mismatch", "Comparison of full snapshots did not confirm unchanged values and metadata")


def _single_error(error: BaseException) -> BaseException:
    while isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        error = error.exceptions[0]
    return error


async def run_probe(args: argparse.Namespace) -> dict:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT), env={"PYTHONUTF8": "1"},
    )
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as client:
            initialized = await client.initialize()
            tools = (await client.list_tools()).tools
            names = {tool.name for tool in tools}
            required = {"sn2_native_status", "sn2_find_objects", "sn2_snapshot", "sn2_compare_snapshots"}
            _require(required <= names and all(tool.annotations and tool.annotations.read_only_hint is True
                                               for tool in tools if tool.name in required),
                     "probe_tools_unavailable", "The bridge does not provide the required read-only tools")

            async def call(name: str, arguments: dict) -> dict:
                response = await client.call_tool(name, arguments)
                if response.is_error:
                    message = next((item.text for item in response.content if item.type == "text"), "MCP error")
                    raise ProbeError("mcp_call_failed", message)
                _require(isinstance(response.structured_content, dict), "probe_response_invalid",
                         "The MCP response does not contain structured JSON")
                return response.structured_content

            status = await call("sn2_native_status", {})
            _require(status.get("ready") is True, "game_not_ready", "The game and Tick must be ready before this read-only check")
            targets = {}
            discoveries = {}
            for label, expected in PROBE_OBJECTS.items():
                found = await call("sn2_find_objects", {"query": "", "class_query": expected["class"],
                                                       "exact_path": expected["path"], "limit": 2})
                targets[label] = check_target(found, expected)
                discoveries[label] = {key: found[key] for key in ("total_slots", "scanned_slots", "skipped", "truncated")}
            request = {"targets": [{"handle": target["handle"], "properties": PROPERTY_NAMES[label]}
                                   for label, target in targets.items()]}
            before = await call("sn2_snapshot", request)
            before_values = check_snapshot(before, targets)
            after = await call("sn2_snapshot", request)
            after_values = check_snapshot(after, targets)
            _require(before_values == after_values, "probe_values_changed", "Probe values changed between reads")
            comparison = await call("sn2_compare_snapshots", {"before": before, "after": after})
            check_comparison(comparison, before, after)
            _require(before["session_id"] == after["session_id"] == status.get("session_id") and
                     before["process_id"] == after["process_id"] == status.get("process_id") and
                     before["thread_id"] == after["thread_id"] == status.get("game_thread_id"),
                     "probe_runtime_changed", "The thread, process, or session changed during the check")
            return {"server_version": initialized.server_info.version, "tool_count": len(tools),
                    "properties": after_values, "equal_selected_values": True, "unknown_count": 0,
                    "discovery": discoveries,
                    "game_launched": False, "dll_loaded": False, "game_calls_invoked": False,
                    "checks": ["exact_cdos", "reflected_name_enum_values", "enum_labels_by_value",
                               "complete_snapshots_on_tick", "copied_comparison_equal"]}


def main(argv: list[str] | None = None) -> int:
    class RussianArgumentParser(argparse.ArgumentParser):
        def format_help(self) -> str:
            return super().format_help().replace("usage:", "usage:", 1).replace("options:", "options:", 1)
    parser = RussianArgumentParser(description="Read FName and enum in an already ready game through a real MCP", add_help=False)
    parser.add_argument("-h", "--help", action="help", help="Show help")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_probe(args))
    except BaseException as error:
        error = _single_error(error)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise error
        report = {"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                  "error_type": type(error).__name__}
        if getattr(error, "context", None) is not None:
            report["context"] = error.context
        print(json.dumps(report, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
