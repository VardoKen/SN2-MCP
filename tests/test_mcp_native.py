"""Сквозная MCP → TCP проверка нативных инструментов без игровой среды."""

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


class FakeNativeServer:
    """Один изолированный loopback-клиент, запись запросов и заданные ответы."""

    def __init__(self, replies):
        self.replies = replies
        self.requests = []
        self.errors = []
        self.connections = 0
        self.stop = threading.Event()
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(1)
        self.listener.settimeout(0.1)
        self.stream = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def receive(self, count):
        data = bytearray()
        while len(data) < count and not self.stop.is_set():
            try:
                fragment = self.stream.recv(count - len(data))
            except socket.timeout:
                continue
            if not fragment:
                raise EOFError
            data.extend(fragment)
        if len(data) != count:
            raise EOFError
        return bytes(data)

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.stream, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.stream is None:
                return
            self.connections += 1
            self.stream.settimeout(0.1)
            while not self.stop.is_set():
                length = struct.unpack(">I", self.receive(4))[0]
                if not 0 < length <= 1024 * 1024:
                    raise AssertionError("MCP-клиент передал неверный размер кадра")
                request = json.loads(self.receive(length).decode("utf-8"))
                index = len(self.requests)
                self.requests.append(request)
                if index >= len(self.replies):
                    raise AssertionError("Нативный запрос был неожиданно повторён")
                operation, parameters, result, error = self.replies[index]
                if request.get("protocol") != 1:
                    raise AssertionError("Неверная версия нативного протокола")
                if request.get("op") != operation or request.get("params") != parameters:
                    raise AssertionError(f"Неверный запрос {index}: {request}")
                if not isinstance(request.get("id"), str) or not request["id"]:
                    raise AssertionError("Запрос без корреляционного идентификатора")
                response = {"protocol": 1, "id": request["id"], "session_id": "isolated-session"}
                if error is None:
                    response.update(type="response", result=result)
                else:
                    response.update(type="error", error=error)
                payload = json.dumps(response, ensure_ascii=False).encode("utf-8")
                frame = struct.pack(">I", len(payload)) + payload
                # Проверить, что MCP → NativeClient не рассчитывает на цельный recv.
                self.stream.sendall(frame[:3])
                self.stream.sendall(frame[3:11])
                self.stream.sendall(frame[11:])
        except (EOFError, OSError) as error:
            if isinstance(error, OSError) and not self.stop.is_set():
                self.errors.append(error)
        except Exception as error:
            self.errors.append(error)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exception_type, exception, traceback):
        self.stop.set()
        self.listener.close()
        if self.stream is not None:
            try:
                self.stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.stream.close()
        self.thread.join(2)
        if exception_type is None:
            if self.thread.is_alive():
                raise AssertionError("Изолированный TCP-сервер не завершился")
            if self.errors:
                raise self.errors[0]
            if len(self.requests) != len(self.replies):
                raise AssertionError("Не все ожидаемые нативные операции выполнены")


