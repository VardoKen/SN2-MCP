"""Общий HTTP и явный reload/watch сохраняют прежний read-only observer."""
import asyncio
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1] / "tools"
spec = importlib.util.spec_from_file_location("world_lifecycle_http_probe", TOOLS / "probe_world_lifecycle_mcp.py")
live = importlib.util.module_from_spec(spec)
spec.loader.exec_module(live)


class Calls:
    def __init__(self, *, baseline_recovers=True, terminal_error=False, cleanup_fails=False, count=1,
                 black_screen=True, watch_baseline=True, terminal_reason="object_or_property_invalidated", delayed_terminal=False):
        self.generation = 0
        self.calls = []
        self.baseline_recovers = baseline_recovers
        self.terminal_error = terminal_error
        self.cleanup_fails = cleanup_fails
        self.count = count
        self.black_screen = black_screen
        self.watch_baseline = watch_baseline
        self.terminal_reason = terminal_reason
        self.delayed_terminal = delayed_terminal
        self.terminal_reads = 0

    async def call(self, name, args):
        self.calls.append((name, args))
        handle = f"obj-{self.generation + 1}"
        if name in {"sn2_status", "sn2_capabilities"}:
            return {}
        if name == "sn2_native_status":
            return {"process_id": 123, "session_id": "native"}
        if name == "sn2_find_objects":
            return {"items": [{"class": live.HOST, "name": live.HOST + f"_{self.generation}",
                               "path": "/Game/Menu." + live.HOST, "handle": handle}]}
        if name == "sn2_object_functions":
            return {"items": [{"name": args["function_query"], "signature_token": "sig-1"}]}
        if name == "sn2_invoke":
            method = args["function"]
            if method == "ReloadCurrentMap":
                self.generation += int(self.baseline_recovers)
            result = {"ReadMarker": live.MARKER, "GetCurrentMapName": "MenuMap",
                      "GetCurrentLoadingScreenType": 1}.get(method)
            observed = {"executed": True, "result": result}
            if method == "GetCurrentLoadingScreenType":
                observed["return_enum"] = {"path": "/Script/G1R.ELoadingScreenType",
                    "value_name": "ELoadingScreenType::BlackScreen" if self.black_screen else "ELoadingScreenType::Default"}
            return observed
        if name == "sn2_object_properties":
            if args["handle"] != handle:
                raise live.ProbeError("stale_handle", "old world")
            return {"items": [{"name": live.COUNT, "supported": True, "value": self.count}]}
        if name == "sn2_watch_start":
            return {"watch_id": "watch-1", "state": "running"}
        if name == "sn2_watch_read":
            if self.terminal_error and self.generation == 2:
                raise live.ProbeError("native_disconnected", "read failed")
            if self.generation == 2:
                self.terminal_reads += 1
                if self.delayed_terminal and self.terminal_reads == 1:
                    return {"state": "running"}
            return {"state": "failed" if self.generation == 2 else "running",
                    "reason": self.terminal_reason if self.generation == 2 else "",
                    "cleanup_complete": self.generation == 2,
                    "events": [{"kind": "baseline", "snapshot": {"process_id": 123, "session_id": "native",
                        "objects": [{"handle": handle, "properties": [{"name": live.COUNT, "value": 1, "supported": True}]}]}}]
                        if self.watch_baseline else []}
        if name in {"sn2_watch_stop", "sn2_watch_forget"}:
            return {"cleanup_complete": not self.cleanup_fails, "forgotten": not self.cleanup_fails}
        raise AssertionError((name, args))


