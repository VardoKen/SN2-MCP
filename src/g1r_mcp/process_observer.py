"""Observe the original process through a retained Win32 HANDLE without changing the game."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timedelta
import ntpath
import os
import re
import threading
from typing import Any


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
PROCESS_OBSERVER_ACCESS = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
_TICKS_PER_SECOND = 10_000_000
_EPOCH = datetime(1601, 1, 1)
_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d{7})Z$")
_MAX_PATH_CHARS = 32768


class ObservationError(Exception):
    def __init__(self, code: str, message: str, win32_error: int | None = None):
        super().__init__(message)
        self.code = code
        self.win32_error = win32_error


def _creation_ticks(value: Any) -> int:
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        raise ObservationError("invalid_process_identity", "An exact UTC creation timestamp with seven fractional second digits is required")
    try:
        moment = datetime.strptime(match[1], "%Y-%m-%dT%H:%M:%S")
        delta = moment - _EPOCH
        ticks = (delta.days * 86400 + delta.seconds) * _TICKS_PER_SECOND + int(match[2])
    except (ValueError, OverflowError):
        raise ObservationError("invalid_process_identity", "Invalid process creation time") from None
    if not 0 < ticks <= 0x7FFFFFFFFFFFFFFF:
        raise ObservationError("invalid_process_identity", "The creation time is outside the FILETIME range")
    return ticks


def _timestamp(ticks: int) -> str:
    if not 0 < ticks <= 0x7FFFFFFFFFFFFFFF:
        raise ObservationError("process_observation_unavailable", "Invalid process time in FILETIME")
    seconds, remainder = divmod(ticks, _TICKS_PER_SECOND)
    try:
        moment = _EPOCH + timedelta(seconds=seconds)
    except (ValueError, OverflowError):
        raise ObservationError("process_observation_unavailable", "FILETIME is outside the UTC timestamp range") from None
    return f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{remainder:07d}Z"


def _filetime(value: wintypes.FILETIME) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _comparable_path(value: str) -> str:
    value = value.replace("/", "\\")
    if value.lower().startswith("\\\\?\\unc\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return ntpath.normcase(ntpath.normpath(value))


def _expected_identity(value: Any) -> tuple[dict, int]:
    if not isinstance(value, dict) or type(value.get("pid")) is not int or not 1 <= value["pid"] <= 0xFFFFFFFF:
        raise ObservationError("invalid_process_identity", "The original process PID is required")
    executable = value.get("executable")
    if (not isinstance(executable, str) or not executable or len(executable) >= _MAX_PATH_CHARS
            or any(ord(character) < 32 for character in executable)
            or not ntpath.isabs(executable) or not ntpath.splitdrive(executable)[0]):
        raise ObservationError("invalid_process_identity", "An absolute full EXE path is required")
    created = _creation_ticks(value.get("creation_time"))
    return {"pid": value["pid"], "creation_time": value["creation_time"], "executable": executable}, created


class _Kernel32:
    def __init__(self, api: Any = None):
        if api is None:
            if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
                raise ObservationError("process_observer_unavailable", "The observer requires Windows x64")
            api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api = api
        signatures = {
            "OpenProcess": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "GetProcessId": ([wintypes.HANDLE], wintypes.DWORD),
            "GetProcessTimes": ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            "QueryFullProcessImageNameW": ([wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            "GetExitCodeProcess": ([wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)], wintypes.BOOL),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        try:
            for name, (arguments, result) in signatures.items():
                function = getattr(api, name)
                function.argtypes = arguments
                function.restype = result
        except (AttributeError, TypeError):
            raise ObservationError("process_observer_unavailable", "The Win32 observation API is unavailable") from None

    def error(self) -> int:
        reader = getattr(self.api, "get_last_error", None)
        return int(reader()) if reader is not None else int(ctypes.get_last_error())

    def times(self, handle: int) -> tuple[int, int]:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not self.api.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)):
            raise ObservationError("process_observation_unavailable", "Could not read the original process times", self.error())
        return _filetime(created), _filetime(exited)

    def identity(self, handle: int) -> tuple[int, int, str]:
        pid = int(self.api.GetProcessId(handle))
        if pid == 0:
            raise ObservationError("process_observation_unavailable", "Could not read the PID through the HANDLE", self.error())
        created, _ = self.times(handle)
        length = wintypes.DWORD(_MAX_PATH_CHARS)
        name = ctypes.create_unicode_buffer(_MAX_PATH_CHARS)
        if (not self.api.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(length))
                or not 0 < length.value < _MAX_PATH_CHARS):
            raise ObservationError("process_observation_unavailable", "Could not read the original process full path", self.error())
        return pid, created, name[:length.value]


class ProcessLease:
    def __init__(self, kernel: _Kernel32, handle: int, process: dict, creation_ticks: int):
        self._kernel = kernel
        self._handle = handle
        self._process = dict(process)
        self._creation_ticks = creation_ticks
        self._lock = threading.Lock()
        self._terminal: dict | None = None

    def _result(self, state: str, **details: Any) -> dict:
        return {"state": state, "process": dict(self._process), "source": "win32_process_handle", **details}

    def poll(self) -> dict:
        """One nonblocking snapshot; determine running state with Wait(0), not exit code 259."""
        with self._lock:
            if self._handle is None:
                return self._result("observation_unknown", code="process_handle_closed")
            if self._terminal is not None:
                return {**self._terminal, "process": dict(self._process)}
            waited = int(self._kernel.api.WaitForSingleObject(self._handle, 0))
            if waited == WAIT_TIMEOUT:
                return self._result("running")
            if waited != WAIT_OBJECT_0:
                return self._result("observation_unknown", code="process_wait_failed",
                                    win32_error=self._kernel.error() if waited == WAIT_FAILED else None,
                                    wait_result=waited)
            exit_code = wintypes.DWORD()
            if not self._kernel.api.GetExitCodeProcess(self._handle, ctypes.byref(exit_code)):
                return self._result("observation_unknown", code="process_exit_code_unavailable", win32_error=self._kernel.error())
            try:
                created, exited = self._kernel.times(self._handle)
                if created != self._creation_ticks:
                    return self._result("observation_unknown", code="process_identity_changed")
                exited_at = _timestamp(exited)
            except ObservationError as error:
                return self._result("observation_unknown", code=error.code, win32_error=error.win32_error)
            self._terminal = self._result("exited", exit_code=int(exit_code.value), exit_time=exited_at)
            return {**self._terminal, "process": dict(self._process)}

    def close(self) -> None:
        """Release only the HANDLE; do not terminate the process or send requests to the game."""
        with self._lock:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            if not self._kernel.api.CloseHandle(handle):
                raise ObservationError("process_handle_close_failed", "Could not release the observation HANDLE", self._kernel.error())

    def __enter__(self) -> "ProcessLease":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def capture_checked_process(expected_identity: dict, *, api: Any = None) -> ProcessLease:
    """Retain the exact original process; disappearance before capture is not a compiler diagnosis."""
    expected, created = _expected_identity(expected_identity)
    kernel = _Kernel32(api)
    handle = kernel.api.OpenProcess(PROCESS_OBSERVER_ACCESS, False, expected["pid"])
    if handle in (None, 0, -1, ctypes.c_void_p(-1).value):
        raise ObservationError("process_capture_unavailable", "Could not retain the original process for observation", kernel.error())
    try:
        pid, actual_created, executable = kernel.identity(handle)
        if pid != expected["pid"] or actual_created != created or _comparable_path(executable) != _comparable_path(expected["executable"]):
            raise ObservationError("process_identity_changed", "The PID, creation time, or full EXE path changed")
        return ProcessLease(kernel, handle, expected, created)
    except BaseException as error:
        if not kernel.api.CloseHandle(handle):
            error.add_note("Could not release the observation HANDLE after a capture error")
        raise
