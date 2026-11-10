"""Any HTTP client can control an explicit companion-package development launch."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import httpx2
from mcp.server.transport_security import TransportSecuritySettings

from g1r_mcp.development import CommandResult, DevelopmentService
from g1r_mcp.native_client import NativeClient
from g1r_mcp.server import create_server
from tests.test_mcp_observations import SessionNativeFixture


class HTTPDevelopmentCompanionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._asyncioRunner.get_loop().slow_callback_duration = 2.0

    async def test_two_clients_start_apply_load_restart_and_stop_without_session_acquire(self):
        native_status = {"ready": True, "session_id": "shared-companion-tcp", "tick_count": 125}
        steps = [
            {"op": "status", "params": {}, "result": native_status},
            {"op": "status", "params": {}, "result": native_status},
        ]
        with tempfile.TemporaryDirectory() as temporary, SessionNativeFixture(steps) as native:
            root = Path(temporary)
            repository, workspace, modkit, game = (
                root / "repository", root / "workspace", root / "Modkit", root / "game")
            (repository / "tools").mkdir(parents=True)
            for name in ("game_process_state.ps1", "start_g1r_development.ps1",
                         "stop_g1r_development.ps1", "load_native.ps1"):
                (repository / "tools" / name).write_text("# isolated executor\n", encoding="utf-8")
            dll = repository / "build" / "native" / "Release" / "G1R_MCP.dll"
            dll.parent.mkdir(parents=True)
            dll.write_bytes(b"isolated companion DLL")
            workspace.mkdir()
            (modkit / "Script").mkdir(parents=True)
            source = modkit / "Script" / "Fixture.as"
            source.write_text("class UFixture {}\n", encoding="utf-8")
            original_source = source.read_bytes()
            game.mkdir()
            package_paths = ["mods/Primary", "helpers/Companion", "unused/Unselected"]
            original_packages = {}
            for relative in package_paths:
                package = workspace / relative
                (package / "Script").mkdir(parents=True)
                (package / "GothicMod.json").write_text(json.dumps({
                    "name": package.name, "title": package.name, "author": "Test",
                    "modVersion": "1.0.0"}), encoding="utf-8")
                (package / "Script" / "Fixture.as").write_bytes(
                    f"class U{package.name} {{}}\r\n".encode("utf-8"))
                original_packages[relative] = {
                    path.relative_to(package).as_posix(): path.read_bytes()
                    for path in package.rglob("*") if path.is_file()}
            executor_calls = []
            current_process = None
            launch_count = 0
            identities = [{"pid": 123 + index, "creation_time": f"2099-11-08T00:01:02.{index:07d}Z",
                           "executable": str(game / "G1R" / "Binaries" / "Win64" / "G1R-Win64-Shipping.exe")}
                          for index in range(2)]

            def execute(arguments, timeout):
                nonlocal current_process, launch_count
                executor_calls.append((list(arguments), timeout))
                script = Path(arguments[arguments.index("-File") + 1]).name
                if script == "game_process_state.ps1":
                    return CommandResult(0, json.dumps({"processes": [current_process] if current_process else []}))
                if script == "load_native.ps1":
                    self.assertEqual(arguments[arguments.index("-ExpectedPid") + 1], str(current_process["pid"]))
                    return CommandResult(0, json.dumps({"state": "loaded", "process": current_process,
                        "dll_sha256": arguments[arguments.index("-ExpectedDllSha256") + 1]}))
                if script == "stop_g1r_development.ps1":
                    stopped, current_process = current_process, None
                    return CommandResult(0, json.dumps({"state": "stopped", "process": stopped}))
                self.assertEqual(script, "start_g1r_development.ps1")
                self.assertIn("-ModsCsv", arguments)
                self.assertNotIn("-Mods", arguments)
                if launch_count:
                    self.assertIn("-Restart", arguments)
                    self.assertEqual(arguments[arguments.index("-ExpectedPid") + 1], str(current_process["pid"]))
                else:
                    self.assertNotIn("-Restart", arguments)
                current_process = identities[launch_count]
                launch_count += 1
                return CommandResult(0, json.dumps({
                    "state": "launch_requested",
                    "mods": arguments[arguments.index("-ModsCsv") + 1].split(","),
                    "mods_directory": arguments[arguments.index("-ModsDirectory") + 1],
                }))

            leases = []

            def capture_process(identity):
                lease = Mock()
                lease.poll.return_value = {"state": "running", "process": identity}
                leases.append(lease)
                return lease

            service = DevelopmentService(repository_root=repository, workspace_root=workspace,
                modkit_root=modkit, game_root=game, executor=execute, powershell="fixture-powershell",
                process_lease_factory=capture_process)
            self.addCleanup(service.close)
            backend = NativeClient(port=native.port)
            self.addCleanup(backend.close)
            replacement_backends = [NativeClient(port=native.port) for _ in range(3)]
            for replacement in replacement_backends:
                self.addCleanup(replacement.close)
            with patch("g1r_mcp.server.NativeClient", side_effect=[backend, *replacement_backends]) as native_factory, \
                    patch("g1r_mcp.server.DevelopmentService", return_value=service) as service_factory:
                server = create_server(modkit, workspace, native_port=native.port,
                                       game_root=game, transport="streamable-http")
                native_factory.assert_called_once_with(port=native.port)
                app = server.streamable_http_app(host="127.0.0.1", streamable_http_path="/mcp",
                    stateless_http=False, json_response=True,
                    transport_security=TransportSecuritySettings(
                        enable_dns_rebinding_protection=True, allowed_hosts=["127.0.0.1:27124"],
                        allowed_origins=["http://127.0.0.1:27124"]))
                request_id = 0

                async def rpc(client, method, params, session=None):
                    nonlocal request_id
                    request_id += 1
                    headers = {"Accept": "application/json, text/event-stream"}
                    if session is not None:
                        headers["Mcp-Session-Id"] = session
                    response = await client.post("/mcp", headers=headers,
                        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
                    self.assertEqual(response.status_code, 200, response.text)
                    return response.json()["result"], response

                async def initialize(client, name):
                    _, response = await rpc(client, "initialize", {
                        "protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": name, "version": "1"}})
                    session = response.headers["mcp-session-id"]
                    response = await client.post("/mcp", headers={"Mcp-Session-Id": session,
                        "Accept": "application/json, text/event-stream"},
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    self.assertEqual(response.status_code, 202, response.text)
                    return session

                async def call(client, session, name, arguments=None):
                    result, _ = await rpc(client, "tools/call", {
                        "name": name, "arguments": arguments or {}}, session)
                    self.assertFalse(result.get("isError", False), result)
                    return result["structuredContent"]

                async with app.router.lifespan_context(app):
                    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                            base_url="http://127.0.0.1:27124") as first, \
                            httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                            base_url="http://127.0.0.1:27124") as second:
                        first_session = await initialize(first, "companion-first")
                        second_session = await initialize(second, "companion-second")
                        self.assertNotEqual(first_session, second_session)
                        self.assertEqual(await call(first, first_session, "g1r_native_status"), native_status)
                        recipe_arguments = {"relative_package": "mods/Primary",
                                            "companion_packages": ["helpers/Companion"]}
                        recipe = await call(first, first_session, "g1r_development_recipe", recipe_arguments)
                        observed_recipe = await call(second, second_session, "g1r_development_recipe", recipe_arguments)
                        self.assertEqual(recipe["manifest"], observed_recipe["manifest"])
                        manifest = recipe["manifest"]
                        self.assertEqual(manifest["composition"], "explicit_companion_packages")
                        self.assertEqual(manifest["selected_mods"], ["Primary", "Companion"])
                        self.assertEqual([(item["role"], item["package_path"]) for item in manifest["packages"]],
                                         [("main", "mods/Primary"), ("companion", "helpers/Companion")])
                        self.assertIsNone(recipe["launch"]["mods_directory"])
                        self.assertEqual(recipe["launch"]["mods"], ["Primary", "Companion"])
                        self.assertEqual(recipe["launch"]["staging_created_by"], "development_start")
                        self.assertEqual(recipe["launch"]["edit_target"], "staged_copy_only")
                        self.assertIs(recipe["launch"]["source_packages_modified"], False)
                        self.assertEqual(recipe["native_artifact"]["sha256"], hashlib.sha256(dll.read_bytes()).hexdigest())
                        self.assertFalse((workspace / "artifacts").exists())
                        start_arguments = {**recipe_arguments,
                                           "expected_manifest_sha256": manifest["manifest_sha256"]}
                        self.assertFalse((workspace / "artifacts").exists())
                        started = await call(second, second_session, "g1r_development_start", start_arguments)
                        self.assertEqual(started["state"], "launch_requested")
                        self.assertEqual(started["manifest_sha256"], manifest["manifest_sha256"])
                        self.assertIs(started["compiled"], False)
                        self.assertIs(started["game_ready"], False)
                        self.assertEqual(started["edit_target"], "staged_copy_only")
                        self.assertEqual(started["staging_retention"], "retain_for_review")
                        self.assertIs(started["source_packages_modified"], False)
                        staging = started["development_staging"]
                        stage_directory = workspace / staging["relative_directory"]
                        self.assertEqual(stage_directory.parent, workspace / "artifacts" / "development-sessions")
                        self.assertRegex(stage_directory.name, r"^[0-9a-f]{32}$")
                        self.assertEqual(Path(staging["mods_directory"]), stage_directory / "packages")
                        self.assertEqual(staging["source_manifest_sha256"], manifest["manifest_sha256"])
                        self.assertEqual(started["launch_observation"]["development_staging"], staging)
                        self.assertEqual(started["active_staging_packages"], [
                            {key: item[key] for key in ("role", "source_package_path", "relative_package",
                                "package_name", "manifest_sha256_at_launch")}
                            for item in staging["packages"]])
                        self.assertEqual(json.loads((stage_directory / "session.json").read_text(encoding="utf-8")), staging)
                        self.assertEqual({path.name for path in (stage_directory / "packages").iterdir()},
                                         {"Primary", "Companion"})
                        for item in staging["packages"]:
                            staged_package = workspace / item["relative_package"]
                            self.assertEqual({path.relative_to(staged_package).as_posix(): path.read_bytes()
                                for path in staged_package.rglob("*") if path.is_file()},
                                original_packages[item["source_package_path"]])
                            self.assertEqual(item["manifest_sha256_at_launch"], item["manifest"]["manifest_sha256"])
                        launch_calls = [arguments for arguments, _ in executor_calls
                            if Path(arguments[arguments.index("-File") + 1]).name == "start_g1r_development.ps1"]
                        self.assertEqual(len(launch_calls), 1)
                        self.assertEqual(launch_calls[0][launch_calls[0].index("-ModsCsv") + 1], "Primary,Companion")
                        self.assertEqual(launch_calls[0][launch_calls[0].index("-ModsDirectory") + 1], staging["mods_directory"])
                        canonical_file = workspace / "mods" / "Primary" / "Script" / "Fixture.as"
                        original_file = canonical_file.read_bytes()
                        changed_file = b"class UPrimary { int Marker = 2; }\r\n"
                        canonical_file.write_bytes(changed_file)
                        applied = await call(first, first_session, "g1r_apply_staged_file", {
                            "relative_package": "mods/Primary", "file_path": "Script/Fixture.as",
                            "expected_source_sha256": hashlib.sha256(changed_file).hexdigest(),
                            "expected_staged_sha256": hashlib.sha256(original_file).hexdigest(),
                            "expected_process": identities[0]})
                        self.assertEqual(applied["process"], identities[0])
                        self.assertFalse(applied["source_modified"])
                        self.assertEqual(canonical_file.read_bytes(), changed_file)
                        staged_primary = next(item for item in staging["packages"] if item["role"] == "main")
                        self.assertEqual((workspace / staged_primary["relative_package"] / "Script" / "Fixture.as").read_bytes(), changed_file)
                        canonical_file.write_bytes(original_file)
                        loaded = await call(first, first_session, "g1r_native_load", {
                            "expected_process": identities[0], "expected_dll_sha256": recipe["native_artifact"]["sha256"]})
                        self.assertEqual(loaded["process"], identities[0])
                        before_stages = set(stage_directory.parent.iterdir())
                        restarted = await call(first, first_session, "g1r_development_start", {
                            **start_arguments, "restart": True, "expected_process": identities[0]})
                        self.assertEqual(restarted["state"], "launch_requested")
                        self.assertEqual(len(set(stage_directory.parent.iterdir()) - before_stages), 1)
                        self.assertNotEqual(restarted["development_staging"]["relative_directory"], staging["relative_directory"])
                        stale_information = await call(second, second_session, "g1r_session_acquire", {
                            "reason": "Retained information about the previous process", "expected_process": identities[0]})
                        self.assertEqual(stale_information["claim"]["process"], identities[0])
                        self.assertFalse(stale_information["control_required"])
                        reloaded = await call(second, second_session, "g1r_native_load", {
                            "expected_process": identities[1], "expected_dll_sha256": recipe["native_artifact"]["sha256"]})
                        self.assertEqual(reloaded["process"], identities[1])
                        stopped = await call(second, second_session, "g1r_development_stop", {"expected_process": identities[1]})
                        self.assertEqual(stopped["tool_result"], {"state": "stopped", "process": identities[1]})
                        self.assertFalse(stopped["outcome_unknown"])
                        self.assertIsNone(current_process)
                        self.assertEqual(await call(second, second_session, "g1r_native_status"), native_status)
                service_factory.assert_called_once()
                self.assertEqual(native_factory.call_args_list, [unittest.mock.call(port=native.port)] * 4)
                self.assertEqual(native.connections, 2)
                self.assertEqual(len(leases), 2)
                leases[0].close.assert_called_once()
            self.assertEqual([request["op"] for request in native.requests], ["status", "status"])
            self.assertEqual(len({request["id"] for request in native.requests}), 2)
            mutating_scripts = [Path(arguments[arguments.index("-File") + 1]).name
                for arguments, _ in executor_calls if Path(arguments[arguments.index("-File") + 1]).name != "game_process_state.ps1"]
            self.assertEqual(mutating_scripts, ["start_g1r_development.ps1", "load_native.ps1",
                "start_g1r_development.ps1", "load_native.ps1", "stop_g1r_development.ps1"])
            self.assertEqual(source.read_bytes(), original_source)
            for relative, original in original_packages.items():
                package = workspace / relative
                self.assertEqual({path.relative_to(package).as_posix(): path.read_bytes()
                    for path in package.rglob("*") if path.is_file()}, original)


if __name__ == "__main__":
    unittest.main()
