"""Check main package and production helper in one development session without raw FHitResult."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint
from probe_function_contract_mcp import TOKEN, make_caller
from probe_hit_result_wrapper_contract import exercise as main_exercise, finite_vector, check_signature as wrapper_signature, ZERO
from probe_object_mcp import ProbeError, _single_error
from probe_scalar_out_contract import check_scalar
from probe_vector_contract import check_parameter_vector_metadata

HOST = "G1RMCPDevelopmentHelpers"
VERSION = "1.0.0-dev.49"
HELPER_VERSION = "1.0.0-dev.49"
METHODS = ("GetHelperVersion", "HasWorld", "HasGameplayPlayer", "GetVisibilityTraceChannel", "TraceImpactPoint")
ENUM_PATH = "/Script/Engine.ETraceTypeQuery"


def check_signature(listing, name):
    if name == "TraceImpactPoint":
        if not isinstance(listing, dict): raise ProbeError("probe_signature_invalid", "Helper listing is not an object")
        matches = [item for item in listing.get("items", []) if isinstance(item, dict) and item.get("name") == name]
        if len(matches) != 1: raise ProbeError("probe_signature_absent", "Helper TraceImpactPoint was not found uniquely")
        original = matches[0]
        fields = original.get("parameters")
        size = original.get("parameter_size")
        if (not isinstance(fields, list) or len(fields) != 8 or type(original.get("parameter_count")) is not int
                or original["parameter_count"] != 8 or type(size) is not int or not 0 <= size <= 128):
            raise ProbeError("probe_signature_mismatch", "Helper trace requires seven arguments and a Bool return")
        extras = [field for field in fields if isinstance(field, dict) and field.get("name") in {"Channel", "TraceComplex", "IgnoreSelf"}]
        if len(extras) != 3 or {field["name"] for field in extras} != {"Channel", "TraceComplex", "IgnoreSelf"}:
            raise ProbeError("probe_signature_mismatch", "Helper configurable trace inputs do not match")
        spans = []
        for field in fields:
            if not isinstance(field, dict): raise ProbeError("probe_signature_mismatch", "Helper parameter is not an object")
            offset, width = field.get("offset"), field.get("element_size")
            if (type(offset) is not int or type(width) is not int or offset < 0 or width < 1 or offset + width > 128
                    or field.get("return") is False and offset + width > size):
                raise ProbeError("probe_signature_mismatch", "Helper trace exceeds the bounded buffer")
            if any(offset < end and start < offset + width for start, end in spans): raise ProbeError("probe_signature_mismatch", "Helper trace spans overlap")
            spans.append((offset, offset + width))
        for field in extras:
            flags = field.get("flags")
            if (type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80 or flags & (0x100 | 0x400 | 0x2 | 0x08000000)
                    or field.get("return") is not False or field.get("out") is not False or field.get("const") is not False
                    or field.get("reference") is not False or type(field.get("array_dim")) is not int or field["array_dim"] != 1):
                raise ProbeError("probe_signature_flags", "Helper enum and bool inputs must be by value")
            if field["name"] == "Channel":
                if (field.get("type") not in {"EnumProperty", "ByteProperty"} or field["element_size"] not in {1, 4, 8}
                        or field["type"] == "ByteProperty" and field["element_size"] != 1):
                    raise ProbeError("probe_signature_mismatch", "Channel did not confirm a numeric enum layout")
            elif (field.get("type"), field["element_size"]) != ("BoolProperty", 1):
                raise ProbeError("probe_signature_mismatch", "TraceComplex/IgnoreSelf is not Bool1")
        reduced = dict(original)
        reduced["parameters"] = [field for field in fields if field not in extras]
        reduced["parameter_count"] = len(reduced["parameters"])
        compact = {**listing, "items": [reduced]}
        _, token, vector_size = wrapper_signature(compact, "TraceImpactPoint")
        return original, token, vector_size
    if (name not in METHODS or not isinstance(listing, dict) or listing.get("truncated") is not False
            or listing.get("scan_complete") is not True or listing.get("scan_truncated") is not False):
        raise ProbeError("probe_signature_invalid", "Helper catalog is incomplete")
    matches = [item for item in listing.get("items", []) if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1: raise ProbeError("probe_signature_absent", "Helper getter was not found uniquely")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or len(fields) != 1 or not isinstance(fields[0], dict)
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != 1 or method.get("parameters_truncated") is not False
            or type(size) is not int or not 0 <= size <= 32):
        raise ProbeError("probe_signature_mismatch", "Helper getter must have one return and no inputs")
    field = fields[0]
    flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
    if (type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80 or not flags & 0x400 or flags & (0x2 | 0x08000000)
            or field.get("return") is not True or field.get("out") is not bool(flags & 0x100)
            or field.get("reference") is not False or field.get("const") is not False
            or type(offset) is not int or offset < 0 or type(width) is not int or width < 1 or offset + width > 32
            or type(field.get("array_dim")) is not int or field["array_dim"] != 1):
        raise ProbeError("probe_signature_flags", "Helper getter return CPF flags and bounds are not confirmed")
    if name == "GetHelperVersion" and (field.get("type"), width) != ("StrProperty", 16):
        raise ProbeError("probe_signature_mismatch", "Helper version must return FString16")
    if name in {"HasWorld", "HasGameplayPlayer"} and (field.get("type"), width) != ("BoolProperty", 1):
        raise ProbeError("probe_signature_mismatch", "Helper world/player getter must return Bool1")
    if name == "GetVisibilityTraceChannel" and (field.get("type") not in {"EnumProperty", "ByteProperty"} or width not in {1, 4, 8}
                                                or field["type"] == "ByteProperty" and width != 1):
        raise ProbeError("probe_signature_mismatch", "Helper visibility channel must return a reflected enum")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token): raise ProbeError("probe_signature_token_absent", "Helper getter token is missing")
    return method, token, None


def check_channel(result):
    value, metadata = result.get("result"), result.get("return_enum")
    if (result.get("executed") is not True or result.get("return_type") not in {"EnumProperty", "ByteProperty"}
            or type(value) is not int or result.get("return_type") == "ByteProperty" and not 0 <= value <= 255
            or not isinstance(metadata, dict) or metadata.get("path") != ENUM_PATH
            or metadata.get("name_status") != "matched" or type(metadata.get("matched_count")) is not int
            or metadata["matched_count"] != 1 or metadata.get("ambiguous") is not False or metadata.get("reason") is not None
            or not isinstance(metadata.get("value_name"), str) or not metadata["value_name"]
            or metadata.get("underlying_type") not in {"ByteProperty", "IntProperty", "Int64Property", "UInt32Property", "UInt64Property"}):
        raise ProbeError("probe_helper_channel", "Visibility channel is not confirmed by its current number and declared UEnum; do not guess")
    return value


async def helper_exercise(call):
    report = {"reflected_functions": {}, "trace": {"tested": False}, "raw_hit_result": "not_marshaled"}
    try:
        found = await call("sn2_find_objects", {"query": HOST, "class_query": HOST, "limit": 100})
        report["find_result"] = found
        if found.get("truncated") is not False or not isinstance(found.get("items"), list): raise ProbeError("probe_object_absent", "Helper object listing is incomplete")
        live, cdos = [], []
        for item in found["items"]:
            if (not isinstance(item, dict) or item.get("class") != HOST or not isinstance(item.get("name"), str)
                    or not isinstance(item.get("path"), str) or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
                raise ProbeError("unsafe_probe_target", "Helper identity is not confirmed")
            if item["name"] == "Default__" + HOST and item["path"] == "/Script/Angelscript.Default__" + HOST: cdos.append(item)
            elif (item["name"].startswith("Default__") or "Default__" in item["path"] or item["path"].startswith("/Script/")
                    or not item["name"].startswith(HOST) or not item["path"].endswith(item["name"])):
                raise ProbeError("unsafe_probe_target", "Helper template cannot be treated as a live instance")
            else: live.append(item)
        report["found_cdo_count"], report["found_live_count"] = len(cdos), len(live)
        if len(live) != 1 or len(cdos) != 1: raise ProbeError("probe_object_absent", "One CDO and one live helper subsystem are required")
        target = live[0]
        report.update(target=target, cdo=cdos[0])
        tokens, sizes = {}, {}
        for name in METHODS:
            listing = await call("sn2_object_functions", {"handle": target["handle"], "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            _, tokens[name], sizes[name] = check_signature(listing, name)
        if len(set(tokens.values())) != len(tokens): raise ProbeError("probe_signature_token_alias", "Helper tokens match")
        async def invoke(name, arguments=None):
            return await call("sn2_invoke", {"handle": target["handle"], "function": name,
                "arguments": {} if arguments is None else arguments, "expected_signature_token": tokens[name]})
        version = await invoke("GetHelperVersion")
        report["version_result"] = version
        check_scalar(version, "StrProperty", HELPER_VERSION)
        if version.get("cleanup_confirmed") is not True: raise ProbeError("probe_helper_cleanup", "Helper version FString return cleanup is not confirmed")
        world = await invoke("HasWorld")
        report["world_result"] = world
        check_scalar(world, "BoolProperty", True)
        gameplay_player = await invoke("HasGameplayPlayer")
        report["gameplay_player_result"] = gameplay_player
        gameplay_player_ready = gameplay_player.get("result") is True
        check_scalar(gameplay_player, "BoolProperty", gameplay_player_ready)
        report["gameplay_player_ready"] = gameplay_player_ready
        channel_result = await invoke("GetVisibilityTraceChannel")
        report["channel_result"] = channel_result
        channel = check_channel(channel_result)
        report.update(version=version["result"], version_cleanup_confirmed=True, has_world=True, channel_result=channel_result, signature_tokens=tokens)
        report["channel_validation"] = "current_UEnum_verified"
        arguments = {"Start": ZERO, "End": ZERO, "Channel": channel, "TraceComplex": False,
                     "ActorsToIgnore": [], "IgnoreSelf": True, "ImpactPoint": {"out": True}}
        trace = await invoke("TraceImpactPoint", arguments)
        if trace.get("cleanup_confirmed") is not True or trace.get("array_input_cleanup_confirmed") is not True:
            raise ProbeError("probe_helper_cleanup", "Helper actor-array cleanup is not confirmed")
        if trace.get("executed") is not True or trace.get("return_type") != "BoolProperty" or type(trace.get("result")) is not bool:
            raise ProbeError("probe_helper_trace", "Helper trace did not confirm Bool")
        out, metadata = trace.get("out_parameters"), trace.get("out_parameter_metadata")
        if not isinstance(out, dict) or set(out) != {"ImpactPoint"} or not isinstance(metadata, dict) or set(metadata) != {"ImpactPoint"}:
            raise ProbeError("probe_helper_trace", "Helper trace must return exactly one FVector out")
        finite_vector(out["ImpactPoint"])
        check_parameter_vector_metadata({"struct": metadata["ImpactPoint"], "direction": "output", "argument_marker": {"out": True}}, sizes["TraceImpactPoint"], "output")
        if trace["result"] is False and any(value != 0 for value in out["ImpactPoint"].values()):
            raise ProbeError("probe_helper_trace", "False helper trace did not preserve the explicit ZeroVector branch")
        report["trace"] = {"tested": True, "arguments": arguments, "hit": trace["result"], "impact_point": out["ImpactPoint"],
                           "array_input_cleanup_confirmed": True, "collision_semantics": "degenerate probe; world nonnull only"}
        return report
    except BaseException as error:
        error.add_note("Development helpers report: " + json.dumps(report, ensure_ascii=False))
        raise


async def exercise(call):
    # Main fixture cleanups finish before the production helper's single trace.
    main = await main_exercise(call)
    if main.get("has_world") is not True: raise ProbeError("main_world_unavailable", "Main subsystem did not confirm a live world")
    try:
        return {"main": main, "helper": await helper_exercise(call)}
    except BaseException as error:
        error.add_note("Development main package report: " + json.dumps(main, ensure_ascii=False))
        raise


async def run(args):
    if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}: raise ProbeError("invalid_mcp_url", "Helpers probe uses shared localhost HTTP")
    async with streamable_http_client(args.mcp_url) as connection:
        async with ClientSession(connection[0], connection[1]) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main():
    parser = argparse.ArgumentParser(description="Check the development helper companion in a shared HTTP session")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:27124/mcp"); args = parser.parse_args()
    async def protected():
        with deferred_sigint(): return await run(args)
    try: print(json.dumps(asyncio.run(protected()), indent=2, ensure_ascii=False)); return 0
    except BaseException as error:
        error = _single_error(error); print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": str(error), "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr); return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8"); sys.exit(main())
