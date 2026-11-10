"""Проверки владения процессом и однократных действий development MCP probe."""

import copy
import asyncio
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import probe_development_mcp as probe


class FakeMCP:
    def __init__(self):
        self.calls = []
        self.started = False
        self.stopped = False
        self.fail_load = False
        self.bad_signature = False
        self.bad_value = False
        self.preexisting = False
        self.process = {"pid": 52, "creation_time": "2026-10-31T00:00:00.0000000Z",
                        "executable": "E:/Game/G1R/Binaries/Win64/G1R-Win64-Shipping.exe"}
        self.recipe = {"manifest": {"manifest_sha256": "a" * 64, "game_root": "E:/Game"},
                       "native_artifact": {"available": True, "sha256": "b" * 64}}

    async def call(self, name, arguments):
        self.calls.append((name, copy.deepcopy(arguments)))
        if name == "g1r_development_recipe":
            return copy.deepcopy(self.recipe)
        if name == "g1r_development_processes":
            return {"processes": [dict(self.process)] if (self.preexisting or self.started) and not self.stopped else []}
        if name == "g1r_development_start":
            self.started = True
            return {"state": "launch_requested", "outcome_unknown": False}
        if name == "g1r_native_load":
            if self.fail_load:
                raise probe.ProbeError("load_unknown", "Запрос мог исполниться")
            return {"state": "load_result", "process": dict(self.process),
                    "dll_sha256": "b" * 64, "outcome_unknown": False}
        if name in {"g1r_native_status", "g1r_native_enable"}:
            return {"process_id": 52, "ready": True, "hook_installed": True, "version": "1.0.0-dev.4"}
        if name == "g1r_find_objects":
            return {"items": [{"name": "Default__G1RMCPCompileProbe", "class": "G1RMCPCompileProbe",
                               "path": "/Script/Angelscript.Default__G1RMCPCompileProbe", "handle": "obj-1"}]}
        if name == "g1r_object_functions":
            return {"items": [{"name": "ReadProbeValue", "parameter_size": 4,
                               "parameters": [{"return": True, "type": "StrProperty" if self.bad_signature else "IntProperty",
                                               "element_size": 4, "reference": False}]}]}
        if name == "g1r_invoke":
            return {"executed": True, "result": 1 if self.bad_value else 2}
        if name == "g1r_development_stop":
            self.stopped = True
            return {"state": "stop_result", "process": dict(self.process), "tool_result": {"state": "stopped"}}
        raise AssertionError(name)


class DevelopmentCycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_uses_exact_owned_identity_and_stops(self):
        fake = FakeMCP()
        result = await probe.development_cycle(fake.call, "artifacts/probe", 2)
        self.assertTrue(result["compiled_probe"])
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(result["stages"][-1], "game_stopped")
        mutations = [name for name, _ in fake.calls if name in {"g1r_development_start", "g1r_native_load", "g1r_invoke", "g1r_development_stop"}]
        self.assertEqual(mutations, ["g1r_development_start", "g1r_native_load", "g1r_invoke", "g1r_development_stop"])
        self.assertEqual(next(args for name, args in fake.calls if name == "g1r_development_stop"),
                         {"expected_process": fake.process})

    async def test_preexisting_game_is_never_changed(self):
        fake = FakeMCP()
        fake.preexisting = True
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(fake.call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "game_already_running")
        self.assertEqual([name for name, _ in fake.calls], ["g1r_development_recipe", "g1r_development_processes"])

    async def test_signature_failure_stops_without_invoke(self):
        fake = FakeMCP()
        fake.bad_signature = True
        with self.assertRaises(probe.ProbeError):
            await probe.development_cycle(fake.call, "artifacts/probe", 2)
        self.assertTrue(fake.stopped)
        self.assertNotIn("g1r_invoke", [name for name, _ in fake.calls])

    async def test_unknown_load_is_not_replayed_and_game_stops(self):
        fake = FakeMCP()
        fake.fail_load = True
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(fake.call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "load_unknown")
        self.assertEqual(sum(name == "g1r_native_load" for name, _ in fake.calls), 1)
        self.assertTrue(fake.stopped)

    async def test_incorrect_result_is_not_replayed_and_game_stops(self):
        fake = FakeMCP()
        fake.bad_value = True
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(fake.call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "probe_value_mismatch")
        self.assertEqual(sum(name == "g1r_invoke" for name, _ in fake.calls), 1)
        self.assertTrue(fake.stopped)

    async def test_changed_manifest_stops_before_native_load(self):
        fake = FakeMCP()
        async def call(name, args):
            response = await fake.call(name, args)
            if name == "g1r_development_recipe" and fake.started:
                response["manifest"]["manifest_sha256"] = "c" * 64
            return response
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "package_changed")
        self.assertTrue(fake.stopped)
        self.assertNotIn("g1r_native_load", [name for name, _ in fake.calls])

    async def test_listener_then_slow_tick_readiness_repeats_only_reads(self):
        fake = FakeMCP()
        current_time = [0.0]
        status_calls = [0]
        async def sleep(seconds):
            current_time[0] += seconds
        async def call(name, args):
            result = await fake.call(name, args)
            if name == "g1r_native_status":
                status_calls[0] += 1
                if status_calls[0] == 1:
                    raise probe.ProbeError("native_unavailable", "Init-поток ещё не открыл listener")
                result["hook_installed"] = current_time[0] > .1
                result["ready"] = current_time[0] >= 80
                result["tick_count"] = 1 if result["ready"] else 0
            elif name == "g1r_native_enable":
                result["hook_installed"] = True
                result["ready"] = False
                result["tick_count"] = 0
            return result
        result = await probe.development_cycle(call, "artifacts/probe", 2,
                sleep=sleep, clock=lambda: current_time[0], startup_timeout=90, ready_timeout=120)
        self.assertGreaterEqual(current_time[0], 80)
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(sum(name == "g1r_native_load" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "g1r_native_enable" for name, _ in fake.calls), 1)
        self.assertGreater(status_calls[0], 2)

    async def test_bootstrap_only_timeout_stops_captured_outer_once(self):
        fake = FakeMCP()
        outer = {**fake.process, "pid": 42, "executable": "E:/Game/G1R-Win64-Shipping.exe"}
        elapsed = [0.0]
        async def sleep(seconds):
            elapsed[0] += seconds
        async def call(name, args):
            if name == "g1r_development_processes" and fake.started and not fake.stopped:
                fake.calls.append((name, copy.deepcopy(args)))
                return {"processes": [dict(outer)]}
            if name == "g1r_development_stop":
                fake.calls.append((name, copy.deepcopy(args)))
                fake.stopped = True
                return {"state": "stop_result", "process": dict(outer), "tool_result": {"state": "stopped"}}
            return await fake.call(name, args)
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2, startup_timeout=1,
                                           clock=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(captured.exception.code, "game_start_timeout")
        stops = [args for name, args in fake.calls if name == "g1r_development_stop"]
        self.assertEqual(stops, [{"expected_process": outer}])
        self.assertNotIn("g1r_native_load", [name for name, _ in fake.calls])

    async def test_replaced_bootstrap_cleanup_does_not_retarget_new_pid(self):
        fake = FakeMCP()
        outer = {**fake.process, "pid": 42, "executable": "E:/Game/G1R-Win64-Shipping.exe"}
        replacement = {**outer, "pid": 43}
        seen = [0]
        elapsed = [0.0]
        async def sleep(seconds):
            elapsed[0] += seconds
        async def call(name, args):
            if name == "g1r_development_processes" and fake.started:
                fake.calls.append((name, copy.deepcopy(args)))
                seen[0] += 1
                return {"processes": [dict(outer if seen[0] == 1 else replacement)]}
            if name == "g1r_development_stop":
                fake.calls.append((name, copy.deepcopy(args)))
                raise probe.ProbeError("process_changed", "Исходный PID уже отсутствует")
            return await fake.call(name, args)
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2,
                                           clock=lambda: elapsed[0], sleep=sleep)
        self.assertEqual(captured.exception.code, "cleanup_failed")
        stops = [args for name, args in fake.calls if name == "g1r_development_stop"]
        self.assertEqual(stops, [{"expected_process": outer}])
        self.assertNotIn({"expected_process": replacement}, stops)

    async def test_cancelled_start_settles_receipt_and_cleans_owned_process(self):
        fake = FakeMCP()
        launched = asyncio.Event()
        release = asyncio.Event()
        async def call(name, args):
            result = await fake.call(name, args)
            if name == "g1r_development_start":
                launched.set()
                await release.wait()
            return result
        task = asyncio.create_task(probe.development_cycle(call, "artifacts/probe", 2))
        await launched.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(fake.stopped)
        self.assertEqual(sum(name == "g1r_development_start" for name, _ in fake.calls), 1)
        self.assertEqual(sum(name == "g1r_development_stop" for name, _ in fake.calls), 1)

    async def test_uncertain_start_does_not_adopt_unproven_process_or_retry(self):
        fake = FakeMCP()
        async def call(name, args):
            result = await fake.call(name, args)
            if name == "g1r_development_start":
                error = probe.ProbeError("operation_timeout", "Steam-запрос уже мог исполниться")
                error.outcome_unknown = True
                raise error
            return result
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "operation_timeout")
        self.assertFalse(fake.stopped)
        self.assertEqual(sum(name == "g1r_development_start" for name, _ in fake.calls), 1)
        self.assertNotIn("g1r_native_load", [name for name, _ in fake.calls])
        self.assertNotIn("g1r_development_stop", [name for name, _ in fake.calls])
        self.assertTrue(captured.exception.outcome_unknown)
        self.assertTrue(captured.exception.__notes__)

    async def test_definite_start_refusal_never_adopts_concurrent_user_game(self):
        fake = FakeMCP()
        async def call(name, args):
            if name == "g1r_development_start":
                fake.calls.append((name, copy.deepcopy(args)))
                fake.preexisting = True
                raise probe.ProbeError("game_running", "Пользователь запустил игру после начального снимка")
            return await fake.call(name, args)
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2)
        self.assertEqual(captured.exception.code, "game_running")
        self.assertFalse(fake.stopped)
        self.assertNotIn("g1r_development_stop", [name for name, _ in fake.calls])

    async def test_repeated_cancel_does_not_interrupt_owned_stop(self):
        fake = FakeMCP()
        loading = asyncio.Event()
        stopping = asyncio.Event()
        release = asyncio.Event()
        async def call(name, args):
            if name == "g1r_native_load":
                fake.calls.append((name, copy.deepcopy(args)))
                loading.set()
                await asyncio.Future()
            if name == "g1r_development_stop":
                stopping.set()
                await release.wait()
            return await fake.call(name, args)
        task = asyncio.create_task(probe.development_cycle(call, "artifacts/probe", 2))
        await loading.wait()
        task.cancel()
        await stopping.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises((asyncio.CancelledError, probe.ProbeError)):
            await task
        self.assertTrue(fake.stopped)
        self.assertEqual(sum(name == "g1r_development_stop" for name, _ in fake.calls), 1)

    async def test_optional_probe_uses_existing_owned_cleanup(self):
        fake = FakeMCP()
        async def callback(call):
            return {"read_only_fixture": True}
        result = await probe.development_cycle(fake.call, "artifacts/probe", 2, probe=callback)
        self.assertEqual(result["probe_result"], {"read_only_fixture": True})
        self.assertTrue(result["stop_confirmed"])
        self.assertNotIn("g1r_find_objects", [name for name, _ in fake.calls])

    async def test_ingame_tools_flag_is_consistent_across_recipe_and_start(self):
        fake = FakeMCP()
        async def callback(call):
            return {"read_only_lifecycle": True}
        result = await probe.development_cycle(fake.call, "artifacts/probe", 2,
                                               probe=callback, enable_ingame_tools=True)
        flags = [args["enable_ingame_tools"] for name, args in fake.calls
                 if name in {"g1r_development_recipe", "g1r_development_start"}]
        self.assertEqual(flags, [True, True, True])
        self.assertTrue(result["stop_confirmed"])
        invalid = FakeMCP()
        with self.assertRaises(probe.ProbeError):
            await probe.development_cycle(invalid.call, "artifacts/probe", 2, enable_ingame_tools="yes")
        self.assertEqual(invalid.calls, [])

    async def test_script_error_mode_is_forwarded_through_owned_lifecycle(self):
        for selected in (False, True):
            with self.subTest(show_script_errors=selected):
                fake = FakeMCP()
                async def callback(call):
                    return {"read_only_lifecycle": True}
                options = {"show_script_errors": True} if selected else {}
                result = await probe.development_cycle(fake.call, "artifacts/probe", 2,
                                                       probe=callback, **options)
                flags = [args["show_script_errors"] for name, args in fake.calls
                         if name in {"g1r_development_recipe", "g1r_development_start"}]
                self.assertEqual(flags, [selected, selected, selected])
                self.assertTrue(result["stop_confirmed"])
                self.assertEqual(sum(name == "g1r_development_stop" for name, _ in fake.calls), 1)

    async def test_owned_process_already_exited_confirms_empty_without_stop_retry(self):
        fake = FakeMCP()
        async def callback(call):
            fake.stopped = True
            return {"probe_finished": True}
        async def call(name, args):
            if name == "g1r_development_stop":
                fake.calls.append((name, copy.deepcopy(args)))
                raise probe.ProbeError("process_changed", "Исходный процесс уже завершён")
            return await fake.call(name, args)
        result = await probe.development_cycle(call, "artifacts/probe", 2, probe=callback)
        self.assertTrue(result["stop_confirmed"])
        self.assertEqual(result["stop_outcome"], "already_exited")
        self.assertEqual(sum(name == "g1r_development_stop" for name, _ in fake.calls), 1)

    async def test_primary_failure_preserved_when_game_already_exited(self):
        fake = FakeMCP()
        async def callback(call):
            fake.stopped = True
            raise probe.ProbeError("compile_failed", "Компилятор завершил игру")
        async def call(name, args):
            if name == "g1r_development_stop":
                fake.calls.append((name, copy.deepcopy(args)))
                raise probe.ProbeError("process_changed", "Исходный процесс уже завершён")
            return await fake.call(name, args)
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2, probe=callback)
        self.assertEqual(captured.exception.code, "compile_failed")
        self.assertEqual(sum(name == "g1r_development_stop" for name, _ in fake.calls), 1)

    async def test_other_game_after_owned_exit_keeps_cleanup_failure(self):
        fake = FakeMCP()
        replacement = {**fake.process, "pid": 99}
        async def callback(call):
            fake.process = replacement
            return {"probe_finished": True}
        async def call(name, args):
            if name == "g1r_development_stop":
                fake.calls.append((name, copy.deepcopy(args)))
                raise probe.ProbeError("process_changed", "Игра заменена")
            return await fake.call(name, args)
        with self.assertRaises(probe.ProbeError) as captured:
            await probe.development_cycle(call, "artifacts/probe", 2, probe=callback)
        self.assertEqual(captured.exception.code, "process_changed")
        stop = next(args for name, args in fake.calls if name == "g1r_development_stop")
        self.assertEqual(stop["expected_process"]["pid"], 52)

    def test_inner_selection_ignores_bootstrap_and_preserves_timestamp(self):
        fake = FakeMCP()
        bootstrap = {**fake.process, "pid": 41, "executable": "E:/Game/G1R.exe"}
        self.assertEqual(probe.select_inner({"processes": [bootstrap, fake.process]}, "E:/Game"), fake.process)
        duplicate = {**fake.process, "pid": 53}
        with self.assertRaises(probe.ProbeError):
            probe.select_inner({"processes": [fake.process, duplicate]}, "E:/Game")
        with self.assertRaises(probe.ProbeError):
            probe.checked_identity({**fake.process, "creation_time": "2026-10-31T00:00:00Z"})


if __name__ == "__main__":
    unittest.main()
