"""Check the reflection token and selected properties of the owned NameProbe through MCP."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import sys

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "tools"))
from probe_cleanup import deferred_sigint, settle_task
from probe_name_mcp import HOST, check_target, check_input_signature
from probe_object_mcp import ProbeError, error_payload, _single_error

TOKEN = re.compile(r"sig-[1-9][0-9]{0,19}\Z")
KNOWN_NAMES = ["StoredNone", "StoredEntry", "CallsCount"]
SELECTED_NAMES = ["StoredNone", "StoredEntry", "MissingForProbe", "CallsCount"]


def check_token(functions: dict, method: str) -> str:
    signature = check_input_signature(functions, method)
    token = signature.get("signature_token")
    if not isinstance(token, str) or not TOKEN.fullmatch(token):
        raise ProbeError("probe_signature_token_absent", "The full signature of the owned method did not receive a valid token")
    return token


def check_selected(response: dict, names: list[str], *, include_missing: bool) -> dict:
    fields = response.get("items")
    if (not isinstance(fields, list) or any(not isinstance(field, dict) for field in fields)
            or [field.get("name") for field in fields] != names
            or response.get("truncated") is not False
            or response.get("partial") is not include_missing):
        raise ProbeError("probe_property_selection_invalid", "The property selection did not preserve order or completeness")
    expected = {"StoredNone": ("NameProperty", "None"),
                "StoredEntry": ("NameProperty", "Entry"), "CallsCount": ("IntProperty", 0)}
    values = {}
    for field in fields:
        name = field["name"]
        if name == "MissingForProbe":
            if (field.get("type") is not None or field.get("supported") is not False
                    or field.get("error", {}).get("code") != "property_not_found"
                    or "value" in field):
                raise ProbeError("probe_missing_property_invalid", "The missing property did not receive an explicit rejection")
            continue
        field_type, value = expected[name]
        if (field.get("type") != field_type or field.get("supported") is not True
                or "error" in field or type(field.get("value")) is not type(value)
                or field["value"] != value):
            raise ProbeError("probe_property_value_invalid", "Owned names or the zero counter changed")
        values[name] = field["value"]
    return values


def check_invocation(result: dict, value: int) -> None:
    if (result.get("executed") is not True or type(result.get("result")) is not int
            or result["result"] != value):
        raise ProbeError("probe_contract_call_unconfirmed", "The call with the verified token was not confirmed")


def check_rejection(result: dict, code: str) -> None:
    if (result.get("code") != code or result.get("may_have_executed") is not False
            or result.get("executed") is True):
        raise ProbeError("probe_contract_rejection_unconfirmed", "Token rejection before execution was not proven")


async def _exercise(call, cleanup_state: dict) -> dict:
    """All EchoName calls must be rejected; Reset restores the CDO after the probes."""
    status = await call("sn2_native_status", {})
    session_id = status.get("session_id")
    if status.get("ready") is not True or not isinstance(session_id, str) or not session_id:
        raise ProbeError("game_not_ready", "The ready queue and native session owner were not confirmed")
    found = await call("sn2_find_objects", {"query": "Default__" + HOST, "class_query": HOST, "limit": 50})
    exact = [item for item in found.get("items", [])
             if isinstance(item, dict) and item.get("name") == "Default__" + HOST]
    if len(exact) != 1:
        raise ProbeError("probe_object_absent", "The owned NameProbe CDO was not uniquely found")
    target = check_target(exact[0])
    handle = target["handle"]
    tokens = {}
    for method in ("ReadMarker", "ReadCallsCount", "EchoName", "ResetProbe"):
        functions = await call("sn2_object_functions", {"handle": handle, "function_query": method, "limit": 10})
        tokens[method] = check_token(functions, method)
    if len(set(tokens.values())) != len(tokens):
        raise ProbeError("probe_signature_token_alias", "Different functions received the same contract token")

    async def read_count():
        result = await call("sn2_invoke", {"handle": handle, "function": "ReadCallsCount", "arguments": {},
                                           "expected_signature_token": tokens["ReadCallsCount"]})
        check_invocation(result, 0)

    await read_count()
    initial = await call("sn2_object_properties", {"handle": handle, "limit": 200, "property_names": KNOWN_NAMES})
    check_selected(initial, KNOWN_NAMES, include_missing=False)
    cleanup_state.update(handle=handle, tokens=tokens)
    marker = await call("sn2_invoke", {"handle": handle, "function": "ReadMarker", "arguments": {},
                                      "expected_signature_token": tokens["ReadMarker"]})
    check_invocation(marker, 71)
    selected = await call("sn2_object_properties", {"handle": handle, "limit": 200, "property_names": SELECTED_NAMES})
    check_selected(selected, SELECTED_NAMES, include_missing=True)

    failures = []
    cleanup_state["attempted"] = True
    for token, code in (("bad-token", "invalid_signature_token"),
                        ("sig-99999999999999999999", "unknown_signature_token"),
                        (tokens["ReadMarker"], "signature_token_mismatch")):
        result = await call("sn2_invoke", {"handle": handle, "function": "EchoName",
                          "arguments": {"Input": {"name_from": {"handle": handle, "property_name": "StoredEntry"}}},
                          "expected_signature_token": token}, expected_error=code)
        check_rejection(result, code)
        await read_count()
        failures.append(code)
    for names in ([], ["StoredEntry", "StoredEntry"], ["x"] * 33, ["StoredEntry\0"]):
        result = await call("sn2_object_properties", {"handle": handle, "limit": 200, "property_names": names},
                            expected_error="invalid_property_names")
        check_rejection(result, "invalid_property_names")
        failures.append("invalid_property_names")
    final = await call("sn2_object_properties", {"handle": handle, "limit": 200, "property_names": KNOWN_NAMES})
    check_selected(final, KNOWN_NAMES, include_missing=False)
    await read_count()
    return {"session_id": session_id, "handle": handle, "signature_tokens": tokens,
            "checks": ["complete_reflection_tokens", "token_bound_marker_return71",
                       "property_order_and_explicit_missing", "token_refusals_before_echo",
                       "invalid_property_selection_refused", "unchanged_names_and_zero_counter"],
            "rejections": failures, "cleanup": "ResetProbe_and_confirmed_names_zero_counter",
            "game_launched": False, "dll_loaded": False,
            "changed_runtime_signature": "not_tested", "other_native_session_token": "not_tested"}


async def exercise(call) -> dict:
    """Check a ready fixture without starting the game; perform one cancellation-protected Reset."""
    cleanup_state = {"attempted": False}
    failure = None
    try:
        return await _exercise(call, cleanup_state)
    except BaseException as error:
        failure = error
        raise
    finally:
        if cleanup_state["attempted"]:
            async def finish_cleanup():
                handle = cleanup_state["handle"]
                tokens = cleanup_state["tokens"]
                result = await call("sn2_invoke", {"handle": handle, "function": "ResetProbe", "arguments": {},
                                                  "expected_signature_token": tokens["ResetProbe"]})
                if result.get("executed") is not True or result.get("result") is not None:
                    raise ProbeError("probe_contract_cleanup_unconfirmed", "The owned ResetProbe was not confirmed")
                fields = await call("sn2_object_properties", {"handle": handle, "limit": 200, "property_names": KNOWN_NAMES})
                check_selected(fields, KNOWN_NAMES, include_missing=False)
                count = await call("sn2_invoke", {"handle": handle, "function": "ReadCallsCount", "arguments": {},
                                                  "expected_signature_token": tokens["ReadCallsCount"]})
                check_invocation(count, 0)
            try:
                await settle_task(asyncio.create_task(finish_cleanup()), timeout=30, label="function contract cleanup")
            except BaseException as error:
                if failure is None:
                    raise
                failure.add_note("NameProbe cleanup was not confirmed: " + str(error))


def make_caller(session):
    async def call(name, arguments, *, expected_error=None):
        result = await session.call_tool(name, arguments)
        if expected_error is not None:
            error = error_payload(result)
            if result.is_error is not True:
                raise ProbeError("probe_contract_rejection_unconfirmed", "The expected MCP rejection is missing")
            check_rejection(error, expected_error)
            return error
        if result.is_error:
            error = error_payload(result)
            raise ProbeError(error.get("code", "mcp_error"), error.get("message", "MCP error"))
        if not isinstance(result.structured_content, dict):
            raise ProbeError("mcp_result_invalid", "A structured MCP response is expected")
        return result.structured_content
    return call


async def run(args):
    if args.mcp_url:
        if args.mcp_url not in {"http://127.0.0.1:27124/mcp", "http://localhost:27124/mcp"}:
            raise ProbeError("invalid_mcp_url", "The owned CDO probe uses only the configured local MCP")
        async with streamable_http_client(args.mcp_url) as connection:
            async with ClientSession(connection[0], connection[1]) as session:
                initialized = await session.initialize()
                return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}
    parameters = StdioServerParameters(command=sys.executable,
        args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(args.modkit_root),
              "--workspace-root", str(args.workspace_root), "--native-port", str(args.native_port)],
        cwd=str(REPOSITORY_ROOT))
    async with stdio_client(parameters) as (incoming, outgoing):
        async with ClientSession(incoming, outgoing) as session:
            initialized = await session.initialize()
            return {"server_version": initialized.server_info.version, **await exercise(make_caller(session))}


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dev.14 contracts on an already loaded owned NameProbe")
    parser.add_argument("--mcp-url")
    parser.add_argument("--modkit-root", type=Path, default=REPOSITORY_ROOT.parent / "G1R-Modkit")
    parser.add_argument("--workspace-root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--native-port", type=int, default=27123)
    args = parser.parse_args()
    async def protected():
        with deferred_sigint():
            return await run(args)
    try:
        print(json.dumps(asyncio.run(protected()), indent=2))
        return 0
    except BaseException as error:
        error = _single_error(error)
        print(json.dumps({"code": getattr(error, "code", "probe_failed"), "message": (f"The MCP client operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    sys.exit(main())
