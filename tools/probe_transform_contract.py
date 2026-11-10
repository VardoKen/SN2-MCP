"""One canonical FTransform input series on an owned AS CDO."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_function_contract_mcp import TOKEN, make_caller
from probe_object_mcp import ProbeError, _single_error
from probe_scalar_out_contract import UNKNOWN_GAME, check_rejection, check_scalar
from probe_vector_contract import check_parameter_vector_metadata, check_vector

HOST = "G1RMCPTransformProbe"
MARKER = 191
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "AcceptTransform", "GetTranslation", "GetScale3D", "GetRotationEuler")
GETTERS = ("GetTranslation", "GetScale3D", "GetRotationEuler")
ZERO = {"X": 0, "Y": 0, "Z": 0}
ONE = {"X": 1, "Y": 1, "Z": 1}
IDENTITY = {"Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1}, "Translation": ZERO, "Scale3D": ONE}


def check_target(item) -> dict:
    if (not isinstance(item, dict) or item.get("name") != "Default__" + HOST or item.get("class") != HOST
            or item.get("path") != "/Script/Angelscript.Default__" + HOST
            or not isinstance(item.get("handle"), str) or not item["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Exact owned Transform CDO is not confirmed")
    return item


def _alignment(value, maximum) -> bool:
    return type(value) is int and 1 <= value <= maximum and not value & (value - 1)


def check_transform_metadata(metadata) -> dict:
    if (not isinstance(metadata, dict) or metadata.get("struct_path") != "/Script/CoreUObject.Transform"
            or metadata.get("struct_name") != "Transform" or type(metadata.get("properties_size")) is not int
            or metadata["properties_size"] != 96 or not _alignment(metadata.get("min_alignment"), 16)
            or not isinstance(metadata.get("members"), list) or len(metadata["members"]) != 3
            or any(not isinstance(member, dict) or not isinstance(member.get("name"), str) for member in metadata["members"])):
        raise ProbeError("probe_transform_layout", "CoreUObject.Transform96 with three members is not confirmed")
    members = {member["name"]: member for member in metadata["members"]}
    if set(members) != {"Rotation", "Translation", "Scale3D"}:
        raise ProbeError("probe_transform_layout", "Transform members do not match Rotation/Translation/Scale3D")
    for name, (offset, width, struct_name) in {"Rotation": (0, 32, "Quat"), "Translation": (32, 24, "Vector"),
                                             "Scale3D": (64, 24, "Vector")}.items():
        member, nested = members[name], members[name].get("struct")
        if (member.get("type") != "StructProperty" or type(member.get("element_size")) is not int or member["element_size"] != width
                or type(member.get("offset")) is not int or member["offset"] != offset
                or type(member.get("array_dim")) is not int or member["array_dim"] != 1
                or not isinstance(nested, dict) or nested.get("struct_path") != "/Script/CoreUObject." + struct_name
                or nested.get("struct_name") != struct_name or type(nested.get("properties_size")) is not int
                or nested["properties_size"] != width or not _alignment(nested.get("min_alignment"), 16 if name == "Rotation" else 8)
                or metadata["min_alignment"] < nested["min_alignment"] or offset % nested["min_alignment"]):
            raise ProbeError("probe_transform_layout", "Exact nested type, size, offset, or alignment does not match: " + name)
        if name != "Rotation":
            check_parameter_vector_metadata({"struct": nested, "direction": "input"}, 24, "input")
        else:
            components = nested.get("components")
            if (not isinstance(components, list) or len(components) != 4
                    or any(not isinstance(component, dict) or not isinstance(component.get("name"), str) for component in components)):
                raise ProbeError("probe_transform_layout", "Rotation did not confirm four Double Quat components")
            by_name = {component["name"]: component for component in components}
            if set(by_name) != {"X", "Y", "Z", "W"}:
                raise ProbeError("probe_transform_layout", "Quat components do not match XYZW")
            for axis, expected_offset in zip(("X", "Y", "Z", "W"), (0, 8, 16, 24)):
                component = by_name[axis]
                if (component.get("type") != "DoubleProperty" or type(component.get("element_size")) is not int
                        or component["element_size"] != 8 or type(component.get("offset")) is not int
                        or component["offset"] != expected_offset or type(component.get("array_dim")) is not int or component["array_dim"] != 1):
                    raise ProbeError("probe_transform_layout", "Quat did not confirm exact Double8 XYZW offsets 0/8/16/24")
    return metadata


def check_signature(listing: dict, name: str, *, const_reference=False) -> tuple[dict, str]:
    accept_name = "AcceptConstTransform" if const_reference else "AcceptTransform"
    methods = tuple(accept_name if method == "AcceptTransform" else method for method in METHODS)
    if (name not in methods or not isinstance(listing, dict) or listing.get("truncated") is not False
            or listing.get("scan_complete") is not True or listing.get("scan_truncated") is not False
            or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete Transform method catalog is not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1:
        raise ProbeError("probe_signature_absent", "Transform method was not found uniquely")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 128
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != len(fields)):
        raise ProbeError("probe_signature_mismatch", "NumParms and bounded Transform buffer are not confirmed")
    returned = [field for field in fields if field.get("return") is True]
    inputs = [field for field in fields if field.get("return") is False]
    expected = {"ReadMarker": ("IntProperty", 4), "ReadCallsCount": ("IntProperty", 4),
                accept_name: ("BoolProperty", 1), **{getter: ("StructProperty", 24) for getter in GETTERS}}.get(name)
    if (len(returned) != (expected is not None) or len(inputs) != (name == accept_name)
            or len(fields) != len(returned) + len(inputs)
            or returned and (returned[0].get("type"), returned[0].get("element_size")) != expected):
        raise ProbeError("probe_signature_mismatch", "Transform input and exact safe return do not match")
    spans = []
    for field in fields:
        flags, offset, width = field.get("flags"), field.get("offset"), field.get("element_size")
        if (not isinstance(field.get("name"), str) or type(offset) is not int or type(width) is not int
                or offset < 0 or width < 1 or offset + width > 128 or type(field.get("array_dim")) is not int
                or field["array_dim"] != 1 or type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80
                or bool(flags & 0x400) is not field["return"] or bool(flags & 0x100) is not field.get("out")
                or bool(flags & 0x08000000) is not field.get("reference") or bool(flags & 0x2) is not field.get("const")
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_flags", "Transform input CPF flags or bounds are not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "Transform parameter ranges overlap")
        spans.append((offset, offset + width))
        if field["return"]:
            if flags & (0x2 | 0x08000000):
                raise ProbeError("probe_signature_flags", "Transform getter return must be by value without const/ref")
            if field["type"] == "StructProperty":
                check_parameter_vector_metadata(field, 24, "return")
        else:
            if (field["name"] != "Input" or field.get("type") != "StructProperty" or width != 96
                    or field.get("direction") != "input" or "argument_marker" in field
                    or const_reference and (not flags & 0x2 or not flags & (0x100 | 0x08000000)
                                            or field.get("passing") != "const_reference")
                    or not const_reference and (flags & (0x100 | 0x2 | 0x08000000) or field.get("passing") != "value")):
                raise ProbeError("probe_transform_policy", "Transform input did not confirm the selected value/const-reference contract")
            metadata = check_transform_metadata(field.get("struct"))
            if offset % metadata["min_alignment"]:
                raise ProbeError("probe_transform_layout", "Transform offset does not match public alignment")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "Transform method did not receive a valid token")
    return method, token


def check_euler(value, expected) -> None:
    if (not isinstance(value, dict) or set(value) != {"X", "Y", "Z"}
            or any(type(value[axis]) not in (int, float) or not math.isfinite(value[axis])
                   or abs(value[axis] - expected[axis]) > 1e-4 for axis in expected)):
        raise ProbeError("probe_transform_rotation", "Rotation did not preserve expected Euler semantics within 1e-4 degrees")


async def exercise(call, *, const_reference=False, native_probe=None) -> dict:
    host = "G1RMCPConstTransformProbe" if const_reference else HOST
    marker = 201 if const_reference else MARKER
    accept_name = "AcceptConstTransform" if const_reference else "AcceptTransform"
    method_names = tuple(accept_name if method == "AcceptTransform" else method for method in METHODS)
    cleanup_required, failure = False, None
    report = {"checks": [], "accepted": [], "rejections": [], "cleanup": [], "reflected_functions": {},
              "game_launched": False, "dll_loaded": False, "transform_return": "not_tested", "transform_out": "not_tested",
              "rotation_verification": "Euler Pitch/Yaw/Roll semantic equivalence; quaternion component bits are checked by native unit tests",
              "euler_tolerance_degrees": 1e-4}
    try:
        status = await call("sn2_native_status", {})
        session_id = status.get("session_id")
        if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
            raise ProbeError("game_not_ready", "Tick and native session are not confirmed")
        found = await call("sn2_find_objects", {"query": "Default__" + host, "class_query": host,
            "exact_path": "/Script/Angelscript.Default__" + host, "limit": 2})
        if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
            raise ProbeError("probe_object_absent", "Exact owned Transform CDO was not found uniquely")
        item = found["items"][0]
        if const_reference:
            if (not isinstance(item, dict) or item.get("name") != "Default__" + host or item.get("class") != host
                    or item.get("path") != "/Script/Angelscript.Default__" + host or not isinstance(item.get("handle"), str)
                    or not item["handle"].startswith("obj-")):
                raise ProbeError("unsafe_probe_target", "Exact owned const Transform CDO is not confirmed")
            target = item
        else:
            target = check_target(item)
        handle, methods, tokens = target["handle"], {}, {}
        for name in method_names:
            listing = await call("sn2_object_functions", {"handle": handle, "function_query": name, "limit": 10})
            report["reflected_functions"][name] = listing
            methods[name], tokens[name] = check_signature(listing, name, const_reference=const_reference)
        if len(set(tokens.values())) != len(tokens):
            raise ProbeError("probe_signature_token_alias", "Different Transform methods received the same token")

        async def invoke(name, arguments=None, *, expected_error=None):
            supplied = {} if arguments is None else arguments
            json.dumps(supplied, allow_nan=False)
            return await call("sn2_invoke", {"handle": handle, "function": name, "arguments": supplied,
                              "expected_signature_token": tokens[name]}, expected_error=expected_error)

        async def verify(translation, scale, euler):
            for getter, expected in (("GetTranslation", translation), ("GetScale3D", scale), ("GetRotationEuler", euler)):
                result = await invoke(getter)
                if result.get("executed") is not True or result.get("return_type") != "StructProperty":
                    raise ProbeError("probe_transform_getter", "Transform getter did not confirm a FVector return")
                check_parameter_vector_metadata({"struct": result.get("result_metadata"), "direction": "return"}, 24, "return")
                if getter == "GetRotationEuler": check_euler(result.get("result"), expected)
                else: check_vector(result.get("result"), expected)

        check_scalar(await invoke("ReadMarker"), "IntProperty", marker)
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
        await verify(ZERO, ONE, ZERO)
        report.update(session_id=session_id, handle=handle, signature_tokens=tokens)
        report["checks"].append(f"own_marker{marker}_counter0_identity_exact_transform96_quat32_vectors24_full_reflection")
        report["passing"] = "const_reference" if const_reference else "value"
        value = {"Rotation": {"X": 0, "Y": 0, "Z": math.sqrt(0.5), "W": math.sqrt(0.5)},
                 "Translation": {"X": 1.25, "Y": -2.5, "Z": 3.75}, "Scale3D": {"X": 2, "Y": 0.5, "Z": -1}}
        count = 0
        for accepted, euler in ((copy.deepcopy(IDENTITY), ZERO), (value, {"X": 0, "Y": 90, "Z": 0})):
            cleanup_required = True
            check_scalar(await invoke(accept_name, {"Input": accepted}), "BoolProperty", True)
            count += 1
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            await verify(accepted["Translation"], accepted["Scale3D"], euler)
            report["accepted"].append({"input": accepted, "euler_expected": euler})
        report["checks"].append("identity_and_yaw90_nonzero_translation_nonuniform_signed_scale_exact_getters")
        malformed = [None, True, [], "FTransform", {}, {**value, "Unexpected": 0}, {**value, "Rotation": {"X": 0, "Y": 0, "Z": 0}},
                     {**value, "Translation": {"X": 1, "Y": 2}}, {**value, "Scale3D": {"X": 1, "Y": 2, "Z": 3, "W": 4}},
                     {**value, "Rotation": {"X": True, "Y": 0, "Z": 0, "W": 1}},
                     {**value, "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 0}},
                     {**value, "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 2}},
                     {**value, "Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1.000002}},
                     {**value, "Translation": {"X": "1", "Y": 2, "Z": 3}},
                     {**value, "Scale3D": {"X": True, "Y": 2, "Z": 3}}]
        cases = [({"Input": item}, "invalid_argument") for item in malformed]
        cases += [({}, "missing_argument"), ({"Input": value, "UnexpectedForProbe": 1}, "unknown_argument")]
        for supplied, code in cases:
            refusal = await invoke(accept_name, supplied, expected_error=code)
            check_rejection(refusal, accept_name, supplied, code)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
            report["rejections"].append({"arguments": supplied, "code": code})
        local_nonfinite = 0
        for member, axis in (("Rotation", "W"), ("Translation", "X"), ("Scale3D", "Z")):
            for nonfinite in (float("nan"), float("inf"), -float("inf")):
                supplied = copy.deepcopy(value)
                supplied[member][axis] = nonfinite
                try: await invoke(accept_name, {"Input": supplied})
                except ValueError: local_nonfinite += 1
                else: raise ProbeError("probe_nonfinite_transport", "Nonfinite JSON was sent to MCP")
        report["local_nonfinite_json_rejections"] = local_nonfinite
        await verify(value["Translation"], value["Scale3D"], {"X": 0, "Y": 90, "Z": 0})
        check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["checks"].append("malformed_nested_transform_nonunit_quat_and_names_refused_without_effect")
        if native_probe is not None:
            await native_probe(call, report)
            check_scalar(await invoke("ReadCallsCount"), "IntProperty", count)
        report["completed_mutating_calls"] = count
        return report
    except BaseException as error:
        failure = error
        error.add_note("Transform report: " + json.dumps(report, ensure_ascii=False, allow_nan=False))
        raise
    finally:
        if cleanup_required:
            if getattr(failure, "code", None) in UNKNOWN_GAME:
                report["cleanup"].append("skipped_unusable_or_unknown_game_root_stops")
                failure.add_note("Transform cleanup skipped after an uncertain call or unavailable connection; the development session owner stops the game")
            else:
                async def finish_cleanup():
                    check_scalar(await invoke("ResetProbe"), "", None)
                    check_scalar(await invoke("ReadCallsCount"), "IntProperty", 0)
                    check_scalar(await invoke("ReadMarker"), "IntProperty", marker)
                    await verify(ZERO, ONE, ZERO)
                    report["cleanup"].append(f"ResetProbe_zero_counter_marker{marker}_identity_transform")
                try: await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="transform contract cleanup")
                except BaseException as error:
                    if failure is None: raise
                    failure.add_note("Owned Transform CDO cleanup is not confirmed: " + str(error))


async def run(args) -> dict:
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "Transform probe uses only local MCP")
        async with streamable_http_client(args.mcp_url) as connection:
            async with ClientSession(connection[0], connection[1]) as session:
                initialized = await session.initialize()
                return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dev.19 Transform input on an owned loaded fixture")
    parser.add_argument("--mcp-url")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    async def protected():
        with deferred_sigint(): return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2, allow_nan=False))
        return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error)),
                          "notes": getattr(error, "__notes__", [])}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
