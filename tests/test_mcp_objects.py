"""MCP → TCP объектные контракты на изолированном стенде, без игры."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tests.test_mcp_observations import SessionNativeFixture, watch_metadata


SESSION = "fixture-session-1"


def descriptor(handle="obj-2", *, session=SESSION):
    return {"handle": handle, "index": 500, "name": "Default__ReferencedProbe",
            "path": "/Script/Angelscript.Default__ReferencedProbe", "class": "ReferencedProbe",
            "session_id": session}


def snapshot(value, tick=42, *, session=SESSION):
    return {"snapshot_id": f"snapshot-{tick}", "source": "live_native", "session_id": session,
            "process_id": 123, "thread_id": 456, "tick_count": tick,
            "consistency": "single_tick_sequential", "partial": False, "truncated": False,
            "limits": {"objects": 16, "properties_per_object": 32, "scan_fields_per_object": 4096,
                       "property_value_bytes": 16384, "snapshot_bytes": 262144},
            "objects": [{"handle": "obj-1", "path": "/Script/Angelscript.Default__ObjectProbe",
                         "class": "ObjectProbe", "partial": False, "truncated": False,
                         "properties": [{"name": "StoredObject", "type": "ObjectProperty",
                                         "supported": True, "value": copy.deepcopy(value)}]}]}


def invoked(function, value):
    return {"executed": True, "function": function, "result": copy.deepcopy(value),
            "return_type": "ObjectProperty", "thread_id": 456}


def echo_signature():
    # AngelScript сообщает ParmsSize входа; ReturnValue располагается за ним.
    return {"items": [{"name": "EchoObject", "owner": "ObjectProbe", "flags": 67108880,
                       "parameter_count": 2, "parameter_size": 8, "return_offset": 8,
                       "parameters": [{"name": "Input", "type": "ObjectProperty", "offset": 0,
                                       "element_size": 8, "out": False, "reference": False, "return": False},
                                      {"name": "ReturnValue", "type": "ObjectProperty", "offset": 8,
                                       "element_size": 8, "out": True, "reference": False, "return": True}]}],
            "truncated": False}


class MCPObjectTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    def parameters(self, root, port):
        modkit = root / "Modkit"
        script = modkit / "Script" / "Probe.as"
        script.parent.mkdir(parents=True)
        script.write_text("class UProbe {}\n", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        return StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(port)],
        )

    async def call_ok(self, client, name, parameters, expected):
        result = await client.call_tool(name, parameters)
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(result.structured_content, expected)
        return result.structured_content

    async def call_error(self, client, name, parameters, code):
        result = await client.call_tool(name, parameters)
        self.assertTrue(result.is_error, result.content)
        text = next(block.text for block in result.content if block.type == "text")
        error = json.loads(text[text.index("{"):])
        self.assertEqual(error["code"], code)
        return error

    async def test_object_values_and_null_transitions_preserve_identity_and_copies(self):
        reference = descriptor()
        initial = snapshot(None)
        selected = snapshot(reference, 43)
        stable = snapshot(reference, 44)
        restored = snapshot(None, 45)
        targets = [{"handle": "obj-1", "properties": ["StoredObject"]}]
        watch_params = {"handle": "obj-1", "properties": ["StoredObject"], "duration_ms": 1000,
                        "interval_ms": 20, "capacity": 4}
        started = dict(watch_metadata(), start_seq=1, duration_ms=1000, interval_ms=20)
        baseline = {"seq": 1, "kind": "baseline", "tick_count": 42, "elapsed_ms": 0,
                    "snapshot": initial}
        changed = {"seq": 2, "kind": "changed", "tick_count": 43, "elapsed_ms": 20,
                   "snapshot": selected}
        null_again = {"seq": 3, "kind": "changed", "tick_count": 45, "elapsed_ms": 60,
                      "snapshot": restored}
        running = dict(watch_metadata(next_seq=4), events=[baseline, changed, null_again],
                       cursor=3, gap=False, truncated=False)
        stopped = watch_metadata(state="stopped", reason="explicit_stop", next_seq=5)
        forgotten = {"watch_id": "watch-1", "forgotten": True, "cleanup_complete": True}
        signature = echo_signature()
        echo_params = {"handle": "obj-1", "function": "EchoObject", "arguments": {"Input": {"handle": "obj-2"}}}
        null_params = {"handle": "obj-1", "function": "EchoObject", "arguments": {"Input": None}}
        steps = [
            {"op": "functions", "params": {"handle": "obj-1", "limit": 50}, "result": signature},
            {"op": "invoke", "params": echo_params, "result": invoked("EchoObject", reference)},
            {"op": "invoke", "params": null_params, "result": invoked("EchoObject", None)},
            {"op": "snapshot", "params": {"targets": targets}, "result": initial},
            {"op": "snapshot", "params": {"targets": targets}, "result": selected},
            {"op": "snapshot", "params": {"targets": targets}, "result": stable},
            {"op": "watch_start", "params": watch_params, "result": started, "session_owner": True},
            {"op": "watch_read", "params": {"watch_id": "watch-1", "after_seq": 0, "limit": 100}, "result": running, "session_owner": True},
            {"op": "watch_stop", "params": {"watch_id": "watch-1"}, "result": stopped, "session_owner": True},
            {"op": "watch_forget", "params": {"watch_id": "watch-1"}, "result": forgotten, "session_owner": True},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            async with stdio_client(self.parameters(Path(directory), native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    functions = await self.call_ok(client, "sn2_object_functions", {"handle": "obj-1"}, signature)
                    method = functions["items"][0]
                    self.assertEqual(method["parameter_size"], 8)
                    self.assertEqual(method["parameters"][1]["offset"] + method["parameters"][1]["element_size"], 16)
                    await self.call_ok(client, "sn2_invoke", echo_params, invoked("EchoObject", reference))
                    await self.call_ok(client, "sn2_invoke", null_params, invoked("EchoObject", None))
                    before = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, initial)
                    after = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, selected)
                    unchanged = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, stable)
                    originals = copy.deepcopy((before, after, unchanged))
                    difference = await client.call_tool("sn2_snapshot_compare", {"before": before, "after": after})
                    self.assertFalse(difference.is_error, difference.content)
                    self.assertEqual(difference.structured_content["value_changes"], 1)
                    self.assertEqual(difference.structured_content["type_changes"], 0)
                    self.assertEqual(difference.structured_content["unknown_count"], 0)
                    self.assertFalse(difference.structured_content["equal_selected_values"])
                    same = await client.call_tool("sn2_snapshot_compare", {"before": after, "after": unchanged})
                    self.assertFalse(same.is_error, same.content)
                    self.assertEqual(same.structured_content["items"], [])
                    self.assertTrue(same.structured_content["equal_selected_values"])
                    self.assertEqual((before, after, unchanged), originals)
                    await self.call_ok(client, "sn2_watch_start", watch_params, started)
                    events = await self.call_ok(client, "sn2_watch_read", {"watch_id": "watch-1"}, running)
                    self.assertEqual([event["kind"] for event in events["events"]], ["baseline", "changed", "changed"])
                    values = [event["snapshot"]["objects"][0]["properties"][0]["value"] for event in events["events"]]
                    self.assertEqual(values, [None, reference, None])
                    await self.call_ok(client, "sn2_watch_stop", {"watch_id": "watch-1"}, stopped)
                    await self.call_ok(client, "sn2_watch_forget", {"watch_id": "watch-1"}, forgotten)
            self.assertEqual(native.connections, 1)
            self.assertEqual(len(native.requests), 10)  # Сравнение копий не создаёт игровых команд.
            self.assertEqual(len({request["id"] for request in native.requests}), 10)

    async def test_incompatible_and_stale_arguments_are_not_replayed(self):
        reference = descriptor()
        incompatible = {"handle": "obj-1", "function": "EchoTypedObject", "arguments": {"Input": {"handle": "obj-other"}}}
        compatible = {"handle": "obj-1", "function": "EchoTypedObject", "arguments": {"Input": {"handle": "obj-2"}}}
        old = {"handle": "obj-previous-owner", "function": "EchoObject", "arguments": {"Input": None}}
        status = {"version": "1.0.0-dev.4", "ready": True, "process_id": 123,
                  "game_thread_id": 456, "tick_count": 50, "session_id": "fixture-session-2", "protocol": 1}
        steps = [
            {"op": "invoke", "params": incompatible,
             "error": {"code": "incompatible_object_argument", "message": "Класс аргумента не совместим"}},
            {"op": "invoke", "params": compatible, "result": invoked("EchoTypedObject", reference)},
            {"op": "invoke", "params": compatible, "drop": True},
            {"op": "status", "params": {}, "result": status},
            {"op": "invoke", "params": old,
             "error": {"code": "stale_handle", "message": "Ссылка другой сессии не существует"}},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            async with stdio_client(self.parameters(Path(directory), native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    rejected = await self.call_error(client, "sn2_invoke", incompatible, "incompatible_object_argument")
                    self.assertEqual(rejected["request_id"], native.requests[0]["id"])
                    await self.call_ok(client, "sn2_invoke", compatible, invoked("EchoTypedObject", reference))
                    lost = await self.call_error(client, "sn2_invoke", compatible, "native_disconnected")
                    self.assertTrue(lost["may_have_executed"])
                    self.assertEqual(lost["request_id"], native.requests[2]["id"])
                    await self.call_ok(client, "sn2_native_status", {}, status)
                    stale = await self.call_error(client, "sn2_invoke", old, "stale_handle")
                    self.assertEqual(stale["request_id"], native.requests[4]["id"])
            self.assertEqual(native.connections, 2)
            self.assertEqual([request["op"] for request in native.requests], ["invoke", "invoke", "invoke", "status", "invoke"])
            self.assertEqual(len({request["id"] for request in native.requests}), 5)


if __name__ == "__main__":
    unittest.main()
