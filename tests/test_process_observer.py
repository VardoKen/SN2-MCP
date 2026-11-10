"""Win32 observer: только внедрённый fake API, без запуска или открытия настоящих процессов."""
import ctypes
from ctypes import wintypes
import unittest

from sn2_mcp.process_observer import (ObservationError, PROCESS_OBSERVER_ACCESS, WAIT_FAILED,
                                      WAIT_OBJECT_0, WAIT_TIMEOUT, capture_checked_process)


CREATED = 134379234001234567
EXITED = CREATED + 20_000_003
IDENTITY = {"pid": 123, "creation_time": "2026-10-31T12:30:00.1234567Z",
            "executable": r"E:\SteamLibrary\G1R\G1R-Win64-Shipping.exe"}


class FakeFunction:
    def __init__(self, function):
        self.function = function
        self.argtypes = None
        self.restype = None

    def __call__(self, *arguments):
        return self.function(*arguments)


class FakeKernel:
    def __init__(self):
        self.handle = 0x123456789AB
        self.pid = IDENTITY["pid"]
        self.created = CREATED
        self.exited = EXITED
        self.path = IDENTITY["executable"]
        self.wait_result = WAIT_TIMEOUT
        self.exit_code = 0
        self.failed = None
        self.calls = []
        self.error = 5
        for name in ("OpenProcess", "GetProcessId", "GetProcessTimes", "QueryFullProcessImageNameW",
                     "WaitForSingleObject", "GetExitCodeProcess", "CloseHandle"):
            setattr(self, name, FakeFunction(getattr(self, "_" + name)))

    def get_last_error(self): return self.error

    def _record(self, name, arguments):
        self.calls.append((name, arguments))
        return self.failed != name

    def _OpenProcess(self, access, inherit, pid):
        return self.handle if self._record("OpenProcess", (access, inherit, pid)) else 0

    def _GetProcessId(self, handle):
        return self.pid if self._record("GetProcessId", (handle,)) else 0

    def _GetProcessTimes(self, handle, created, exited, kernel, user):
        if not self._record("GetProcessTimes", (handle,)): return 0
        for pointer, value in ((created, self.created), (exited, self.exited), (kernel, 0), (user, 0)):
            target = ctypes.cast(pointer, ctypes.POINTER(wintypes.FILETIME)).contents
            target.dwLowDateTime = value & 0xFFFFFFFF
            target.dwHighDateTime = value >> 32
        return 1

    def _QueryFullProcessImageNameW(self, handle, flags, buffer, length):
        count = ctypes.cast(length, ctypes.POINTER(wintypes.DWORD)).contents
        if not self._record("QueryFullProcessImageNameW", (handle, flags, count.value)): return 0
        buffer.value = self.path
        count.value = len(self.path)
        return 1

    def _WaitForSingleObject(self, handle, timeout):
        self._record("WaitForSingleObject", (handle, timeout))
        return self.wait_result

    def _GetExitCodeProcess(self, handle, result):
        if not self._record("GetExitCodeProcess", (handle,)): return 0
        ctypes.cast(result, ctypes.POINTER(wintypes.DWORD)).contents.value = self.exit_code
        return 1

    def _CloseHandle(self, handle):
        return int(self._record("CloseHandle", (handle,)))


