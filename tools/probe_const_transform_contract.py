"""Const FTransform input on an owned AS CDO and native Kismet TransformLocation."""
from __future__ import annotations

import argparse
import asyncio
import copy
import json
from pathlib import Path
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint
from probe_function_contract_mcp import TOKEN, make_caller
from probe_object_mcp import ProbeError, _single_error
from probe_transform_contract import check_signature as check_transform_signature, check_transform_metadata, exercise as transform_exercise, IDENTITY
from probe_vector_contract import check_parameter_vector_metadata, check_vector

HOST = "G1RMCPConstTransformProbe"
MARKER = 201
METHODS = ("ReadMarker", "ReadCallsCount", "ResetProbe", "AcceptConstTransform", "GetTranslation", "GetScale3D", "GetRotationEuler")
NATIVE_CLASS = "KismetMathLibrary"
NATIVE_PATH = "/Script/Engine.Default__KismetMathLibrary"
NATIVE_METHOD = "TransformLocation"


def check_signature(listing: dict, name: str):
    return check_transform_signature(listing, name, const_reference=True)


def check_native_signature(listing: dict) -> tuple[dict, str, str, str]:
    if (not isinstance(listing, dict) or listing.get("truncated") is not False or listing.get("scan_complete") is not True
            or listing.get("scan_truncated") is not False or not isinstance(listing.get("items"), list)):
        raise ProbeError("probe_signature_invalid", "Complete native TransformLocation catalog is not confirmed")
    matches = [item for item in listing["items"] if isinstance(item, dict) and item.get("name") == NATIVE_METHOD]
    if len(matches) != 1: raise ProbeError("probe_signature_absent", "Native TransformLocation was not found uniquely")
    method = matches[0]
    fields, size = method.get("parameters"), method.get("parameter_size")
    if (not isinstance(fields, list) or len(fields) != 3 or any(not isinstance(field, dict) for field in fields)
            or method.get("parameters_truncated") is not False or type(size) is not int or not 0 <= size <= 160
            or type(method.get("parameter_count")) is not int or method["parameter_count"] != 3):
        raise ProbeError("probe_signature_mismatch", "TransformLocation requires exactly two inputs and one Vector return")
    spans, transform_name, vector_name, return_count = [], None, None, 0
    for field in fields:
        flags, offset, width, name = field.get("flags"), field.get("offset"), field.get("element_size"), field.get("name")
        if (not isinstance(name, str) or not name or type(flags) is not int or not 0 <= flags < 2**64 or not flags & 0x80
                or type(field.get("return")) is not bool or bool(flags & 0x400) is not field["return"]
                or bool(flags & 0x100) is not field.get("out") or bool(flags & 0x2) is not field.get("const")
                or bool(flags & 0x08000000) is not field.get("reference") or type(offset) is not int or offset < 0
                or type(width) is not int or width <= 0 or offset + width > 160 or field.get("type") != "StructProperty"
                or type(field.get("array_dim")) is not int or field["array_dim"] != 1
                or field["return"] is False and offset + width > size):
            raise ProbeError("probe_signature_flags", "Native TransformLocation CPF flags, types, or bounds are not confirmed")
        if any(offset < end and start < offset + width for start, end in spans):
            raise ProbeError("probe_signature_mismatch", "Native TransformLocation parameters overlap")
        spans.append((offset, offset + width))
        if field["return"]:
            if width != 24 or flags & (0x2 | 0x08000000):
                raise ProbeError("probe_signature_mismatch", "TransformLocation must return Vector24 by value")
            check_parameter_vector_metadata(field, 24, "return")
            return_count += 1
        elif width == 96:
            if (transform_name is not None or not flags & 0x2 or not flags & (0x100 | 0x08000000)
                    or field.get("direction") != "input" or field.get("passing") != "const_reference" or "argument_marker" in field):
                raise ProbeError("probe_transform_policy", "Native TransformLocation T did not confirm a single const-reference Transform")
            metadata = check_transform_metadata(field.get("struct"))
            if offset % metadata["min_alignment"]:
                raise ProbeError("probe_transform_layout", "Native Transform offset does not match public alignment")
            transform_name = name
        elif width == 24:
            const_input = bool(flags & 0x2)
            if (vector_name is not None or field.get("direction") != "input" or "argument_marker" in field
                    or const_input and (not flags & (0x100 | 0x08000000) or field.get("passing") != "const_reference")
                    or not const_input and (flags & (0x100 | 0x08000000) or field.get("passing") != "value")):
                raise ProbeError("probe_transform_policy", "Native TransformLocation point did not confirm a value or const-reference Vector")
            check_parameter_vector_metadata(field, 24, "input")
            vector_name = name
        else:
            raise ProbeError("probe_signature_mismatch", "Native TransformLocation contains an extra or unsupported input")
    if transform_name is None or vector_name is None or return_count != 1 or transform_name == vector_name:
        raise ProbeError("probe_signature_mismatch", "Native TransformLocation inputs and return are not identified uniquely")
    token = method.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "Native TransformLocation did not receive a signature token")
    return method, token, transform_name, vector_name


