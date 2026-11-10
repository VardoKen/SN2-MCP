"""MCP-to-TCP snapshot and shared-observation contracts without a game."""

import copy
import json
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import unittest

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def native_snapshot(*, session="fixture-session-1", value=True, partial=False, tick=42):
    properties = [{"name": "Enabled", "type": "BoolProperty", "supported": True, "value": value}]
    if partial:
        properties.append({"name": "Missing", "type": None, "supported": False,
                           "error": {"code": "property_not_found", "message": "Свойство не найдено"}})
    return {
        "snapshot_id": f"snapshot-{tick}", "source": "live_native", "session_id": session,
        "process_id": 123, "thread_id": 456, "tick_count": tick,
        "consistency": "single_tick_sequential", "partial": partial, "truncated": False,
        "limits": {"objects": None, "properties_per_object": None, "scan_fields_per_object": None,
                   "property_value_bytes": None, "snapshot_bytes": None},
        "objects": [{"handle": "obj-1", "path": "/Script/Probe.Default__Probe", "class": "Probe",
                     "properties": properties, "partial": partial, "truncated": False}],
    }


def watch_metadata(*, session="fixture-session-1", state="running", reason="", next_seq=2,
                   dropped=0, oldest_seq=1):
    return {"watch_id": "watch-1", "session_id": session, "state": state, "reason": reason,
            "cleanup_complete": state != "running", "next_seq": next_seq,
            "last_seq": next_seq - 1, "oldest_seq": oldest_seq, "dropped_events": dropped,
            "capacity": 2, "buffer_bytes": 1024, "buffer_limit_bytes": None}


class SessionNativeFixture:
    """Scripted responses with connection loss and shared client metadata."""

    def __init__(self, steps):
        self.steps = steps
        self.requests = []
        self.response_frame_lengths = []
        self.response_sessions = []
        self.watch_client_owner = None
        self.connections = 0
        self.errors = []
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.stream = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def receive(self, count):
        result = bytearray()
        while len(result) < count and not self.stop.is_set():
            try:
                data = self.stream.recv(count - len(result))
            except socket.timeout:
                continue
            if not data:
                raise EOFError
            result.extend(data)
        if len(result) != count:
            raise EOFError
        return bytes(result)

    def run(self):
        try:
            while len(self.requests) < len(self.steps) and not self.stop.is_set():
                if self.stream is None:
                    try:
                        self.stream, _ = self.listener.accept()
                    except socket.timeout:
                        continue
                    self.connections += 1
                    self.stream.settimeout(0.1)
                try:
                    length = struct.unpack(">I", self.receive(4))[0]
                    if not 0 < length <= 1024 * 1024:
                        raise AssertionError("Неверная длина нативного запроса")
                    request = json.loads(self.receive(length).decode("utf-8"))
                except EOFError:
                    self.stream.close()
                    self.stream = None
                    continue
                step = self.steps[len(self.requests)]
                self.requests.append(request)
                parameters = dict(request.get("params", {}))
                if step.get("session_owner") is True:
                    owner = parameters.pop("client_owner", None)
                    if owner != "shared_native_connection":
                        raise AssertionError("The observation route did not use shared client metadata")
                    if self.watch_client_owner is None:
                        self.watch_client_owner = owner
                    elif owner != self.watch_client_owner:
                        raise AssertionError("The shared observation client metadata changed")
                if request.get("protocol") != 1 or request.get("op") != step["op"] or parameters != step["params"]:
                    raise AssertionError(f"Неверный маршрут нативного запроса: {request}")
                if not isinstance(request.get("id"), str) or not request["id"]:
                    raise AssertionError("Нет корреляционного ID")
                if step.get("drop"):
                    self.stream.shutdown(socket.SHUT_RDWR)
                    self.stream.close()
                    self.stream = None
                    continue
                response = {"protocol": 1, "id": request["id"],
                            "session_id": f"fixture-session-{self.connections}"}
                if "error" in step:
                    response.update(type="error", error=step["error"])
                else:
                    response.update(type="response", result=step["result"])
                payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
                self.response_frame_lengths.append(len(payload))
                self.response_sessions.append(response["session_id"])
                frame = struct.pack(">I", len(payload)) + payload
                self.stream.sendall(frame[:3])
                self.stream.sendall(frame[3:15])
                self.stream.sendall(frame[15:])
        except (OSError, EOFError) as error:
            if not self.stop.is_set():
                self.errors.append(error)
        except Exception as error:
            self.errors.append(error)
        finally:
            if self.errors and self.stream is not None:
                try:
                    self.stream.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                self.stream.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop.set()
        self.listener.close()
        if self.stream is not None:
            try:
                self.stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.stream.close()
        self.thread.join(2)
        if exc_type is None:
            if self.thread.is_alive():
                raise AssertionError("Нативная fixture не завершилась")
            if self.errors:
                raise self.errors[0]
            if len(self.requests) != len(self.steps):
                raise AssertionError("Не все ожидаемые запросы были выполнены")


class MCPObservationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    def parameters(self, root, port):
        modkit = root / "Modkit"
        source = modkit / "Script/Probe.as"
        source.parent.mkdir(parents=True)
        source.write_text("class UProbe {}\n", encoding="utf-8")
        workspace = root / "workspace"
        workspace.mkdir()
        return StdioServerParameters(command=sys.executable,
            args=["-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(port)])

    async def call_ok(self, client, name, params, expected):
        answer = await client.call_tool(name, params)
        self.assertFalse(answer.is_error, answer.content)
        self.assertEqual(answer.structured_content, expected)
        return answer.structured_content

    def assert_fixture_errors(self, native):
        if native.errors:
            raise AssertionError(f"Ошибка native fixture: {native.errors[0]}") from native.errors[0]

    async def test_large_full_snapshot_and_more_than_200_differences_share_native_connection(self):
        baseline = native_snapshot()
        baseline["objects"] = [{"handle": f"obj-{index:02d}", "path": f"/Game/Fixture.Probe_{index}",
            "class": "Probe", "partial": False, "truncated": False,
            "properties": [{"name": f"Caption_{number:02d}", "type": "StrProperty", "supported": True,
                            "value": "Я" * 2048 + f"-{index}-{number}"} for number in range(33)]}
            for index in range(17)]
        changed = copy.deepcopy(baseline)
        changed.update(snapshot_id="snapshot-43", tick_count=43)
        for target in changed["objects"]:
            for prop in target["properties"]:
                prop["value"] += "!"
        targets = [{"handle": target["handle"], "properties": [prop["name"] for prop in target["properties"]]}
                   for target in baseline["objects"]]
        self.assertGreater(len(json.dumps(baseline, ensure_ascii=False).encode("utf-8")), 1048576)
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 44}
        steps = [{"op": "snapshot", "params": {"targets": targets}, "result": baseline},
                 {"op": "snapshot", "params": {"targets": targets}, "result": changed},
                 {"op": "status", "params": {}, "result": status}]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            root = Path(directory)
            async with stdio_client(self.parameters(root, native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    before = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, baseline)
                    after = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, changed)
                    self.assertEqual(len(before["objects"]), 17)
                    self.assertTrue(all(len(target["properties"]) == 33 for target in before["objects"]))
                    self.assertTrue(all(value is None for value in before["limits"].values()))
                    self.assertFalse(before["truncated"])
                    self.assertFalse(before["partial"])
                    self.assertEqual(json.dumps(before, ensure_ascii=False).encode("utf-8"),
                                     json.dumps(baseline, ensure_ascii=False).encode("utf-8"))
                    copies = copy.deepcopy((before, after))
                    difference = await client.call_tool("sn2_snapshot_compare", {"before": before, "after": after, "limit": 561})
                    self.assertFalse(difference.is_error, difference.content)
                    report = difference.structured_content
                    self.assertEqual(report["value_changes"], 561)
                    self.assertEqual(report["total_items"], 561)
                    self.assertEqual(len(report["items"]), 561)
                    self.assertFalse(report["truncated"])
                    self.assertFalse(report["equal_selected_values"])
                    self.assertTrue(report["coverage"]["complete"])
                    self.assertGreater(len(json.dumps(report, ensure_ascii=False).encode("utf-8")), 1048576)
                    self.assertEqual({(item["handle"], item["property"]) for item in report["items"]},
                                     {(target["handle"], prop["name"]) for target in before["objects"] for prop in target["properties"]})
                    self.assertEqual((before, after), copies)
                    self.assertEqual(len(native.requests), 2)
                    await self.call_ok(client, "sn2_native_status", {}, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["snapshot", "snapshot", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 3)
            self.assertTrue(all(length > 1048576 for length in native.response_frame_lengths[:2]))
            self.assertEqual(native.response_sessions, ["fixture-session-1"] * 3)

    async def test_actual_snapshot_and_watch_schemas_routes_and_terminal_result(self):
        targets = [{"handle": "obj-1", "properties": ["Enabled", "Missing"]}]
        baseline = native_snapshot(partial=True)
        changed = native_snapshot(value=False, partial=True, tick=43)
        start_params = {"handle": "obj-1", "properties": ["Enabled"], "duration_ms": 1000,
                        "interval_ms": 20, "capacity": 2}
        started = dict(watch_metadata(), start_seq=1, duration_ms=1000, interval_ms=20,
                       sampling="all_due_watches_per_tick_no_catchup")
        event = {"seq": 3, "kind": "changed", "elapsed_ms": 40, "tick_count": 44,
                 "snapshot": native_snapshot(value=False, tick=44)}
        running = dict(watch_metadata(next_seq=4, dropped=1, oldest_seq=2), events=[event],
                       gap=True, cursor=3, truncated=False)
        stopped = watch_metadata(state="stopped", reason="explicit_stop", next_seq=5,
                                 dropped=2, oldest_seq=3)
        terminal_event = {"seq": 4, "kind": "terminal", "elapsed_ms": 45, "tick_count": 45,
                          "reason": "explicit_stop", "state": "stopped"}
        final = dict(stopped, events=[terminal_event], gap=False, cursor=4, truncated=False)
        forgotten = {"watch_id": "watch-1", "forgotten": True, "cleanup_complete": True}
        steps = [
            {"op": "snapshot", "params": {"targets": targets}, "result": baseline},
            {"op": "snapshot", "params": {"targets": targets}, "result": changed},
            {"op": "watch_start", "params": start_params, "result": started, "session_owner": True},
            {"op": "watch_read", "params": {"watch_id": "watch-1", "after_seq": 0, "limit": 100}, "result": running, "session_owner": True},
            {"op": "watch_stop", "params": {"watch_id": "watch-1"}, "result": stopped, "session_owner": True},
            {"op": "watch_read", "params": {"watch_id": "watch-1", "after_seq": 3, "limit": 10}, "result": final, "session_owner": True},
            {"op": "watch_forget", "params": {"watch_id": "watch-1"}, "result": forgotten, "session_owner": True},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            root = Path(directory)
            async with stdio_client(self.parameters(root, native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), 22)
                    for name in ("sn2_snapshot", "sn2_snapshot_compare", "sn2_watch_read"):
                        self.assertTrue(tools[name].annotations.read_only_hint)
                    self.assertFalse(tools["sn2_watch_start"].annotations.read_only_hint)
                    self.assertFalse(tools["sn2_watch_start"].annotations.idempotent_hint)
                    self.assertTrue(tools["sn2_watch_stop"].annotations.idempotent_hint)
                    self.assertFalse(tools["sn2_watch_forget"].annotations.read_only_hint)
                    self.assertNotIn("sn2_development_recipe", tools)
                    self.assertNotIn("sn2_build_script_patch", tools)
                    self.assertNotIn("sn2_read_source_exact", tools)
                    for name in ("sn2_create_package_directory", "sn2_development_start", "sn2_development_stop"):
                        self.assertNotIn(name, tools)
                    self.assertFalse(tools["sn2_native_load"].annotations.read_only_hint)
                    self.assertTrue(tools["sn2_native_load"].annotations.open_world_hint)
                    self.assertFalse(tools["sn2_native_load"].annotations.idempotent_hint)
                    self.assertFalse(tools["sn2_native_load"].annotations.destructive_hint)
                    before = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, baseline)
                    after = await self.call_ok(client, "sn2_snapshot", {"targets": targets}, changed)
                    copies = copy.deepcopy((before, after))
                    difference = await client.call_tool("sn2_snapshot_compare", {"before": before, "after": after})
                    self.assertFalse(difference.is_error, difference.content)
                    result = difference.structured_content
                    self.assertEqual(result["value_changes"], 1)
                    self.assertEqual(result["unknown_count"], 1)
                    self.assertIsNone(result["equal_selected_values"])
                    self.assertEqual(result["provenance"], "unverified")
                    self.assertEqual((before, after), copies)
                    await self.call_ok(client, "sn2_watch_start", start_params, started)
                    try:
                        await self.call_ok(client, "sn2_watch_read", {"watch_id": "watch-1"}, running)
                    except BaseException:
                        self.assert_fixture_errors(native)
                        raise
                    await self.call_ok(client, "sn2_watch_stop", {"watch_id": "watch-1"}, stopped)
                    await self.call_ok(client, "sn2_watch_read", {"watch_id": "watch-1", "after_seq": 3, "limit": 10}, final)
                    await self.call_ok(client, "sn2_watch_forget", {"watch_id": "watch-1"}, forgotten)
            self.assertEqual(native.connections, 1)
            self.assertEqual(len(native.requests), 7)  # Сравнение выполняется на копиях без TCP.
            self.assertEqual(len({request["id"] for request in native.requests}), 7)

    async def test_lost_start_is_not_retried_and_old_native_connection_watch_is_unavailable(self):
        start_params = {"handle": "obj-1", "properties": ["Enabled"], "duration_ms": 1000,
                        "interval_ms": 20, "capacity": 32}
        status = {"version": "1.0.0-dev.6", "ready": True, "tick_count": 90,
                  "game_thread_id": 456, "native_route": "standalone_in_process"}
        unknown = {"code": "unknown_watch", "message": "Наблюдение отсутствует в этой сессии"}
        steps = [
            {"op": "watch_start", "params": start_params, "drop": True, "session_owner": True},
            {"op": "status", "params": {}, "result": status},
            {"op": "watch_read", "params": {"watch_id": "watch-1", "after_seq": 0, "limit": 100}, "error": unknown, "session_owner": True},
        ]
        with tempfile.TemporaryDirectory() as directory, SessionNativeFixture(steps) as native:
            async with stdio_client(self.parameters(Path(directory), native.port)) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    lost = await client.call_tool("sn2_watch_start", start_params)
                    self.assertTrue(lost.is_error)
                    text = next(block.text for block in lost.content if block.type == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "native_disconnected")
                    self.assertTrue(error["may_have_executed"])
                    self.assertEqual(error["request_id"], native.requests[0]["id"])
                    await self.call_ok(client, "sn2_native_status", {}, status)
                    stale = await client.call_tool("sn2_watch_read", {"watch_id": "watch-1"})
                    self.assert_fixture_errors(native)
                    self.assertTrue(stale.is_error)
                    self.assertIn("unknown_watch", repr(stale.content))
                    before = native_snapshot()
                    after = native_snapshot(session="fixture-session-2")
                    comparison = await client.call_tool("sn2_snapshot_compare", {"before": before, "after": after})
                    self.assertFalse(comparison.is_error, comparison.content)
                    self.assertFalse(comparison.structured_content["comparable"])
                    self.assertEqual(comparison.structured_content["reason"], "different_session")
            self.assertEqual(native.connections, 2)
            self.assertEqual([request["op"] for request in native.requests], ["watch_start", "status", "watch_read"])
            self.assertEqual(len({request["id"] for request in native.requests}), 3)


if __name__ == "__main__":
    unittest.main()
