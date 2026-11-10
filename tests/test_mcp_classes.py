"""ClassProperty MCP → TCP: классовые ссылки и эффекты отказов, без игры."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture


SESSION = "fixture-session-1"


def class_descriptor(handle="class-1", *, name="ClassProbe", kind="AngelscriptClass", index=501):
    return {"handle": handle, "index": index, "name": name,
            "path": f"/Script/Angelscript.{name}", "class": kind, "session_id": SESSION}


def class_snapshot(value, *, tick=42):
    return {"snapshot_id": f"snapshot-{tick}", "source": "live_native", "session_id": SESSION,
            "process_id": 123, "thread_id": 456, "tick_count": tick,
            "consistency": "single_tick_sequential", "partial": False, "truncated": False,
            "limits": {"objects": 16, "properties_per_object": 32, "scan_fields_per_object": 4096,
                       "property_value_bytes": 16384, "snapshot_bytes": 262144},
            "objects": [{"handle": "obj-probe", "path": "/Script/Angelscript.Default__ClassProbe",
                         "class": "ClassProbe", "partial": False, "truncated": False,
                         "properties": [{"name": "StoredClass", "type": "ClassProperty",
                                         "supported": True, "value": copy.deepcopy(value)}]}]}


def class_signature():
    return {"items": [{"name": "EchoTypedClass", "owner": "ClassProbe", "flags": 67108880,
                       "parameter_count": 2, "parameter_size": 8, "return_offset": 8,
                       "parameters": [
                           {"name": "Input", "type": "ClassProperty", "offset": 0,
                            "element_size": 8, "array_dim": 1, "flags": 128},
                           {"name": "ReturnValue", "type": "ClassProperty", "offset": 8,
                            "element_size": 8, "array_dim": 1, "flags": 1024},
                       ]}], "truncated": False}


def invoked(value):
    return {"function": "EchoTypedClass", "return_type": "ClassProperty", "result": copy.deepcopy(value),
            "executed": True, "thread_id": 456}


class MCPClassTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    def parameters(self, root, port):
        modkit = root / "Modkit"
        script = modkit / "Script/Probe.as"
        script.parent.mkdir(parents=True)
        script.write_text("class UClassProbe {}\n", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        return StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "g1r_mcp.server", "--modkit-root", str(modkit),
                  "--workspace-root", str(workspace), "--native-port", str(port)])

    async def call_ok(self, client, name, params, expected=None):
        response = await client.call_tool(name, params)
        self.assertFalse(response.is_error, response.content)
        if expected is not None:
            self.assertEqual(response.structured_content, expected)
        return response.structured_content

    async def call_error(self, client, name, params, code):
        response = await client.call_tool(name, params)
        self.assertTrue(response.is_error, response.content)
        text = next(block.text for block in response.content if block.type == "text")
        error = json.loads(text[text.index("{"):])
        self.assertEqual(error["code"], code)
        return error

    async def test_class_returns_null_subtype_and_copied_snapshot_identity(self):
        base = class_descriptor()
        child = class_descriptor("class-2", name="DerivedClassProbe", index=502)
        native_class = class_descriptor("class-native", name="Object", kind="Class", index=15)
        native_class["path"] = "/Script/CoreUObject.Object"
        initial = class_snapshot(None)
        selected = class_snapshot(base, tick=43)
        same = class_snapshot(base, tick=44)
        derived = class_snapshot(child, tick=45)
        targets = [{"handle": "obj-probe", "properties": ["StoredClass"]}]
        params = lambda value: {"handle": "obj-probe", "function": "EchoTypedClass", "arguments": {"Input": value}}
        steps = [
            {"op": "functions", "params": {"handle": "obj-probe", "limit": 50}, "result": class_signature()},
            {"op": "invoke", "params": params({"handle": "class-1"}), "result": invoked(base)},
            {"op": "invoke", "params": params(None), "result": invoked(None)},
            {"op": "invoke", "params": params({"handle": "class-2"}), "result": invoked(child)},
            {"op": "snapshot", "params": {"targets": targets}, "result": initial},
            {"op": "snapshot", "params": {"targets": targets}, "result": selected},
            {"op": "snapshot", "params": {"targets": targets}, "result": same},
            {"op": "snapshot", "params": {"targets": targets}, "result": derived},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            async with stdio_client(self.parameters(Path(directory), native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    signature = await self.call_ok(client, "g1r_object_functions", {"handle": "obj-probe"}, class_signature())
                    self.assertEqual(signature["items"][0]["parameters"][1]["offset"], 8)
                    for input_value, expected in (({"handle": "class-1"}, base), (None, None), ({"handle": "class-2"}, child)):
                        await self.call_ok(client, "g1r_invoke", params(input_value), invoked(expected))
                    snapshots = [await self.call_ok(client, "g1r_snapshot", {"targets": targets}, expected)
                                 for expected in (initial, selected, same, derived)]
                    originals = copy.deepcopy(snapshots)
                    changed = await self.call_ok(client, "g1r_compare_snapshots", {"before": snapshots[0], "after": snapshots[1]})
                    self.assertEqual((changed["type_changes"], changed["value_changes"], changed["unknown_count"]), (0, 1, 0))
                    stable = await self.call_ok(client, "g1r_compare_snapshots", {"before": snapshots[1], "after": snapshots[2]})
                    self.assertTrue(stable["equal_selected_values"])
                    subclass = await self.call_ok(client, "g1r_compare_snapshots", {"before": snapshots[2], "after": snapshots[3]})
                    self.assertEqual((subclass["type_changes"], subclass["value_changes"]), (0, 1))
                    for value in (native_class, base, {**base, "class": "FutureSubclassOfClass"}):
                        copied = class_snapshot(value)
                        response = await self.call_ok(client, "g1r_compare_snapshots", {"before": copied, "after": copied})
                        self.assertTrue(response["equal_selected_values"])
                        self.assertEqual(response["provenance"], "unverified")
                        self.assertEqual(response["validation"], "client_supplied_copies")
                    conflicting = class_snapshot({**base, "class": "DifferentMetaclass"})
                    unknown = await self.call_ok(client, "g1r_compare_snapshots", {"before": selected, "after": conflicting})
                    self.assertEqual((unknown["value_changes"], unknown["unknown_count"]), (0, 1))
                    self.assertEqual(snapshots, originals)
            self.assertEqual(native.connections, 1)
            self.assertEqual(len(native.requests), 8)
            self.assertEqual(len({request["id"] for request in native.requests}), 8)

    async def test_wrong_class_and_plain_object_are_pre_event_errors_without_replay(self):
        params = lambda handle: {"handle": "obj-probe", "function": "EchoTypedClass", "arguments": {"Input": {"handle": handle}}}
        effect_false = {"executed": False, "may_have_executed": False, "do_not_retry": False}
        result_error = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                        "cause_code": "incompatible_class_result"}
        status = {"version": "1.0.0-dev.6", "ready": True, "session_id": "fixture-session-2",
                  "process_id": 123, "game_thread_id": 456, "tick_count": 80}
        stale = {"handle": "obj-previous-session", "function": "EchoTypedClass", "arguments": {"Input": None}}
        steps = [
            {"op": "invoke", "params": params("class-unrelated"),
             "error": {"code": "incompatible_class_argument", "message": "Представляемый класс не совместим", "details": effect_false}},
            {"op": "invoke", "params": params("obj-cdo"),
             "error": {"code": "invalid_class_argument", "message": "Обычный UObject не является UClass", "details": effect_false}},
            {"op": "invoke", "params": params("class-1"), "result": invoked(class_descriptor())},
            {"op": "invoke", "params": params("class-1"),
             "error": {"code": "game_result_error", "message": "Вызов выполнен, результат не получен", "details": result_error}},
            {"op": "invoke", "params": params("class-1"), "drop": True},
            {"op": "status", "params": {}, "result": status},
            {"op": "invoke", "params": stale,
             "error": {"code": "stale_handle", "message": "Ссылка старой сессии", "details": effect_false}},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            async with stdio_client(self.parameters(Path(directory), native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    for handle, code in (("class-unrelated", "incompatible_class_argument"), ("obj-cdo", "invalid_class_argument")):
                        error = await self.call_error(client, "g1r_invoke", params(handle), code)
                        self.assertFalse(error["may_have_executed"])
                        self.assertEqual(error["effect"], effect_false)
                    await self.call_ok(client, "g1r_invoke", params("class-1"), invoked(class_descriptor()))
                    result = await self.call_error(client, "g1r_invoke", params("class-1"), "game_result_error")
                    self.assertTrue(result["may_have_executed"])
                    self.assertEqual(result["effect"], result_error)
                    lost = await self.call_error(client, "g1r_invoke", params("class-1"), "native_disconnected")
                    self.assertTrue(lost["may_have_executed"])
                    await self.call_ok(client, "g1r_native_status", {}, status)
                    old = await self.call_error(client, "g1r_invoke", stale, "stale_handle")
                    self.assertFalse(old["may_have_executed"])
            self.assertEqual(native.connections, 2)
            self.assertEqual(len(native.requests), 7)
            self.assertEqual([request["op"] for request in native.requests], ["invoke"] * 5 + ["status", "invoke"])
            self.assertEqual(len({request["id"] for request in native.requests}), 7)


if __name__ == "__main__":
    unittest.main()
