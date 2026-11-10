"""Реальная MCP stdio-сессия на изолированных источниках и пакетах."""

import json
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

import httpx2
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.transport_security import TransportSecuritySettings

from sn2_mcp import __version__
from sn2_mcp.server import create_server
from tests.test_mcp_observations import watch_metadata


class DropCompletedStopResponse(httpx2.AsyncBaseTransport):
    """Потерять полный ASGI-ответ на стороне клиента после завершения операции."""

    def __init__(self, app, tool_name="sn2_watch_stop"):
        self.inner = httpx2.ASGITransport(app=app)
        self.tool_name = tool_name
        self.discarded = None

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        body = json.loads(request.content) if request.method == "POST" else {}
        params = body.get("params", {})
        if params.get("name") == self.tool_name and self.discarded is None:
            payload = json.loads(await response.aread())
            self.discarded = payload["result"]["structuredContent"]
            await response.aclose()
            raise httpx2.ReadError("fixture: полный HTTP-ответ потерян", request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_lost_start_is_readable_by_another_http_client_before_optional_resume(self):
        metadata = {**watch_metadata(state="expired", reason="duration_elapsed", next_seq=3),
                    "watch_id": "watch-lost", "started_ms": 55}
        events = [{"seq": 2, "kind": "terminal", "reason": "duration_elapsed"}]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_bytes(b"class UFixture {}\r\n")
            original = source.read_bytes()
            workspace.mkdir()
            with patch("sn2_mcp.server.NativeClient") as factory:
                native = factory.return_value
                server = create_server(modkit, workspace, native_port=27123, transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, transport_security=security)
                lossy = DropCompletedStopResponse(app, "sn2_watch_start")
                counter = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response

                async def initialize(client, name):
                    response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                               "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    return session

                async def tool(client, session, name, arguments=None):
                    result = (await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session)).json()["result"]
                    self.assertFalse(result.get("isError", False), result)
                    return result["structuredContent"]

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=lossy, base_url="http://127.0.0.1:27124") as first, \
                               httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as second:
                        one, two = await initialize(first, "lost-start-owner"), await initialize(second, "resume-owner")
                        self.assertNotEqual(one, two)
                        owner_a = owner_b = "shared_native_connection"
                        lost_metadata = {**metadata, "client_owner": owner_a}
                        started_metadata = {**lost_metadata, "state": "running", "reason": "", "cleanup_complete": False,
                                            "next_seq": 1, "last_seq": 0, "oldest_seq": 1}
                        resumed_metadata = {**metadata, "client_owner": owner_b}
                        start = {"handle": "obj-lost", "properties": ["Enabled"], "duration_ms": 1000,
                                 "interval_ms": 20, "capacity": 2}
                        resume = {"watch_id": "watch-lost", "expected_session_id": metadata["session_id"],
                                  "expected_client_owner": "legacy-client-metadata"}
                        read = {"watch_id": "watch-lost", "after_seq": 0, "limit": 100}
                        terminal = {**resumed_metadata, "events": events, "cursor": 2, "gap": False, "truncated": False}
                        expected = [
                            ("watch_start", {**start, "client_owner": owner_a}, started_metadata),
                            ("watch_list", {"client_owner": owner_b}, {"session_id": metadata["session_id"],
                                "current_client_owner": owner_b, "items": [lost_metadata]}),
                            ("watch_read", {**read, "client_owner": owner_b}, terminal),
                            ("watch_resume", {"watch_id": "watch-lost", "client_owner": owner_b}, resumed_metadata),
                            ("watch_resume", {"watch_id": "watch-lost", "client_owner": owner_b}, resumed_metadata),
                            ("watch_resume", {**resume, "client_owner": owner_b}, resumed_metadata),
                            ("watch_list", {"client_owner": owner_b}, {"session_id": metadata["session_id"],
                                "current_client_owner": owner_b, "items": [resumed_metadata]}),
                            ("watch_read", {**read, "client_owner": owner_b}, terminal),
                            ("watch_read", {**read, "client_owner": owner_a}, terminal),
                            ("watch_forget", {"watch_id": "watch-lost", "client_owner": owner_b},
                                {"watch_id": "watch-lost", "forgotten": True, "cleanup_complete": True}),
                        ]
                        observed = []

                        def boundary(operation, parameters):
                            planned_operation, planned_parameters, result = expected[len(observed)]
                            self.assertEqual((operation, parameters), (planned_operation, planned_parameters))
                            observed.append((operation, parameters))
                            return result

                        native.request.side_effect = boundary
                        listed = (await rpc(second, "tools/list", {}, two)).json()["result"]["tools"]
                        self.assertEqual(len(listed), 64)
                        schemas = {item["name"]: item["inputSchema"] for item in listed}
                        self.assertEqual(set(schemas["sn2_watch_list"].get("properties", {})), {"timeout_seconds"})
                        self.assertEqual(set(schemas["sn2_watch_resume"]["properties"]), set(resume) | {"timeout_seconds"})
                        self.assertEqual(schemas["sn2_watch_resume"].get("required"), ["watch_id"])
                        for legacy in ("expected_session_id", "expected_client_owner"):
                            self.assertIsNone(schemas["sn2_watch_resume"]["properties"][legacy]["default"])
                        for name in ("sn2_watch_list", "sn2_watch_resume"):
                            for hidden in ("ctx", "client_owner", "new_owner"):
                                self.assertNotIn(hidden, schemas[name].get("properties", {}))
                        with self.assertRaises(httpx2.ReadError):
                            await tool(first, one, "sn2_watch_start", start)
                        self.assertEqual(lossy.discarded, started_metadata)
                        discovered = await tool(second, two, "sn2_watch_list")
                        self.assertEqual(discovered["items"], [lost_metadata])
                        self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-lost"}), terminal)
                        self.assertEqual(await tool(second, two, "sn2_watch_resume", {"watch_id": "watch-lost"}), resumed_metadata)
                        self.assertEqual(await tool(second, two, "sn2_watch_resume", {
                            "watch_id": "watch-lost", "expected_session_id": None, "expected_client_owner": None}), resumed_metadata)
                        transferred = await tool(second, two, "sn2_watch_resume", resume)
                        self.assertEqual(transferred, resumed_metadata)
                        self.assertEqual({key: value for key, value in transferred.items() if key != "client_owner"}, metadata)
                        self.assertEqual((await tool(second, two, "sn2_watch_list"))["items"], [resumed_metadata])
                        self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-lost"}), terminal)
                        self.assertEqual(await tool(first, one, "sn2_watch_read", {"watch_id": "watch-lost"}), terminal)
                        self.assertTrue((await tool(second, two, "sn2_watch_forget", {"watch_id": "watch-lost"}))["forgotten"])
                        self.assertEqual(len(observed), len(expected))
                        self.assertEqual(sum(operation == "watch_start" for operation, _ in observed), 1)
                        self.assertEqual(sum(operation == "watch_resume" for operation, _ in observed), 3)
                        self.assertFalse(any(operation == "watch_stop" for operation, _ in observed))
                        for client, session in ((first, one), (second, two)):
                            deleted = await client.delete("/mcp", headers={"Mcp-Session-Id": session,
                                "Accept": "application/json, text/event-stream"})
                            self.assertEqual(deleted.status_code, 200, deleted.text)
                        factory.assert_called_once_with(port=27123)
                        native.close.assert_not_called()
                native.close.assert_called_once_with()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_lost_http_stop_response_remains_readable_and_removable_by_another_client(self):
        started_a = {**watch_metadata(), "watch_id": "watch-a"}
        started_b = {**watch_metadata(), "watch_id": "watch-b"}
        stopped_a = {**watch_metadata(state="stopped", reason="explicit_stop", next_seq=3), "watch_id": "watch-a"}
        terminal_a = {**stopped_a, "events": [{"seq": 2, "kind": "terminal", "reason": "explicit_stop"}],
                      "cursor": 2, "gap": False, "truncated": False}
        running_b = {**started_b, "events": [], "cursor": 0, "gap": False, "truncated": False}
        forgotten_a = {"watch_id": "watch-a", "forgotten": True, "cleanup_complete": True}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_bytes(b"class UFixture {}\r\n")
            original = source.read_bytes()
            workspace.mkdir()
            with patch("sn2_mcp.server.NativeClient") as factory:
                native = factory.return_value
                server = create_server(modkit, workspace, native_port=27123, transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, transport_security=security)
                lossy = DropCompletedStopResponse(app)
                counter = 0
                initializations = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()["result"]

                async def initialize(client, name):
                    nonlocal counter, initializations
                    counter += 1
                    response = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": counter, "method": "initialize", "params": {
                            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": name, "version": "1"}}})
                    self.assertEqual(response.status_code, 200, response.text)
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    initializations += 1
                    return session

                async def tool(client, session, name, arguments):
                    result = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                    self.assertFalse(result.get("isError", False), result)
                    return result["structuredContent"]

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=lossy, base_url="http://127.0.0.1:27124") as first, \
                               httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as second:
                        one, two = await initialize(first, "watch-loss-a"), await initialize(second, "watch-loss-b")
                        self.assertNotEqual(one, two)
                        owner_a = owner_b = "shared_native_connection"
                        start = {"properties": ["Enabled"], "duration_ms": 1000, "interval_ms": 20, "capacity": 2}
                        query_a = {"watch_id": "watch-a", "after_seq": 0, "limit": 100}
                        query_b = {"watch_id": "watch-b", "after_seq": 0, "limit": 100}
                        expected = [
                            ("watch_start", {**start, "handle": "obj-a", "client_owner": owner_a}, started_a),
                            ("watch_start", {**start, "handle": "obj-b", "client_owner": owner_b}, started_b),
                            ("watch_stop", {"watch_id": "watch-a", "client_owner": owner_a}, stopped_a),
                            ("watch_read", {**query_a, "client_owner": owner_a}, terminal_a),
                            ("watch_read", {**query_b, "client_owner": owner_b}, running_b),
                            ("watch_read", {**query_a, "client_owner": owner_b}, terminal_a),
                            ("watch_stop", {"watch_id": "watch-a", "client_owner": owner_b}, stopped_a),
                            ("watch_forget", {"watch_id": "watch-a", "client_owner": owner_b}, forgotten_a),
                            ("watch_read", {**query_b, "client_owner": owner_b}, running_b),
                        ]
                        observed = []

                        def boundary(operation, parameters):
                            planned_operation, planned_parameters, result = expected[len(observed)]
                            self.assertEqual((operation, parameters), (planned_operation, planned_parameters))
                            observed.append((operation, parameters))
                            return result

                        native.request.side_effect = boundary
                        self.assertEqual(await tool(first, one, "sn2_watch_start", {**start, "handle": "obj-a"}), started_a)
                        self.assertEqual(await tool(second, two, "sn2_watch_start", {**start, "handle": "obj-b"}), started_b)
                        with self.assertRaises(httpx2.ReadError):
                            await tool(first, one, "sn2_watch_stop", {"watch_id": "watch-a"})
                        self.assertEqual(lossy.discarded, stopped_a)
                        self.assertTrue(lossy.discarded["cleanup_complete"])
                        self.assertEqual(len(observed), 3)
                        native.close.assert_not_called()
                        async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                     base_url="http://127.0.0.1:27124") as resumed:
                            self.assertEqual(await tool(resumed, one, "sn2_watch_read", {"watch_id": "watch-a"}), terminal_a)
                            self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-b"}), running_b)
                            self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-a"}), terminal_a)
                            self.assertEqual(await tool(second, two, "sn2_watch_stop", {"watch_id": "watch-a"}), stopped_a)
                            self.assertEqual(await tool(second, two, "sn2_watch_forget", {"watch_id": "watch-a"}), forgotten_a)
                            self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-b"}), running_b)
                            self.assertEqual(initializations, 2)
                            for client, session in ((resumed, one), (second, two)):
                                deleted = await client.delete("/mcp", headers={"Mcp-Session-Id": session,
                                    "Accept": "application/json, text/event-stream"})
                                self.assertEqual(deleted.status_code, 200, deleted.text)
                        self.assertEqual(len(observed), len(expected))
                        self.assertEqual(sum(operation == "watch_stop" for operation, _ in observed), 2)
                        self.assertFalse(any(operation == "watch_resume" for operation, _ in observed))
                        factory.assert_called_once_with(port=27123)
                        native.close.assert_not_called()
                native.close.assert_called_once_with()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_http_watch_tools_use_shared_client_metadata_in_one_native_connection(self):
        calls = (("sn2_watch_start", "watch_start", {"handle": "obj-1", "properties": ["Enabled"],
                  "duration_ms": 1000, "interval_ms": 20, "capacity": 2}),
                 ("sn2_watch_read", "watch_read", {"watch_id": "watch-1", "after_seq": 3, "limit": 10}),
                 ("sn2_watch_stop", "watch_stop", {"watch_id": "watch-1"}),
                 ("sn2_watch_forget", "watch_forget", {"watch_id": "watch-1"}))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_bytes(b"class UFixture {}\r\n")
            original = source.read_bytes()
            workspace.mkdir()
            with patch("sn2_mcp.server.NativeClient") as factory:
                native = factory.return_value
                native.request.side_effect = lambda operation, parameters: {"operation": operation}
                server = create_server(modkit, workspace, native_port=27123, transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, transport_security=security)
                counter = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response

                async def initialize(client, name):
                    response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                               "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    return session

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as first, \
                               httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                                 base_url="http://127.0.0.1:27124") as second:
                        one, two = await initialize(first, "watch-owner-one"), await initialize(second, "watch-owner-two")
                        self.assertNotEqual(one, two)
                        listed = (await rpc(first, "tools/list", {}, one)).json()["result"]["tools"]
                        schemas = {item["name"]: item["inputSchema"] for item in listed}
                        for name, _, arguments in calls:
                            self.assertEqual(set(schemas[name]["properties"]), set(arguments) | {"timeout_seconds"})
                            self.assertNotIn("ctx", schemas[name]["properties"])
                            self.assertNotIn("client_owner", schemas[name]["properties"])
                        for client, session in ((first, one), (second, two)):
                            for name, operation, arguments in calls:
                                response = await rpc(client, "tools/call", {"name": name, "arguments": arguments}, session)
                                result = response.json()["result"]
                                self.assertFalse(result.get("isError", False), result)
                                self.assertEqual(result["structuredContent"], {"operation": operation})
                                native.request.assert_called_with(operation, {**arguments, "client_owner": "shared_native_connection"})
                        self.assertEqual(native.request.call_count, 8)
                        first_owner = {item.args[1]["client_owner"] for item in native.request.call_args_list[:4]}
                        second_owner = {item.args[1]["client_owner"] for item in native.request.call_args_list[4:]}
                        self.assertEqual(first_owner, {"shared_native_connection"})
                        self.assertEqual(second_owner, first_owner)
                        for client, session in ((first, one), (second, two)):
                            deleted = await client.delete("/mcp", headers={"Mcp-Session-Id": session,
                                                        "Accept": "application/json, text/event-stream"})
                            self.assertEqual(deleted.status_code, 200, deleted.text)
                        factory.assert_called_once_with(port=27123)
                        native.close.assert_not_called()
                native.close.assert_called_once_with()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_stdio_tools_sources_validation_and_errors(self):
        project = Path(__file__).resolve().parents[1] / "pyproject.toml"
        self.assertEqual(tomllib.loads(project.read_text(encoding="utf-8"))["project"]["version"], __version__)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit = root / "Modkit"
            script = modkit / "Script" / "Example.as"
            script.parent.mkdir(parents=True)
            script.write_text("class UExample : UObject\n{\n    FString Caption;\n}\n", encoding="utf-8")
            package = root / "workspace" / "Example"
            package.mkdir(parents=True)
            descriptor = package / "GothicMod.json"
            descriptor.write_text(json.dumps({
                "name": "Example", "title": "Пример", "author": "Автор", "modVersion": "1.0.0-dev.1",
            }, ensure_ascii=False), encoding="utf-8")
            before = (script.read_bytes(), descriptor.read_bytes())
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-B", "-m", "sn2_mcp.server", "--modkit-root", str(modkit),
                      "--workspace-root", str(root / "workspace"), "--source-revision", "a" * 40],
            )
            async with stdio_client(parameters) as (incoming, outgoing):
                async with ClientSession(incoming, outgoing) as client:
                    initialized = await client.initialize()
                    self.assertEqual(initialized.server_info.name, "G1R MCP")
                    self.assertEqual(initialized.server_info.version, __version__)
                    tools = (await client.list_tools()).tools
                    names = {tool.name for tool in tools}
                    self.assertEqual(len(names), 64)
                    self.assertTrue({"sn2_movieplayer_status", "sn2_movieplayer_finish"}.issubset(names))
                    self.assertIn("sn2_development_key", names)
                    source_tools = {"sn2_source_identity", "sn2_verify_source_game", "sn2_list_source_functions"}
                    self.assertTrue(source_tools.issubset(names))
                    type_schema = next(tool for tool in tools if tool.name == "sn2_list_source_types").input_schema
                    relative_schema = type_schema["properties"]["relative_path"]
                    self.assertEqual({item["type"] for item in relative_schema["anyOf"]}, {"string", "null"})
                    self.assertIn("default", relative_schema)
                    self.assertIsNone(relative_schema["default"])
                    mutations = {"sn2_invoke", "sn2_native_enable", "sn2_create_package",
                                 "sn2_write_package_file", "sn2_export_package", "sn2_watch_start", "sn2_watch_stop", "sn2_watch_forget",
                                 "sn2_watch_resume", "sn2_movieplayer_finish", "sn2_apply_staged_file",
                                 "sn2_development_start", "sn2_development_stop", "sn2_development_key", "sn2_native_load",
                                 "sn2_create_package_directory"}
                    mutations.update({"sn2_session_acquire", "sn2_session_release"})
                    mutations.update({"sn2_save_list", "sn2_save_load", "sn2_modlist_dialog",
                                      "sn2_debug_attach", "sn2_debug_detach", "sn2_debug_breakpoints",
                                      "sn2_debug_control", "sn2_debug_evaluate"})
                    self.assertTrue(all(tool.annotations.read_only_hint for tool in tools if tool.name not in mutations))
                    self.assertFalse(next(tool for tool in tools if tool.name == "sn2_invoke").annotations.read_only_hint)
                    key_tool = next(tool for tool in tools if tool.name == "sn2_development_key")
                    self.assertFalse(key_tool.annotations.read_only_hint)
                    self.assertFalse(key_tool.annotations.idempotent_hint)
                    for tool in tools:
                        if tool.name in {"sn2_watch_start", "sn2_watch_read", "sn2_watch_stop", "sn2_watch_forget",
                                         "sn2_watch_list", "sn2_watch_resume"}:
                            self.assertNotIn("ctx", tool.input_schema["properties"])
                            self.assertNotIn("client_owner", tool.input_schema["properties"])

                    status = await client.call_tool("sn2_status", {})
                    self.assertFalse(status.is_error)
                    self.assertTrue(status.structured_content["server"]["ready"])
                    self.assertFalse(status.structured_content["game"]["ready"])
                    self.assertEqual(status.structured_content["sources"]["revision_source"], "caller_label")

                    capabilities = await client.call_tool("sn2_capabilities", {})
                    self.assertFalse(capabilities.is_error)
                    self.assertTrue(source_tools.issubset({tool["name"] for tool in
                                                         capabilities.structured_content["local_tools"]}))
                    helpers = capabilities.structured_content["development_tools"]["helpers"]
                    self.assertIn("ReadText", helpers["functions"])
                    self.assertIn("sn2_development_key", capabilities.structured_content["development_tools"]["operations"])
                    self.assertTrue(all(not group["available"] for group in capabilities.structured_content["game_groups"]))
                    found = await client.call_tool("sn2_find_source_files", {"query": "Example"})
                    self.assertFalse(found.is_error)
                    self.assertEqual(len(found.structured_content["items"]), 1)
                    read = await client.call_tool("sn2_read_source", {"relative_path": "Script/Example.as", "line_count": 2})
                    self.assertFalse(read.is_error)
                    search = await client.call_tool("sn2_search_source", {"query": "Caption"})
                    self.assertFalse(search.is_error)
                    types = await client.call_tool("sn2_list_source_types", {"query": "UExample"})
                    self.assertFalse(types.is_error)
                    self.assertEqual(len(types.structured_content["items"]), 1)
                    scoped_types = await client.call_tool("sn2_list_source_types", {
                        "query": "UExample", "limit": 50, "offset": 0,
                        "relative_path": "Script/Example.as",
                    })
                    self.assertFalse(scoped_types.is_error)
                    self.assertEqual(scoped_types.structured_content["items"], types.structured_content["items"])
                    self.assertEqual(scoped_types.structured_content["scanned_files"], 1)
                    validation = await client.call_tool("sn2_validate_package", {"relative_path": "Example"})
                    self.assertFalse(validation.is_error)
                    self.assertTrue(validation.structured_content["valid_structure"])
                    self.assertEqual(validation.structured_content["runtime_validation"], "not_performed")

                    denied = await client.call_tool("sn2_read_source", {"relative_path": "../private.as"})
                    self.assertTrue(denied.is_error)
                    self.assertNotIn(str(root), repr(denied))
                    bad_limit = await client.call_tool("sn2_find_source_files", {"query": "x", "limit": 0})
                    self.assertTrue(bad_limit.is_error)
            self.assertEqual(before, (script.read_bytes(), descriptor.read_bytes()))


if __name__ == "__main__":
    unittest.main()