class WorldLifecycleHTTPTests(unittest.TestCase):
    def exercise(self, fixture):
        clock = [0.0]
        async def sleep(seconds):
            clock[0] += seconds
        return asyncio.run(live.exercise_reload_watch(fixture.call, wait_seconds=1,
                          clock=lambda: clock[0], sleep=sleep))

    def test_baseline_then_watch_reload_and_cleanup(self):
        fixture = Calls()
        report = self.exercise(fixture)
        self.assertEqual(report["status"], "completed")
        names = [name for name, _ in fixture.calls]
        reloads = [i for i, (name, args) in enumerate(fixture.calls) if name == "sn2_invoke" and args["function"] == "ReloadCurrentMap"]
        self.assertEqual(len(reloads), 2)
        self.assertLess(reloads[0], names.index("sn2_watch_start"))
        self.assertLess(names.index("sn2_watch_start"), reloads[1])
        self.assertEqual(names[-2:], ["sn2_watch_stop", "sn2_watch_forget"])
        self.assertTrue(report["watch_trial"]["watch_cleanup_confirmed"])

    def test_failed_baseline_does_not_start_watch_or_second_reload(self):
        fixture = Calls(baseline_recovers=False)
        self.assertEqual(self.exercise(fixture)["status"], "baseline_failed")
        self.assertFalse(any(name.startswith("sn2_watch_") for name, _ in fixture.calls))
        self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "ReloadCurrentMap" for name, args in fixture.calls), 1)

    def test_uninitialized_counter_does_not_reload(self):
        fixture = Calls(count=0)
        self.assertEqual(self.exercise(fixture)["status"], "baseline_failed")
        self.assertFalse(any(name == "sn2_invoke" and args["function"] == "ReloadCurrentMap" for name, args in fixture.calls))

    def test_transport_error_is_not_watch_invalidation(self):
        fixture = Calls(terminal_error=True)
        report = self.exercise(fixture)
        self.assertEqual(report["status"], "error")
        self.assertEqual(report["error"]["code"], "native_disconnected")
        self.assertEqual([name for name, _ in fixture.calls][-2:], ["sn2_watch_stop", "sn2_watch_forget"])

    def test_cleanup_failure_prevents_completed(self):
        self.assertEqual(self.exercise(Calls(cleanup_fails=True))["status"], "observed_different")

    def test_black_screen_enum_must_match_actual_name(self):
        fixture = Calls(black_screen=False)
        self.assertEqual(self.exercise(fixture)["status"], "baseline_failed")
        self.assertFalse(any(name == "sn2_watch_start" for name, _ in fixture.calls))

    def test_missing_watch_baseline_prevents_second_reload(self):
        fixture = Calls(watch_baseline=False)
        self.assertEqual(self.exercise(fixture)["status"], "watch_baseline_not_confirmed")
        self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "ReloadCurrentMap" for name, args in fixture.calls), 1)
        self.assertEqual([name for name, _ in fixture.calls][-2:], ["sn2_watch_stop", "sn2_watch_forget"])

    def test_nested_stale_handle_code_is_not_terminal_reason(self):
        self.assertEqual(self.exercise(Calls(terminal_reason="stale_handle"))["status"], "observed_different")

    def test_running_watch_after_reload_waits_for_next_due_sample_without_reloading(self):
        fixture = Calls(delayed_terminal=True)
        self.assertEqual(self.exercise(fixture)["status"], "completed")
        self.assertEqual(fixture.terminal_reads, 2)
        self.assertEqual(sum(name == "sn2_invoke" and args["function"] == "ReloadCurrentMap" for name, args in fixture.calls), 2)

    def run_client(self, *, reload=False, acquire_error=False, release_ok=True):
        calls = []
        async def call(name, args):
            calls.append(name)
            if name == "sn2_session_acquire" and acquire_error:
                raise live.ProbeError("game_session_owned", "another session")
            return {"released": release_ok} if name == "sn2_session_release" else {}
        class Client:
            async def initialize(self):
                return SimpleNamespace(server_info=SimpleNamespace(version="dev"))
        @asynccontextmanager
        async def connect(args):
            yield Client()
        async def exercise(*args, **kwargs):
            calls.append("exercise")
            return {"status": "completed"}
        with patch.object(live, "connect_client", connect), patch.object(live, "_caller", lambda client: call), \
             patch.object(live, "exercise", exercise), patch.object(live, "exercise_reload_watch", exercise):
            try:
                return asyncio.run(live.run_probe(SimpleNamespace(reload_and_watch=reload, wait_seconds=3))), calls
            except Exception as error:
                return error, calls

    def test_default_observation_does_not_acquire(self):
        report, calls = self.run_client()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(calls, ["exercise"])

    def test_reload_claim_and_release_wrap_exercise(self):
        _, calls = self.run_client(reload=True)
        self.assertEqual(calls, ["sn2_status", "sn2_capabilities", "sn2_session_acquire", "exercise", "sn2_session_release"])

    def test_foreign_claim_refusal_does_not_invoke_or_release(self):
        error, calls = self.run_client(reload=True, acquire_error=True)
        self.assertEqual(error.code, "game_session_owned")
        self.assertNotIn("exercise", calls)
        self.assertNotIn("sn2_session_release", calls)

    def test_unconfirmed_release_does_not_return_completed(self):
        error, _ = self.run_client(reload=True, release_ok=False)
        self.assertEqual(error.code, "ownership_release_unconfirmed")

    def test_default_connect_uses_shared_http_without_stdio(self):
        calls = []
        @asynccontextmanager
        async def http(url):
            calls.append(url)
            yield ("incoming", "outgoing", None)
        class Session:
            def __init__(self, incoming, outgoing):
                self.streams = incoming, outgoing
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                return False
        async def connected():
            async with live.connect_client(SimpleNamespace()) as client:
                self.assertEqual(client.streams, ("incoming", "outgoing"))
        with patch.object(live, "streamable_http_client", http), patch.object(live, "ClientSession", Session), \
             patch.object(live, "stdio_client", side_effect=AssertionError("unexpected stdio")):
            asyncio.run(connected())
        self.assertEqual(calls, ["http://127.0.0.1:27124/mcp"])


if __name__ == "__main__":
    unittest.main()
