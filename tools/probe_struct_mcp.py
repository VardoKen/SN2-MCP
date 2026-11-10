"""Check copied reads of two struct properties on an owned CDO through MCP."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))

from probe_cleanup import deferred_sigint
from probe_object_mcp import ProbeError, _caller, _single_error

HOST = "G1RMCPPODReadProbe"
FIELDS = {
    "StoredVector": {"X": ("ReadVectorX", 1.25), "Y": ("ReadVectorY", -2.5), "Z": ("ReadVectorZ", 3.75)},
    "StoredRotation": {"Pitch": ("ReadPitch", 10.0), "Yaw": ("ReadYaw", -20.0), "Roll": ("ReadRoll", 30.0)},
}
METHODS = {"ReadMarker": ("IntProperty", 4)} | {
    method: ("FloatProperty", 4) for components in FIELDS.values() for method, _ in components.values()
}


def check_target(value: object) -> dict:
    if (not isinstance(value, dict) or value.get("name") != "Default__" + HOST
            or value.get("class") != HOST or value.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(value.get("handle"), str) or not value["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Owned struct probe CDO is not confirmed")
    return value


def check_signature(response: object, method_name: str) -> dict:
    if not isinstance(response, dict) or method_name not in METHODS:
        raise ProbeError("probe_signature_invalid", "Unknown struct fixture method")
    items = response.get("items")
    if (not isinstance(items, list) or response.get("truncated") is not False
            or response.get("scan_complete") is not True):
        raise ProbeError("probe_signature_incomplete", "Struct fixture signature search is incomplete")
    matches = [item for item in items if isinstance(item, dict) and item.get("name") == method_name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Owned method signature was not found uniquely")
    method = matches[0]
    parameters = method.get("parameters")
    allowed_layouts = {("IntProperty", 4)} if method_name == "ReadMarker" else {("FloatProperty", 4), ("DoubleProperty", 8)}
    if (method.get("parameters_truncated") is not False or not isinstance(parameters, list)
            or len(parameters) != 1 or not isinstance(parameters[0], dict)):
        raise ProbeError("probe_signature_mismatch", "Method must have one complete scalar return")
    parameter = parameters[0]
    actual_type, actual_size = parameter.get("type"), parameter.get("element_size")
    offset, size = parameter.get("offset"), method.get("parameter_size")
    if (not isinstance(parameter.get("name"), str) or not parameter["name"] or parameter.get("return") is not True
            or parameter.get("reference") is not False or parameter.get("array_dim") != 1
            or type(parameter.get("array_dim")) is not int or (actual_type, actual_size) not in allowed_layouts
            or type(actual_size) is not int
            or type(offset) is not int or not 0 <= offset <= 32 - actual_size
            or type(size) is not int or not 0 <= size <= 32):
        raise ProbeError("probe_signature_mismatch", "Owned method scalar signature is not confirmed: "
                         + json.dumps({"method": method_name, "allowed_layouts": sorted(allowed_layouts),
                                       "parameter_size": size, "parameter": parameter}, ensure_ascii=False))
    # AngelScript return may lie outside ParmsSize; offset+size itself is bounded.
    return method


def _finite(value: object) -> bool:
    try:
        return type(value) in (int, float) and math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def check_struct(response: object, target: dict, session_id: str, property_name: str) -> dict:
    if property_name not in FIELDS or not isinstance(response, dict):
        raise ProbeError("probe_struct_invalid", "Unknown probe struct property")
    if (response.get("source") != "live_native" or response.get("session_id") != session_id
            or response.get("handle") != target["handle"] or response.get("property_name") != property_name):
        raise ProbeError("probe_struct_owner", "Struct copy source or owner is not confirmed")
    if ("error" in response or response.get("partial", False) is not False
            or response.get("truncated", False) is not False
            or response.get("consistency") != "copied_bytes_then_metadata_revalidated"):
        raise ProbeError("probe_struct_incomplete", "Struct copy completeness and revalidation are not confirmed")
    path, size, alignment = response.get("struct_path"), response.get("properties_size"), response.get("min_alignment")
    if (not isinstance(path, str) or not path or "\0" in path or len(path.encode("utf-8")) > 8192
            or type(size) is not int or size not in (12, 24)
            or type(alignment) is not int or alignment not in (1, 2, 4, 8)):
        raise ProbeError("probe_struct_layout", "Actual struct path, size, and alignment are not confirmed")
    components = response.get("components")
    if not isinstance(components, list) or len(components) != 3 or not all(isinstance(item, dict) for item in components):
        raise ProbeError("probe_struct_layout", "Struct copy must contain three numeric components")
    expected = FIELDS[property_name]
    by_name = {item.get("name"): item for item in components if isinstance(item.get("name"), str)}
    if len(by_name) != 3 or set(by_name) != set(expected):
        raise ProbeError("probe_struct_layout", "Struct components are duplicated, missing, or unexpected")
    width = size // 3
    numeric_type = "FloatProperty" if width == 4 else "DoubleProperty"
    offsets = []
    for name, (_, expected_value) in expected.items():
        item = by_name[name]
        offset = item.get("offset")
        if (set(item) != {"name", "type", "element_size", "offset", "value"} or item.get("type") != numeric_type
                or type(item.get("element_size")) is not int or item["element_size"] != width
                or type(offset) is not int or offset < 0 or offset > size - width
                or not _finite(item.get("value")) or item["value"] != expected_value):
            raise ProbeError("probe_struct_component", "Struct component layout or value is not confirmed")
        offsets.append(offset)
    if alignment > width or sorted(offsets) != [0, width, 2 * width]:
        raise ProbeError("probe_struct_layout", "Components overlap or do not cover the struct")
    return {"struct_path": path, "properties_size": size, "min_alignment": alignment,
            "components": {name: by_name[name]["value"] for name in expected}, "numeric_type": numeric_type}


async def exercise(call) -> dict:
    status = await call("sn2_native_status", {})
    owner = status.get("session_id")
    if status.get("ready") is not True or not isinstance(owner, str) or not owner:
        raise ProbeError("game_not_ready", "Tick and current session are not confirmed")
    found = await call("sn2_find_objects", {"query": "Default__" + HOST, "class_query": HOST,
                                           "exact_path": "/Script/Angelscript.Default__" + HOST, "limit": 2})
    if (found.get("truncated") is not False or not isinstance(found.get("items"), list)
            or len(found["items"]) != 1):
        raise ProbeError("probe_object_absent", "Owned struct CDO was not found uniquely")
    target = check_target(found["items"][0])
    signatures = {}
    for method in METHODS:
        signature = await call("sn2_object_functions", {"handle": target["handle"], "limit": 20,
                                                        "function_query": method})
        signatures[method] = check_signature(signature, method)

    async def read_scalar(method):
        result = await call("sn2_invoke", {"handle": target["handle"], "function": method, "arguments": {}})
        expected_type = signatures[method]["parameters"][0]["type"]
        if result.get("executed") is not True or result.get("return_type") != expected_type:
            raise ProbeError("probe_not_executed", "Owned component read did not confirm execution")
        return result.get("result")

    marker = await read_scalar("ReadMarker")
    if type(marker) is not int or marker != 101:
        raise ProbeError("probe_marker_mismatch", "Owned struct CDO marker is not confirmed")
    report = {"checks": [], "structs": {}, "game_launched": False, "dll_loaded": False,
              "mutations_performed": False, "proof": "own_fixture_reflection_and_scalar_components"}
    for property_name, expected in FIELDS.items():
        before = {name: await read_scalar(method) for name, (method, _) in expected.items()}
        if any(not _finite(before[name]) or before[name] != number for name, (_, number) in expected.items()):
            raise ProbeError("probe_initial_values", "Initial struct constants are not confirmed by scalar methods")
        result = await call("sn2_read_struct", {"handle": target["handle"], "property_name": property_name})
        checked = check_struct(result, target, owner, property_name)
        after = {name: await read_scalar(method) for name, (method, _) in expected.items()}
        if before != checked["components"] or after != before:
            raise ProbeError("probe_struct_mismatch", "Copied components do not match stable AS methods")
        repeated = check_struct(await call("sn2_read_struct", {"handle": target["handle"], "property_name": property_name}),
                                target, owner, property_name)
        if repeated != checked:
            raise ProbeError("probe_struct_unstable", "Repeated struct constant reads changed")
        report["structs"][property_name] = checked
        report["checks"].append(property_name + "_copied_layout_matches_scalar_methods")
    return report


async def run_probe(args) -> dict:
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT), env={"PYTHONUTF8": "1"})
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            report = await exercise(_caller(session))
            report["server_version"] = initialized.server_info.version
            return report


async def _protected(args):
    with deferred_sigint():
        return await run_probe(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="Check read-only StructProperty on an owned fixture through MCP")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    try:
        report = asyncio.run(_protected(args))
    except BaseException as error:
        cause = _single_error(error)
        if isinstance(cause, (KeyboardInterrupt, SystemExit)):
            raise cause
        print(json.dumps({"code": getattr(cause, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={cause.errno}, winerror={getattr(cause, 'winerror', None)})" if isinstance(cause, OSError) else str(cause))}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
