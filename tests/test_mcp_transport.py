"""One HTTP server shares the workspace and native client across MCP sessions."""
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import socket
import sys
import struct
import tempfile
import unittest
from unittest.mock import patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from g1r_mcp.native_client import NativeClient
from g1r_mcp.server import create_server, main
from tests.test_mcp_observations import SessionNativeFixture


class LargeFrameNativeFixture(SessionNativeFixture):
    """Isolated uint32 framing without the obsolete artificial 1 MiB limit."""

    def __init__(self, steps):
        super().__init__(steps)
        self.request_frame_lengths = []
        self.response_frame_lengths = []
        self.response_sessions = []
        self.echo_calls = 0

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
                    if length == 0:
                        raise AssertionError("Empty native frame")
                    request = json.loads(self.receive(length).decode("utf-8"))
                except EOFError:
                    self.stream.close()
                    self.stream = None
                    continue
                step = self.steps[len(self.requests)]
                self.requests.append(request)
                self.request_frame_lengths.append(length)
                if (request.get("protocol") != 1 or request.get("op") != step["op"]
                        or request.get("params") != step["params"]):
                    raise AssertionError("The request does not match the expected operation")
                if not isinstance(request.get("id"), str) or not request["id"]:
                    raise AssertionError("Missing native request ID")
                result = dict(step["result"])
                if request.get("params", {}).get("function") == "EchoLargeString":
                    self.echo_calls += 1
                    result["out_parameters"] = {"CallCount": self.echo_calls}
                elif request.get("params", {}).get("function") == "ReadCallCount":
                    result["result"] = self.echo_calls
                session = f"fixture-session-{self.connections}"
                response = {"protocol": 1, "id": request["id"], "session_id": session,
                            "type": "response", "result": result}
                payload = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                self.response_frame_lengths.append(len(payload))
                self.response_sessions.append(session)
                header = struct.pack(">I", len(payload))
                self.stream.sendall(header[:2])
                self.stream.sendall(header[2:])
                for offset in range(0, len(payload), 65537):
                    self.stream.sendall(payload[offset:offset + 65537])
        except (OSError, EOFError) as error:
            if not self.stop.is_set():
                self.errors.append(error)
        except Exception as error:
            self.errors.append(error)


class MCPTransportCLITests(unittest.TestCase):
    def args(self, *extra):
        return ["--modkit-root", "fixture-modkit", "--workspace-root", "fixture-workspace", *extra]

    def test_stdio_is_default_and_does_not_receive_http_options(self):
        with patch("g1r_mcp.server.create_server") as factory:
            self.assertEqual(main(self.args()), 0)
            factory.return_value.run.assert_called_once_with(transport="stdio")

    def test_streamable_http_passes_exact_installed_sdk_options(self):
        for port in (27124, 29124):
            with self.subTest(port=port), patch("g1r_mcp.server.create_server") as factory:
                extra = ["--transport", "streamable-http"]
                if port != 27124:
                    extra += ["--port", str(port)]
                self.assertEqual(main(self.args(*extra)), 0)
                factory.assert_called_once()
                run = factory.return_value.run
                run.assert_called_once()
                options = run.call_args.kwargs
                self.assertEqual({key: value for key, value in options.items() if key != "transport_security"}, {
                    "transport": "streamable-http", "host": "127.0.0.1", "port": port,
                    "streamable_http_path": "/mcp", "stateless_http": False, "json_response": True,
                    "max_request_body_size": sys.maxsize})
                security = options["transport_security"]
                self.assertIsInstance(security, TransportSecuritySettings)
                self.assertIs(security.enable_dns_rebinding_protection, True)
                self.assertEqual(security.allowed_hosts, [f"127.0.0.1:{port}"])
                self.assertEqual(security.allowed_origins, [f"http://127.0.0.1:{port}"])

    def test_non_loopback_hosts_ports_and_unknown_transports_refuse_before_server(self):
        for extra in (
                ("--host", "0.0.0.0"), ("--host", "192.168.1.1"), ("--host", "localhost"),
                ("--host", "::1"), ("--port", "0"), ("--port", "1023"), ("--port", "65536"),
                ("--port", "text"), ("--transport", "sse")):
            with self.subTest(extra=extra), patch("g1r_mcp.server.create_server") as factory, redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    main(self.args("--transport", "streamable-http", *extra))
                self.assertEqual(caught.exception.code, 2)
                factory.assert_not_called()


class MCPHTTPSharedServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_large_snapshot_comparison_over_http_preserves_same_session_without_native_requests(self):
        large_text = "x" * 300_000
        objects = [{"handle": f"obj-large-{index}", "path": f"/Script/Probe.Large{index}", "class": "Probe",
                    "properties": [{"name": "LargeText", "type": "StrProperty", "supported": True, "value": large_text}] +
                                  [{"name": f"Number{number}", "type": "IntProperty", "supported": True, "value": number}
                                   for number in range(32)]}
                   for index in range(17)]
        before = {"snapshot_id": "large-before", "source": "live_native", "session_id": "fixture-native-session",
                  "process_id": 123, "thread_id": 456, "tick_count": 10, "consistency": "single_tick_sequential",
                  "truncated": False, "partial": False, "objects": objects}
        after = {**before, "snapshot_id": "large-after", "tick_count": 11}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            source = modkit / "Script" / "Fixture.as"
            source.parent.mkdir(parents=True)
            source.write_bytes(b"class UFixture {}\n")
            original = source.read_bytes()
            workspace.mkdir()
            with patch("g1r_mcp.server.NativeClient") as factory:
                backend = factory.return_value
                server = create_server(modkit, workspace, transport="streamable-http")
            app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                stateless_http=False, json_response=True, max_request_body_size=sys.maxsize,
                transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"]))
            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                             base_url="http://127.0.0.1:27124") as client:
                    initialized = await client.post("/mcp", headers={"Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                            "protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "large-snapshot-client", "version": "1"}}})
                    self.assertEqual(initialized.status_code, 200, initialized.text[:500])
                    session = initialized.headers["mcp-session-id"]
                    headers = {"Mcp-Session-Id": session, "Accept": "application/json, text/event-stream"}
                    notified = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text[:500])
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                            "name": "g1r_compare_snapshots", "arguments": {"before": before, "after": after}}})
                    self.assertGreater(len(response.request.content), 4 * 1024 * 1024)
                    self.assertEqual(response.status_code, 200, response.text[:500])
                    result = response.json()["result"]
                    self.assertFalse(result.get("isError", False), str(result)[:500])
                    comparison = result["structuredContent"]
                    self.assertTrue(comparison["comparable"])
                    self.assertTrue(comparison["equal_selected_values"])
                    self.assertEqual(comparison["items"], [])
                    self.assertEqual(comparison["total_items"], 0)
                    self.assertEqual(comparison["coverage"]["before_targets"], 17)
                    self.assertEqual(comparison["coverage"]["after_targets"], 17)
                    self.assertTrue(comparison["coverage"]["complete"])
                    self.assertFalse(comparison["truncated"])
                    status = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                            "name": "g1r_status", "arguments": {}}})
                    self.assertEqual(status.status_code, 200, status.text[:500])
                    self.assertFalse(status.json()["result"].get("isError", False))
                    self.assertEqual(status.request.headers["Mcp-Session-Id"], session)
                    self.assertEqual(status.json()["result"]["structuredContent"]["server"]["transport"], "streamable-http")
                    backend.request.assert_not_called()
                    terminated = await client.delete("/mcp", headers=headers)
                    self.assertEqual(terminated.status_code, 200, terminated.text[:500])
            backend.request.assert_not_called()
            factory.assert_called_once()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_large_string_request_and_result_share_connection_after_http_session_delete(self):
        handle, token = "obj-large-http-string", "sig-large-http-string"
        text = "G1R-large-string-" * 65537
        self.assertGreater(len(text.encode("ascii")), 1048576)
        arguments = {"Input": text, "CallCount": {"out": True}}
        status = {"ready": True, "session_id": "fixture-session-1", "tick_count": 41}
        reflection = {"items": [{"name": "EchoLargeString", "signature_token": token,
            "signature_status": "complete", "parameters": [
                {"name": "Input", "type": "StrProperty", "offset": 0, "element_size": 16,
                 "array_dim": 1, "flags": 0x182, "return": False, "out": True,
                 "reference": False, "const": True, "direction": "input", "passing": "const_reference",
                 "argument_constraints": {"max_utf16_units": 2147483646, "embedded_nul": "rejected",
                                          "ownership": "engine_owned_copy"}},
                {"name": "CallCount", "type": "IntProperty", "element_size": 4, "array_dim": 1,
                 "flags": 0x180, "return": False, "out": True, "reference": False,
                 "const": False, "direction": "output", "argument_marker": {"out": True}},
                {"name": "ReturnValue", "type": "StrProperty", "element_size": 16, "array_dim": 1,
                 "flags": 0x480, "return": True, "out": False, "reference": False, "const": False}]}]}
        echoed = {"function": "EchoLargeString", "return_type": "StrProperty", "result": text,
                  "out_parameters": {"CallCount": 1},
                  "out_parameter_metadata": {"CallCount": {"type": "IntProperty", "element_size": 4,
                                                            "array_dim": 1}},
                  "executed": True, "thread_id": 456, "cleanup_confirmed": True,
                  "input_cleanup_confirmed": True}
        getter = {"function": "ReadCallCount", "return_type": "IntProperty", "result": 1,
                  "executed": True, "thread_id": 456}
        steps = [
            {"op": "status", "params": {}, "result": status},
            {"op": "functions", "params": {"handle": handle, "limit": 50,
                "function_query": "EchoLargeString"}, "result": reflection},
            {"op": "invoke", "params": {"handle": handle, "function": "EchoLargeString",
                "arguments": arguments, "expected_signature_token": token}, "result": echoed},
            {"op": "invoke", "params": {"handle": handle, "function": "ReadCallCount",
                "arguments": {}}, "result": getter},
            {"op": "status", "params": {}, "result": status},
        ]
        with tempfile.TemporaryDirectory() as temporary, LargeFrameNativeFixture(steps) as native:
            root = Path(temporary)
            modkit, workspace = root / "Modkit", root / "workspace"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            original = b"class UFixture {}\r\n"
            source.write_bytes(original)
            workspace.mkdir()
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            with patch("g1r_mcp.server.NativeClient", return_value=backend) as factory:
                server = create_server(modkit, workspace, native_port=native.port, transport="streamable-http")
                factory.assert_called_once_with(port=native.port)
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, max_request_body_size=sys.maxsize,
                    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
                        allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"]))
                counter = 0

                async def rpc(client, method, params, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text[:500])
                    return response

                async def initialize(client, name):
                    response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(notified.status_code, 202, notified.text)
                    return session

                async def call(client, session, name, parameters=None):
                    response = await rpc(client, "tools/call", {"name": name,
                        "arguments": parameters or {}}, session)
                    result = response.json()["result"]
                    self.assertFalse(result.get("isError", False), str(result)[:500])
                    return result["structuredContent"], response

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                            base_url="http://127.0.0.1:27124") as first, \
                            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                            base_url="http://127.0.0.1:27124") as second:
                        first_session = await initialize(first, "large-string-owner")
                        second_session = await initialize(second, "large-string-reader")
                        self.assertNotEqual(first_session, second_session)
                        self.assertEqual((await call(first, first_session, "g1r_native_status"))[0], status)
                        listed, _ = await call(first, first_session, "g1r_object_functions",
                            {"handle": handle, "function_query": "EchoLargeString"})
                        self.assertEqual(listed, reflection)
                        actual, response = await call(first, first_session, "g1r_invoke",
                            {"handle": handle, "function": "EchoLargeString", "arguments": arguments,
                             "expected_signature_token": listed["items"][0]["signature_token"]})
                        self.assertEqual(actual, echoed)
                        self.assertEqual(actual["result"].encode("ascii"), text.encode("ascii"))
                        self.assertGreater(len(response.request.content), 1048576)
                        self.assertGreater(len(response.content), 1048576)
                        terminated = await first.delete("/mcp", headers={"Mcp-Session-Id": first_session,
                            "Accept": "application/json, text/event-stream"})
                        self.assertEqual(terminated.status_code, 200, terminated.text)
                        self.assertEqual((await call(second, second_session, "g1r_invoke",
                            {"handle": handle, "function": "ReadCallCount"}))[0], getter)
                        self.assertEqual((await call(second, second_session, "g1r_native_status"))[0], status)
                factory.assert_called_once_with(port=native.port)
            self.assertEqual(native.connections, 1)
            self.assertEqual(native.echo_calls, 1)
            self.assertGreater(native.request_frame_lengths[2], 1048576)
            self.assertGreater(native.response_frame_lengths[2], 1048576)
            self.assertEqual(native.response_sessions, ["fixture-session-1"] * len(steps))
            self.assertEqual([request["op"] for request in native.requests],
                             ["status", "functions", "invoke", "invoke", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), len(steps))
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(list(workspace.iterdir()), [])

    async def test_two_http_sessions_share_native_connection_workspace_and_security_boundary(self):
        native_status = {"ready": True, "session_id": "shared-native-owner", "tick_count": 31}
        steps = [
            {"op": "status", "params": {}, "result": native_status},
            {"op": "objects", "params": {"query": "Probe", "class_query": "", "limit": 50},
             "result": {"items": [{"handle": "obj-shared", "name": "Probe"}], "truncated": False}},
            {"op": "invoke", "params": {"handle": "obj-shared", "function": "ReadMarker", "arguments": {}},
             "result": {"return": 111}},
            {"op": "status", "params": {}, "result": native_status},
            {"op": "status", "params": {}, "result": native_status},
        ]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            modkit = root / "Modkit"
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_text("class UFixture {}\n", encoding="utf-8")
            before = source.read_bytes()
            workspace = root / "workspace"
            workspace.mkdir()
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            with patch("g1r_mcp.server.NativeClient", return_value=backend) as factory:
                server = create_server(modkit, workspace, native_port=native.port, transport="streamable-http")
                factory.assert_called_once_with(port=native.port)
            security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1:27124"], allowed_origins=["http://127.0.0.1:27124"])
            app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                stateless_http=False, json_response=True, max_request_body_size=sys.maxsize,
                transport_security=security)
            counter = 0

            async def rpc(client, method, params, session=None):
                nonlocal counter
                counter += 1
                headers = {"Accept": "application/json, text/event-stream"}
                if session:
                    headers["Mcp-Session-Id"] = session
                return await client.post("/mcp", headers=headers,
                    json={"jsonrpc": "2.0", "id": counter, "method": method, "params": params})

            async def initialize(client):
                response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                    "capabilities": {}, "clientInfo": {"name": "isolated-http-client", "version": "1"}})
                self.assertEqual(response.status_code, 200, response.text)
                session = response.headers["mcp-session-id"]
                notified = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                    "Accept": "application/json, text/event-stream"},
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                self.assertEqual(notified.status_code, 202, notified.text)
                return session

            async def tool(client, session, name, arguments=None):
                response = await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session)
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()["result"]
                self.assertFalse(result.get("isError", False), result)
                return result["structuredContent"]

            async with app.router.lifespan_context(app):
                async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as first, \
                        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                        base_url="http://127.0.0.1:27124") as second:
                    first_session = await initialize(first)
                    second_session = await initialize(second)
                    self.assertNotEqual(first_session, second_session)
                    listed = await rpc(second, "tools/list", {}, second_session)
                    self.assertEqual(listed.status_code, 200, listed.text)
                    self.assertEqual(len(listed.json()["result"]["tools"]), 64)
                    self.assertEqual((await tool(second, second_session, "g1r_status"))["server"]["transport"], "streamable-http")
                    self.assertEqual(await tool(first, first_session, "g1r_native_status"), native_status)
                    found = await tool(first, first_session, "g1r_find_objects", {"query": "Probe"})
                    handle = found["items"][0]["handle"]
                    self.assertEqual(await tool(second, second_session, "g1r_invoke", {
                        "handle": handle, "function": "ReadMarker"}), {"return": 111})
                    information = await tool(second, second_session, "g1r_session_status")
                    self.assertFalse(information["claimed"])
                    self.assertEqual(information["scope"], "informational")
                    self.assertFalse(information["control_required"])
                    self.assertFalse(information["ownership_enforced"])
                    metadata = {"name": "Shared", "title": "Shared package", "author": "Fixture", "modVersion": "1.0.0"}
                    await tool(first, first_session, "g1r_create_package", {"relative_path": "Shared", "metadata": metadata})
                    package = await tool(second, second_session, "g1r_read_package_file", {
                        "relative_path": "Shared", "file_path": "GothicMod.json"})
                    self.assertEqual(json.loads(package["text"]), metadata)
                    body = {"jsonrpc": "2.0", "id": 500, "method": "tools/call",
                            "params": {"name": "g1r_native_status", "arguments": {}}}
                    common = {"Accept": "application/json, text/event-stream", "Mcp-Session-Id": first_session}
                    host = await first.post("/mcp", json=body, headers={**common, "Host": "external.example:27124"})
                    self.assertEqual(host.status_code, 421, host.text)
                    origin = await first.post("/mcp", json=body, headers={**common, "Origin": "https://external.example"})
                    self.assertEqual(origin.status_code, 403, origin.text)
                    wrong_type = await first.post("/mcp", content=json.dumps(body),
                        headers={**common, "Content-Type": "text/plain"})
                    self.assertEqual(wrong_type.status_code, 400, wrong_type.text)
                    self.assertEqual(len(native.requests), 3)
                    self.assertEqual(await tool(second, second_session, "g1r_native_status"), native_status)
                    terminated = await first.delete("/mcp", headers=common)
                    self.assertEqual(terminated.status_code, 200, terminated.text)
                    self.assertEqual(await tool(second, second_session, "g1r_native_status"), native_status)
                    self.assertEqual((await first.post("/other", json=body, headers=common)).status_code, 404)
            self.assertEqual(native.connections, 1)
            self.assertEqual([request["op"] for request in native.requests], ["status", "objects", "invoke", "status", "status"])
            self.assertEqual(source.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
