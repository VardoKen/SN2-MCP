"""Shared HTTP observation access and lost-response recovery with neutral state."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from sn2_mcp.server import create_server


class DropCompletedToolResponse(httpx2.AsyncBaseTransport):
    """Lose one fully completed response without repeating the tool call."""

    def __init__(self, app, tool_name):
        self.inner = httpx2.ASGITransport(app=app)
        self.tool_name = tool_name
        self.discarded = None

    async def handle_async_request(self, request):
        response = await self.inner.handle_async_request(request)
        body = json.loads(request.content) if request.method == "POST" else {}
        if body.get("params", {}).get("name") == self.tool_name and self.discarded is None:
            payload = json.loads(await response.aread())
            self.discarded = payload["result"]["structuredContent"]
            await response.aclose()
            raise httpx2.ReadError("Fixture completed response lost", request=request)
        return response

    async def aclose(self):
        await self.inner.aclose()


class SubnauticaHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_lost_watch_start_is_observable_and_stoppable_by_another_client(self):
        owner = "shared_native_connection"
        watch = {"watch_id": "watch-fixture", "session_id": "native-fixture", "client_owner": owner,
                 "state": "running", "reason": "", "cleanup_complete": False, "next_seq": 2,
                 "last_seq": 1, "oldest_seq": 1, "dropped_events": 0, "capacity": 2}
        terminal = {**watch, "state": "stopped", "reason": "explicit_stop", "cleanup_complete": True,
                    "next_seq": 3, "last_seq": 2}
        events = {**watch, "events": [{"seq": 1, "kind": "baseline"}], "cursor": 1,
                  "gap": False, "truncated": False}
        start = {"handle": "obj-fixture", "properties": ["Enabled"], "duration_ms": 500,
                 "interval_ms": 20, "capacity": 2}
        expected = [
            ("watch_start", {**start, "client_owner": owner}, watch),
            ("watch_list", {"client_owner": owner}, {"session_id": "native-fixture", "items": [watch]}),
            ("watch_read", {"watch_id": "watch-fixture", "after_seq": 0, "limit": 100,
                            "client_owner": owner}, events),
            ("watch_stop", {"watch_id": "watch-fixture", "client_owner": owner}, terminal),
            ("watch_list", {"client_owner": owner}, {"session_id": "native-fixture", "items": [terminal]}),
        ]
        observed = []

        def boundary(operation, parameters):
            planned_operation, planned_parameters, result = expected[len(observed)]
            self.assertEqual((operation, parameters), (planned_operation, planned_parameters))
            observed.append((operation, parameters))
            return result

        with tempfile.TemporaryDirectory() as temporary:
            native = Mock()
            native.request.side_effect = boundary
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                    patch("sn2_mcp.server.RuntimeService") as service_factory:
                server = create_server(Path(temporary), transport="streamable-http")
                security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                    allowed_hosts=["127.0.0.1:27224"], allowed_origins=["http://127.0.0.1:27224"])
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True, transport_security=security)
                lossy = DropCompletedToolResponse(app, "sn2_watch_start")
                counter = 0

                async def rpc(client, method, parameters, session=None):
                    nonlocal counter
                    counter += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": counter, "method": method, "params": parameters})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response

                async def initialize(client, name):
                    response = await rpc(client, "initialize", {"protocolVersion": "2025-06-18",
                        "capabilities": {}, "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    response = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(response.status_code, 202)
                    return session

                async def tool(client, session, name, arguments=None):
                    response = await rpc(client, "tools/call", {"name": name, "arguments": arguments or {}}, session)
                    payload = response.json()["result"]
                    self.assertFalse(payload.get("isError", False), payload)
                    return payload["structuredContent"]

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=lossy, base_url="http://127.0.0.1:27224") as first, \
                            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                              base_url="http://127.0.0.1:27224") as second:
                        one, two = await initialize(first, "first-client"), await initialize(second, "second-client")
                        self.assertNotEqual(one, two)
                        catalog = (await rpc(second, "tools/list", {}, two)).json()["result"]["tools"]
                        self.assertEqual(len(catalog), 22)
                        self.assertNotIn("sn2_session_acquire", {item["name"] for item in catalog})
                        with self.assertRaises(httpx2.ReadError):
                            await tool(first, one, "sn2_watch_start", start)
                        self.assertEqual(lossy.discarded, watch)
                        self.assertEqual(await tool(second, two, "sn2_watch_list"),
                                         {"session_id": "native-fixture", "items": [watch]})
                        self.assertEqual(await tool(second, two, "sn2_watch_read", {"watch_id": "watch-fixture"}), events)
                        self.assertEqual(await tool(second, two, "sn2_watch_stop", {"watch_id": "watch-fixture"}), terminal)
                        self.assertEqual(await tool(first, one, "sn2_watch_list"),
                                         {"session_id": "native-fixture", "items": [terminal]})
                        native.close.assert_not_called()
                        service_factory.assert_not_called()
                native.close.assert_called_once_with()
                self.assertEqual(len(observed), len(expected))
                self.assertEqual([op for op, _ in observed].count("watch_start"), 1)


if __name__ == "__main__":
    unittest.main()