class MCPNativeTests(unittest.IsolatedAsyncioTestCase):
    async def test_angelscript_exception_preserves_diagnostics_and_next_request(self):
        details = {"executed": True, "may_have_executed": True, "do_not_retry": True,
                   "process_event_returned": True, "script_completion_confirmed": False,
                   "script_errors": {"source": "observed_logging_entry", "exception_count": 2,
                       "full_capture": True, "stack_captured": False,
                       "messages": ["Array index out of bounds.", None],
                       "message_bytes": [{"index": 1, "encoding": "hex", "bytes": "726177ff"}]}}
        status = {"ready": True, "tick_count": 16, "version": "1.0.0-dev.39"}
        steps = [
            ("invoke", {"handle": "obj-1", "function": "RaiseArrayBounds", "arguments": {}}, None,
             {"code": "angelscript_exception", "message": "Ошибка выполнения AS", "details": details}),
            ("status", {}, status, None),
        ]
        with tempfile.TemporaryDirectory() as temporary, FakeNativeServer(steps) as native:
            root = Path(temporary)
            modkit = root / "Modkit"
            (modkit / "Script").mkdir(parents=True)
            workspace = root / "workspace"
            workspace.mkdir()
            parameters = StdioServerParameters(command=sys.executable, args=[
                "-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)])
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    failure = await client.call_tool("sn2_invoke", {"handle": "obj-1", "function": "RaiseArrayBounds"})
                    self.assertTrue(failure.is_error)
                    message = next(item.text for item in failure.content if item.type == "text")
                    error = json.loads(message[message.index("{"):])
                    self.assertEqual(error["code"], "angelscript_exception")
                    self.assertEqual(error["effect"], details)
                    following = await client.call_tool("sn2_native_status", {})
                    self.assertEqual(following.structured_content, status)
            self.assertEqual(native.connections, 1)
            self.assertEqual(len(native.requests), 2)

    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_native_tools_stdio_parameters_and_unknown_effect_without_replay(self):
        status = {"ready": True, "game_thread_id": 123, "tick_count": 42, "version": "1.0.0-dev.3"}
        found = {"items": [{"handle": "obj-1", "name": "ТестовыйОбъект"}], "truncated": False}
        properties = {"items": [{"name": "Enabled", "type": "BoolProperty", "value": True}], "truncated": False}
        functions = {"items": [{"name": "ReadOnlyProbe", "parameters": [
            {"name": "Target", "type": "ObjectProperty",
             "declared_type": {"name": "Actor", "path": "/Script/Engine.Actor"}},
            {"name": "Kind", "type": "ClassProperty",
             "declared_type": {"name": "Class", "path": "/Script/CoreUObject.Class"},
             "meta_class": {"name": "Actor", "path": "/Script/Engine.Actor"}},
            {"name": "Mode", "type": "EnumProperty", "enum": {
                "path": "/Script/Probe.EMode", "underlying_type": "ByteProperty",
                "entries": [{"name": "EMode::Default", "value": 0},
                            {"name": "EMode::Alias", "value": 0}]}}
        ]}], "truncated": False}
        invocation = {"executed": True, "result": False, "thread_id": 123}
        timeout = {"code": "game_timeout", "message": "Исход уже принятого вызова неизвестен"}
        replies = [
            ("status", {}, status, None),
            ("objects", {"query": "Тест", "class_query": "Probe", "limit": 2}, found, None),
            ("properties", {"handle": "obj-1", "limit": 3}, properties, None),
            ("functions", {"handle": "obj-1", "limit": 4}, functions, None),
            ("invoke", {"handle": "obj-1", "function": "ReadOnlyProbe", "arguments": {}}, invocation, None),
            ("invoke", {"handle": "obj-1", "function": "TimedAction", "arguments": {"Amount": 2}}, None, timeout),
            ("status", {}, status, None),
        ]
        with tempfile.TemporaryDirectory() as directory, FakeNativeServer(replies) as native:
            root = Path(directory)
            modkit = root / "Modkit"
            script = modkit / "Script" / "Probe.as"
            script.parent.mkdir(parents=True)
            script.write_text("class UProbe {}\n", encoding="utf-8")
            workspace = root / "workspace"
            workspace.mkdir()
            source_before = script.read_bytes()
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "sn2_mcp.server", "--workspace-root", str(workspace), "--native-port", str(native.port)],
            )
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    await client.initialize()
                    tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                    self.assertEqual(len(tools), 33)
                    self.assertFalse(tools["sn2_invoke"].annotations.read_only_hint)
                    self.assertFalse(tools["sn2_native_enable"].annotations.read_only_hint)
                    self.assertFalse(tools["sn2_invoke"].annotations.idempotent_hint)
                    for tool_name in ("sn2_native_status", "sn2_find_objects", "sn2_object_properties", "sn2_object_functions"):
                        self.assertTrue(tools[tool_name].annotations.read_only_hint)
                    cases = [
                        ("sn2_native_status", {}, status),
                        ("sn2_find_objects", {"query": "Тест", "class_query": "Probe", "limit": 2,
                                              "timeout_seconds": 6}, found),
                        ("sn2_object_properties", {"handle": "obj-1", "limit": 3}, properties),
                        ("sn2_object_functions", {"handle": "obj-1", "limit": 4}, functions),
                        ("sn2_invoke", {"handle": "obj-1", "function": "ReadOnlyProbe"}, invocation),
                    ]
                    for tool_name, arguments, expected in cases:
                        with self.subTest(tool=tool_name):
                            result = await client.call_tool(tool_name, arguments)
                            self.assertFalse(result.is_error)
                            self.assertEqual(result.structured_content, expected)

                    failure = await client.call_tool("sn2_invoke", {
                        "handle": "obj-1", "function": "TimedAction", "arguments": {"Amount": 2},
                    })
                    self.assertTrue(failure.is_error)
                    text = next(item.text for item in failure.content if item.type == "text")
                    error = json.loads(text[text.index("{"):])
                    self.assertEqual(error["code"], "game_timeout")
                    self.assertTrue(error["may_have_executed"])
                    self.assertEqual(error["request_id"], native.requests[5]["id"])
                    self.assertEqual(error["message"], timeout["message"])
                    following = await client.call_tool("sn2_native_status", {})
                    self.assertFalse(following.is_error)
                    self.assertEqual(following.structured_content, status)
            self.assertEqual(script.read_bytes(), source_before)
            self.assertEqual(list(workspace.iterdir()), [])
            self.assertEqual(native.connections, 1)
            self.assertEqual(len(native.requests), 7)
            self.assertGreater(native.requests[1]["wait_timeout_ms"], 0)
            self.assertLessEqual(native.requests[1]["wait_timeout_ms"], 6_000)
            self.assertEqual(len({request["id"] for request in native.requests}), 7)


if __name__ == "__main__":
    unittest.main()
