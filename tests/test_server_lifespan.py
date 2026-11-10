"""Общий shutdown закрывает ресурсы однажды; отдельный запрос их не закрывает."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from g1r_mcp.server import create_server


class ServerLifespanTests(unittest.IsolatedAsyncioTestCase):
    def roots(self, temporary):
        root = Path(temporary)
        modkit, workspace = root / "modkit", root / "workspace"
        (modkit / "Script").mkdir(parents=True)
        (modkit / "Script/Fixture.as").write_text("class UFixture {}\n", encoding="utf-8")
        workspace.mkdir()
        return modkit, workspace

    async def test_shutdown_does_not_create_development_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            modkit, workspace = self.roots(temporary)
            native = Mock()
            with patch("g1r_mcp.server.NativeClient", return_value=native), \
                    patch("g1r_mcp.server.DevelopmentService") as service_factory:
                server = create_server(modkit, workspace, native_port=28123)
                async with server.settings.lifespan(server):
                    await server.call_tool("g1r_status", {})
                    native.close.assert_not_called()
                    service_factory.assert_not_called()
                native.close.assert_called_once_with()
                service_factory.assert_not_called()

    async def test_shutdown_closes_replaced_current_client_and_created_service(self):
        identity = {"pid": 123, "creation_time": "2026-10-31T00:01:02Z",
                    "executable": "C:/isolated/Game/G1R-Win64-Shipping.exe"}
        stopped = {"state": "stop_result", "process": identity,
                   "tool_result": {"state": "stopped", "process": identity},
                   "outcome_unknown": False}
        with tempfile.TemporaryDirectory() as temporary:
            modkit, workspace = self.roots(temporary)
            old_native, current_native, service = Mock(), Mock(), Mock()
            service.stop.return_value = stopped
            with patch("g1r_mcp.server.NativeClient", side_effect=[old_native, current_native]) as factory, \
                    patch("g1r_mcp.server.DevelopmentService", return_value=service) as service_factory:
                server = create_server(modkit, workspace, native_port=28123)
                async with server.settings.lifespan(server):
                    await server.call_tool("g1r_development_stop", {"expected_process": identity})
                    old_native.close.assert_called_once_with()
                    current_native.close.assert_not_called()
                    service.close.assert_not_called()
                self.assertEqual(factory.call_args_list, [call(port=28123), call(port=28123)])
                old_native.close.assert_called_once_with()
                current_native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                service.stop.assert_called_once_with(expected_process=identity)
                service_factory.assert_called_once()

    async def test_exception_still_closes_resources_without_game_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            modkit, workspace = self.roots(temporary)
            native, service = Mock(), Mock()
            service.process_state.return_value = {"running": False, "processes": []}
            with patch("g1r_mcp.server.NativeClient", return_value=native), \
                    patch("g1r_mcp.server.DevelopmentService", return_value=service):
                server = create_server(modkit, workspace)
                with self.assertRaisesRegex(RuntimeError, "fixture shutdown"):
                    async with server.settings.lifespan(server):
                        await server.call_tool("g1r_development_processes", {})
                        raise RuntimeError("fixture shutdown")
                native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                service.start.assert_not_called()
                service.stop.assert_not_called()
                service.load_native.assert_not_called()

    async def test_native_close_failure_still_releases_development_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            modkit, workspace = self.roots(temporary)
            native, service = Mock(), Mock()
            native.close.side_effect = RuntimeError("fixture close failure")
            service.process_state.return_value = {"running": False, "processes": []}
            with patch("g1r_mcp.server.NativeClient", return_value=native), \
                    patch("g1r_mcp.server.DevelopmentService", return_value=service):
                server = create_server(modkit, workspace)
                with self.assertRaisesRegex(RuntimeError, "fixture close failure"):
                    async with server.settings.lifespan(server):
                        await server.call_tool("g1r_development_processes", {})
                native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                service.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
