"""One targeted window message pair; queueing does not prove processing."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from typing import Any, Callable

from .process_observer import ObservationError, capture_checked_process


WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
MAPVK_VK_TO_VSC_EX = 4
_WindowCallback = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class WindowInputError(Exception):
    def __init__(self, code: str, message: str, receipt: dict):
        super().__init__(message)
        self.code = code
        self.receipt = receipt
        self.outcome_unknown = receipt["outcome_unknown"]
        self.do_not_retry = receipt["do_not_retry"]


class _User32:
    def __init__(self, api: Any = None):
        if api is None:
            if os.name != "nt":
                raise OSError("Window input requires Windows")
            api = ctypes.WinDLL("user32", use_last_error=True)
        self.api = api
        signatures = {
            "EnumWindows": ([_WindowCallback, wintypes.LPARAM], wintypes.BOOL),
            "GetWindowThreadProcessId": ([wintypes.HWND, ctypes.POINTER(wintypes.DWORD)], wintypes.DWORD),
            "IsWindow": ([wintypes.HWND], wintypes.BOOL),
            "IsWindowVisible": ([wintypes.HWND], wintypes.BOOL),
            "MapVirtualKeyW": ([wintypes.UINT, wintypes.UINT], wintypes.UINT),
            "PostMessageW": ([wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes = arguments
            function.restype = result

    def error(self) -> int:
        reader = getattr(self.api, "get_last_error", None)
        return int(reader()) if reader is not None else int(ctypes.get_last_error())

    def owner(self, window: int) -> tuple[int, int]:
        pid = wintypes.DWORD()
        thread = int(self.api.GetWindowThreadProcessId(window, ctypes.byref(pid)))
        return thread, int(pid.value)


def queue_key(expected_process: dict, virtual_key: int, *, expected_window: int | None = None,
              api: Any = None, lease_factory: Callable | None = None) -> dict:
    receipt = {"status": "input_queue_failed", "process": dict(expected_process),
               "selected_hwnd": None, "window_thread_id": None, "virtual_key": virtual_key,
               "messages": [], "handling_confirmed": False, "outcome_unknown": False,
               "do_not_retry": False}

    def failure(code: str, message: str, **details: Any) -> WindowInputError:
        queued = any(item["queued"] for item in receipt["messages"])
        receipt.update(status="input_queue_failed", outcome_unknown=queued, do_not_retry=queued)
        receipt["error"] = {"code": code, "message": message, **details}
        return WindowInputError(code, message, receipt)

    if type(virtual_key) is not int or not 0 <= virtual_key <= 255:
        raise failure("invalid_virtual_key", "virtual_key must be an integer between 0 and 255")
    if expected_window is not None and (type(expected_window) is not int or expected_window <= 0
            or expected_window >= 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
        raise failure("invalid_window", "expected_window must be a positive HWND")

    lease = None
    error = None
    try:
        lease = (lease_factory or capture_checked_process)(expected_process)
        user = _User32(api)

        def running() -> None:
            observed = lease.poll()
            if observed["state"] != "running":
                raise failure("process_not_running", "The original process has not been confirmed as running",
                              process_observation=observed)

        running()
        windows = []

        @_WindowCallback
        def collect(window, _parameter):
            window = int(window or 0)
            thread, pid = user.owner(window)
            if thread and pid == expected_process["pid"] and user.api.IsWindowVisible(window):
                windows.append((window, thread))
            return True

        if not user.api.EnumWindows(collect, 0):
            raise failure("window_enumeration_failed", "Could not enumerate windows",
                          win32_error=user.error())
        running()
        available = [window for window, _ in windows]
        if expected_window is not None:
            selected = next((item for item in windows if item[0] == expected_window), None)
            if selected is None:
                receipt["available_windows"] = available
                raise failure("window_not_found", "The selected visible window does not belong to the original process")
        elif len(windows) != 1:
            receipt["available_windows"] = available
            raise failure("ambiguous_window" if windows else "window_not_found",
                          "Input requires one selected visible window of the original process")
        else:
            selected = windows[0]
        window, thread = selected
        receipt.update(selected_hwnd=window, window_thread_id=thread)
        scan = int(user.api.MapVirtualKeyW(virtual_key, MAPVK_VK_TO_VSC_EX))
        down = 1 | ((scan & 0xFF) << 16) | (int((scan & 0xFF00) in (0xE000, 0xE100)) << 24)
        for message, label, bits in ((WM_KEYDOWN, "WM_KEYDOWN", down),
                                     (WM_KEYUP, "WM_KEYUP", down | (1 << 30) | (1 << 31))):
            running()
            if not user.api.IsWindow(window) or user.owner(window) != (thread, expected_process["pid"]):
                raise failure("window_identity_changed", "The selected window identity changed")
            posted = bool(user.api.PostMessageW(window, message, virtual_key, bits))
            item = {"message": label, "message_id": message, "wparam": virtual_key,
                    "lparam": bits, "queued": posted}
            receipt["messages"].append(item)
            if not posted:
                item["win32_error"] = user.error()
                raise failure("window_message_failed", "Could not queue the window message",
                              win32_error=item["win32_error"])
            running()
        receipt["status"] = "input_queued"
    except WindowInputError as caught:
        error = caught
    except ObservationError as caught:
        error = failure(caught.code, str(caught), win32_error=caught.win32_error)
    except (OSError, AttributeError):
        error = failure("window_input_unavailable", "The Win32 window input API is unavailable")
    finally:
        if lease is not None:
            try:
                lease.close()
            except ObservationError as caught:
                if error is None:
                    error = failure(caught.code, str(caught), win32_error=caught.win32_error)
                else:
                    receipt["cleanup_error"] = {"code": caught.code, "message": str(caught),
                                                "win32_error": caught.win32_error}
    if error is not None:
        raise error
    return receipt