async def native_proof(call, report):
    report["native_math"] = {"tested": False}
    found = await call("g1r_find_objects", {"query": "Default__" + NATIVE_CLASS, "class_query": NATIVE_CLASS,
                                          "exact_path": NATIVE_PATH, "limit": 2})
    if (found.get("truncated") is not False or not isinstance(found.get("items"), list) or len(found["items"]) != 1):
        raise ProbeError("probe_object_absent", "Exact native KismetMathLibrary CDO was not found uniquely")
    target = found["items"][0]
    if (not isinstance(target, dict) or target.get("name") != "Default__" + NATIVE_CLASS or target.get("class") != NATIVE_CLASS
            or target.get("path") != NATIVE_PATH or not isinstance(target.get("handle"), str) or not target["handle"].startswith("obj-")):
        raise ProbeError("unsafe_probe_target", "Exact native Kismet math CDO is not confirmed")
    listing = await call("g1r_object_functions", {"handle": target["handle"], "function_query": NATIVE_METHOD, "limit": 10})
    report["native_math"].update(target=target, reflected_functions=listing)
    _, token, transform_name, vector_name = check_native_signature(listing)
    transform = copy.deepcopy(IDENTITY)
    transform.update(Translation={"X": 1.25, "Y": -2.5, "Z": 3.75}, Scale3D={"X": 2, "Y": 0.5, "Z": -1})
    point, expected = {"X": 1, "Y": 2, "Z": 3}, {"X": 3.25, "Y": -1.5, "Z": 0.75}
    result = await call("g1r_invoke", {"handle": target["handle"], "function": NATIVE_METHOD,
        "arguments": {transform_name: transform, vector_name: point}, "expected_signature_token": token})
    if result.get("executed") is not True or result.get("return_type") != "StructProperty":
        raise ProbeError("probe_transform_native_result", "Native TransformLocation did not confirm a Vector return")
    check_vector(result.get("result"), expected)
    check_parameter_vector_metadata({"struct": result.get("result_metadata"), "direction": "return"}, 24, "return")
    report["native_math"].update(tested=True, signature_token=token, transform_parameter=transform_name,
        point_parameter=vector_name, transform=transform, point=point, result=result["result"])
    report["checks"].append("native_kismet_const_transform_location_identity_rotation_signed_scale_translation_point_exact_result")


async def exercise(call):
    return await transform_exercise(call, const_reference=True, native_probe=native_proof)


async def run(args):
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "Const Transform probe uses only local MCP")
        async with streamable_http_client(args.mcp_url) as connection:
            async with ClientSession(connection[0], connection[1]) as session:
                initialized = await session.initialize()
                return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)], cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main():
    parser = argparse.ArgumentParser(description="Check dev.20 const Transform input on an owned fixture and native math")
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
