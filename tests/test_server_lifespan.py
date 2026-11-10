"""Shared shutdown closes runtime resources without changing the game."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sn2_mcp.server import create_server


class ServerLifespanTests(unittest.IsolatedAsyncioTestCase):
    def roots(self, temporary):
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        return workspace

    async def test_shutdown_does_not_create_runtime_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.roots(temporary)
            native = Mock()
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                    patch("sn2_mcp.server.RuntimeService") as service_factory:
                server = create_server(workspace, native_port=28223)
                async with server.settings.lifespan(server):
                    await server.call_tool("sn2_status", {})
                    native.close.assert_not_called()
                    service_factory.assert_not_called()
                native.close.assert_called_once_with()
                service_factory.assert_not_called()

    async def test_shutdown_closes_shared_client_and_created_runtime_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.roots(temporary)
            native, service = Mock(), Mock()
            service.process_state.return_value = {"running": False, "processes": [], "selected": False}
            with patch("sn2_mcp.server.NativeClient", return_value=native) as factory, \
                    patch("sn2_mcp.server.RuntimeService", return_value=service) as service_factory:
                server = create_server(workspace, native_port=28223)
                async with server.settings.lifespan(server):
                    await server.call_tool("sn2_target_processes", {})
                    await server.call_tool("sn2_target_processes", {})
                    native.close.assert_not_called()
                    service.close.assert_not_called()
                factory.assert_called_once_with(port=28223)
                native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                self.assertEqual(service.process_state.call_count, 2)
                native.request.assert_not_called()
                service.load_native.assert_not_called()
                service_factory.assert_called_once()

    async def test_exception_still_closes_resources_without_game_operation(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.roots(temporary)
            native, service = Mock(), Mock()
            service.process_state.return_value = {"running": False, "processes": []}
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                    patch("sn2_mcp.server.RuntimeService", return_value=service):
                server = create_server(workspace)
                with self.assertRaisesRegex(RuntimeError, "fixture shutdown"):
                    async with server.settings.lifespan(server):
                        await server.call_tool("sn2_target_processes", {})
                        raise RuntimeError("fixture shutdown")
                native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                service.start.assert_not_called()
                service.stop.assert_not_called()
                service.load_native.assert_not_called()

    async def test_native_close_failure_still_releases_runtime_resources(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = self.roots(temporary)
            native, service = Mock(), Mock()
            native.close.side_effect = RuntimeError("fixture close failure")
            service.process_state.return_value = {"running": False, "processes": []}
            with patch("sn2_mcp.server.NativeClient", return_value=native), \
                    patch("sn2_mcp.server.RuntimeService", return_value=service):
                server = create_server(workspace)
                with self.assertRaisesRegex(RuntimeError, "fixture close failure"):
                    async with server.settings.lifespan(server):
                        await server.call_tool("sn2_target_processes", {})
                native.close.assert_called_once_with()
                service.close.assert_called_once_with()
                service.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