class ProcessObserverTests(unittest.TestCase):
    def test_exact_abi_readonly_access_and_64bit_handle(self):
        api = FakeKernel()
        lease = capture_checked_process(IDENTITY, api=api)
        self.assertEqual(api.calls[0], ("OpenProcess", (0x00101000, False, 123)))
        self.assertEqual(PROCESS_OBSERVER_ACCESS, 0x00101000)
        self.assertEqual(api.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(api.OpenProcess.argtypes, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
        self.assertEqual(api.GetProcessTimes.argtypes, [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4)
        self.assertEqual(api.QueryFullProcessImageNameW.argtypes,
                         [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)])
        self.assertEqual(api.WaitForSingleObject.argtypes, [wintypes.HANDLE, wintypes.DWORD])
        self.assertEqual(api.GetExitCodeProcess.argtypes, [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)])
        self.assertEqual(api.CloseHandle.argtypes, [wintypes.HANDLE])
        self.assertTrue(all(call[1][0] == api.handle for call in api.calls[1:]))
        self.assertEqual(lease.poll()["state"], "running")
        self.assertEqual(api.calls[-1], ("WaitForSingleObject", (api.handle, 0)))
        self.assertFalse(any(name == "GetExitCodeProcess" for name, _ in api.calls))
        lease.close(); lease.close()
        self.assertEqual(sum(name == "CloseHandle" for name, _ in api.calls), 1)
        self.assertEqual(lease.poll()["code"], "process_handle_closed")

    def test_signaled_exit_259_is_terminal_and_filetime_preserves_seventh_digit(self):
        api = FakeKernel(); api.wait_result = WAIT_OBJECT_0; api.exit_code = 259
        lease = capture_checked_process(IDENTITY, api=api)
        result = lease.poll()
        self.assertEqual(result["state"], "exited")
        self.assertEqual(result["exit_code"], 259)
        self.assertEqual(result["exit_time"], "2026-10-31T12:30:02.1234570Z")
        self.assertEqual(result["process"], IDENTITY)
        api.wait_result = WAIT_TIMEOUT
        self.assertEqual(lease.poll(), result)
        self.assertEqual(sum(name == "WaitForSingleObject" for name, _ in api.calls), 1)
        lease.close()

    def test_capture_identity_failures_close_same_handle_once(self):
        for field, value in (("pid", 124), ("created", CREATED + 1), ("path", r"E:\Elsewhere\game.exe")):
            with self.subTest(field=field):
                api = FakeKernel(); setattr(api, field, value)
                with self.assertRaises(ObservationError) as caught: capture_checked_process(IDENTITY, api=api)
                self.assertEqual(caught.exception.code, "process_identity_changed")
                self.assertEqual([call for call in api.calls if call[0] == "CloseHandle"], [("CloseHandle", (api.handle,))])
        for failed in ("GetProcessId", "GetProcessTimes", "QueryFullProcessImageNameW"):
            api = FakeKernel(); api.failed = failed
            with self.assertRaises(ObservationError): capture_checked_process(IDENTITY, api=api)
            self.assertEqual(sum(name == "CloseHandle" for name, _ in api.calls), 1)

    def test_case_and_long_path_prefix_match_without_reopening_by_pid(self):
        api = FakeKernel(); api.path = "\\\\?\\" + IDENTITY["executable"].upper()
        with capture_checked_process(IDENTITY, api=api) as lease:
            self.assertEqual(lease.poll()["process"], IDENTITY)
        self.assertEqual(sum(name == "OpenProcess" for name, _ in api.calls), 1)

    def test_capture_missed_before_observation_never_infers_compile_failure(self):
        api = FakeKernel(); api.failed = "OpenProcess"; api.error = 87
        with self.assertRaises(ObservationError) as caught: capture_checked_process(IDENTITY, api=api)
        self.assertEqual(caught.exception.code, "process_capture_unavailable")
        self.assertEqual(caught.exception.win32_error, 87)
        self.assertEqual([name for name, _ in api.calls], ["OpenProcess"])

    def test_unknown_wait_exit_or_times_does_not_report_process_failure(self):
        for wait in (WAIT_FAILED, 128):
            api = FakeKernel(); api.wait_result = wait
            with capture_checked_process(IDENTITY, api=api) as lease:
                result = lease.poll()
                self.assertEqual(result["state"], "observation_unknown")
                self.assertEqual(result["wait_result"], wait)
                self.assertNotIn("exit_code", result)
        for failed in ("GetExitCodeProcess", "GetProcessTimes"):
            api = FakeKernel(); lease = capture_checked_process(IDENTITY, api=api)
            api.wait_result = WAIT_OBJECT_0; api.failed = failed
            self.assertEqual(lease.poll()["state"], "observation_unknown")
            lease.close()
        api = FakeKernel(); lease = capture_checked_process(IDENTITY, api=api)
        api.wait_result = WAIT_OBJECT_0; api.created += 1
        self.assertEqual(lease.poll()["code"], "process_identity_changed")
        api.created = CREATED; api.exited = 0
        self.assertEqual(lease.poll()["state"], "observation_unknown")
        lease.close()

    def test_invalid_identity_is_rejected_before_any_win32_call(self):
        values = [{**IDENTITY, "pid": True}, {**IDENTITY, "pid": 0}, {**IDENTITY, "pid": 0x100000000},
                  {**IDENTITY, "creation_time": "2026-10-31T12:30:00.123456Z"},
                  {**IDENTITY, "creation_time": "2026-02-31T12:30:00.1234567Z"},
                  {**IDENTITY, "executable": r"\relative.exe"}, {**IDENTITY, "executable": "E:\\game\x00.exe"}]
        for identity in values:
            api = FakeKernel()
            with self.subTest(identity=identity), self.assertRaises(ObservationError): capture_checked_process(identity, api=api)
            self.assertEqual(api.calls, [])

    def test_close_failure_has_no_retry_or_process_side_effect(self):
        api = FakeKernel(); lease = capture_checked_process(IDENTITY, api=api); api.failed = "CloseHandle"
        with self.assertRaises(ObservationError) as caught: lease.close()
        self.assertEqual(caught.exception.code, "process_handle_close_failed")
        lease.close()
        self.assertEqual(sum(name == "CloseHandle" for name, _ in api.calls), 1)
