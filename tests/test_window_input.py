"""Адресные Win32-сообщения: только fake API и lease, без настоящих окон/процессов."""

import ctypes
from ctypes import wintypes
import unittest

from sn2_mcp.process_observer import ObservationError
from sn2_mcp.window_input import MAPVK_VK_TO_VSC_EX, WM_KEYDOWN, WM_KEYUP, WindowInputError, queue_key


IDENTITY = {"pid": 123, "creation_time": "2026-11-01T10:00:00.1234567Z",
            "executable": r"E:\SteamLibrary\G1R\G1R-Win64-Shipping.exe"}
WINDOW = 0x123456789AB


class Function:
    def __init__(self, function):
        self.function = function

    def __call__(self, *arguments):
        return self.function(*arguments)


class FakeWindows:
    def __init__(self):
        self.windows = {WINDOW: (123, 456, True), 44: (999, 777, True), 45: (123, 888, False)}
        self.calls = []
        self.fail_message = None
        self.scan = 0x1C
        self.after_post = None
        for name in ("EnumWindows", "GetWindowThreadProcessId", "IsWindow", "IsWindowVisible",
                     "MapVirtualKeyW", "PostMessageW"):
            setattr(self, name, Function(getattr(self, "_" + name)))

    def get_last_error(self):
        return 5

    def _EnumWindows(self, callback, parameter):
        self.calls.append(("EnumWindows",))
        for window in list(self.windows):
            callback(window, parameter)
        return True

    def _GetWindowThreadProcessId(self, window, pid):
        self.calls.append(("owner", window))
        owner, thread, _ = self.windows.get(window, (0, 0, False))
        ctypes.cast(pid, ctypes.POINTER(wintypes.DWORD)).contents.value = owner
        return thread

    def _IsWindow(self, window):
        self.calls.append(("IsWindow", window))
        return window in self.windows

    def _IsWindowVisible(self, window):
        return self.windows[window][2]

    def _MapVirtualKeyW(self, key, mode):
        self.calls.append(("map", key, mode))
        return self.scan

    def _PostMessageW(self, window, message, key, bits):
        self.calls.append(("post", window, message, key, bits))
        if self.after_post is not None:
            self.after_post(message)
        return message != self.fail_message


class FakeLease:
    def __init__(self, calls):
        self.calls = calls
        self.polls = 0
        self.stop_at = None
        self.close_error = False

    def poll(self):
        self.polls += 1
        self.calls.append(("poll", self.polls))
        return {"state": "exited" if self.stop_at == self.polls else "running", "process": IDENTITY}

    def close(self):
        self.calls.append(("close",))
        if self.close_error:
            raise ObservationError("process_handle_close_failed", "Закрытие HANDLE не подтверждено", 6)


class WindowInputTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeWindows()
        self.lease = FakeLease(self.api.calls)

    def run_key(self, key=13, **options):
        def factory(identity):
            self.assertEqual(identity, IDENTITY)
            self.api.calls.append(("capture",))
            return self.lease
        return queue_key(IDENTITY, key, api=self.api, lease_factory=factory, **options)

    def test_exact_pair_and_order_use_only_selected_window_and_close_lease(self):
        result = self.run_key()
        self.assertEqual(result["status"], "input_queued")
        self.assertEqual(result["process"], IDENTITY)
        self.assertEqual((result["selected_hwnd"], result["window_thread_id"]), (WINDOW, 456))
        self.assertFalse(result["handling_confirmed"])
        self.assertFalse(result["outcome_unknown"])
        self.assertEqual([item["message_id"] for item in result["messages"]], [WM_KEYDOWN, WM_KEYUP])
        self.assertEqual([item["lparam"] for item in result["messages"]],
                         [0x001C0001, 0xC01C0001])
        posts = [call for call in self.api.calls if call[0] == "post"]
        self.assertEqual(posts, [("post", WINDOW, WM_KEYDOWN, 13, 0x001C0001),
                                 ("post", WINDOW, WM_KEYUP, 13, 0xC01C0001)])
        self.assertEqual(self.api.calls[0], ("capture",))
        self.assertEqual(self.api.calls[-1], ("close",))
        self.assertEqual(self.lease.polls, 6)
        for index, call in enumerate(self.api.calls):
            if call[0] == "post":
                self.assertEqual(self.api.calls[index - 1], ("owner", WINDOW))
                self.assertEqual(self.api.calls[index - 2], ("IsWindow", WINDOW))
                self.assertEqual(self.api.calls[index - 3][0], "poll")
                self.assertEqual(self.api.calls[index + 1][0], "poll")
        self.assertEqual(self.api.PostMessageW.argtypes[0], wintypes.HWND)
        self.assertIn(("map", 13, MAPVK_VK_TO_VSC_EX), self.api.calls)

    def test_second_message_failure_retains_queued_down_and_is_not_retried(self):
        self.api.fail_message = WM_KEYUP
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        error = caught.exception
        self.assertEqual(error.code, "window_message_failed")
        self.assertTrue(error.outcome_unknown)
        self.assertTrue(error.do_not_retry)
        self.assertEqual([item["queued"] for item in error.receipt["messages"]], [True, False])
        self.assertEqual(error.receipt["messages"][1]["win32_error"], 5)
        self.assertEqual(sum(call[0] == "post" for call in self.api.calls), 2)
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_failed_first_message_has_known_failure_and_no_keyup(self):
        self.api.fail_message = WM_KEYDOWN
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertEqual(len(caught.exception.receipt["messages"]), 1)
        self.assertEqual(sum(call[0] == "post" for call in self.api.calls), 1)
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_ambiguous_visible_windows_require_explicit_selection(self):
        other = WINDOW + 1
        self.api.windows[other] = (123, 457, True)
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        self.assertEqual(caught.exception.code, "ambiguous_window")
        self.assertEqual(caught.exception.receipt["available_windows"], [WINDOW, other])
        self.assertFalse(any(call[0] == "post" for call in self.api.calls))
        self.api.calls.clear()
        result = self.run_key(expected_window=other)
        self.assertEqual(result["selected_hwnd"], other)
        self.assertTrue(all(call[1] == other for call in self.api.calls if call[0] == "post"))

    def test_foreign_explicit_window_is_never_posted_to(self):
        with self.assertRaises(WindowInputError) as caught:
            self.run_key(expected_window=44)
        self.assertEqual(caught.exception.code, "window_not_found")
        self.assertFalse(any(call[0] == "post" for call in self.api.calls))
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_window_owner_changes_after_down_prevent_keyup(self):
        self.api.after_post = lambda _message: self.api.windows.update({WINDOW: (999, 456, True)})
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        self.assertEqual(caught.exception.code, "window_identity_changed")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertEqual(sum(call[0] == "post" for call in self.api.calls), 1)
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_exited_lease_after_discovery_prevents_any_message(self):
        self.lease.stop_at = 2
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        self.assertEqual(caught.exception.code, "process_not_running")
        self.assertFalse(caught.exception.outcome_unknown)
        self.assertFalse(any(call[0] == "post" for call in self.api.calls))
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_capture_refuses_reused_pid_before_window_api(self):
        def refuse(_identity):
            raise ObservationError("process_identity_changed", "PID соответствует другому EXE или FILETIME")
        with self.assertRaises(WindowInputError) as caught:
            queue_key(IDENTITY, 13, api=self.api, lease_factory=refuse)
        self.assertEqual(caught.exception.code, "process_identity_changed")
        self.assertEqual(self.api.calls, [])

    def test_close_failure_after_pair_preserves_unknown_outcome(self):
        self.lease.close_error = True
        with self.assertRaises(WindowInputError) as caught:
            self.run_key()
        self.assertEqual(caught.exception.code, "process_handle_close_failed")
        self.assertTrue(caught.exception.outcome_unknown)
        self.assertTrue(all(item["queued"] for item in caught.exception.receipt["messages"]))
        self.assertEqual(self.api.calls[-1], ("close",))

    def test_virtual_key_exact_range_and_extended_scan_are_preserved(self):
        for key in (True, False, -1, 256, 1.0, "13", None):
            with self.subTest(key=key), self.assertRaises(WindowInputError):
                self.run_key(key)
        self.assertEqual(self.api.calls, [])
        self.api.scan = 0xE01D
        result = self.run_key(255)
        self.assertEqual([item["lparam"] for item in result["messages"]], [0x011D0001, 0xC11D0001])
        self.api.scan = 0
        self.assertEqual(self.run_key(0)["virtual_key"], 0)


if __name__ == "__main__":
    unittest.main()
