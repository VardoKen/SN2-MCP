"""Neutral process-helper completion, byte-channel and uncertain-effect checks."""

import base64
import io
import subprocess
import unittest
from unittest.mock import Mock, patch

from sn2_mcp.development import DevelopmentError, execute_command, PROCESS_TIMEOUT_SECONDS


class CommandExecutorTests(unittest.TestCase):
    def test_executor_has_one_deadline_and_no_shell(self):
        process = Mock(stdout=io.BytesIO(b'{"ok":true}'), stderr=io.BytesIO())
        process.wait.return_value = 0
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as run:
            result = execute_command(["fixed", "argument"], PROCESS_TIMEOUT_SECONDS)
        self.assertEqual(result.stdout, '{"ok":true}')
        process.wait.assert_called_once_with(timeout=30)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_executor_timeout_settles_helper_without_replay(self):
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = [subprocess.TimeoutExpired(["fixed"], 30), 0]
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as created:
            with self.assertRaises(DevelopmentError) as captured:
                execute_command(["fixed"], 30)
        self.assertEqual(captured.exception.code, "operation_timeout")
        self.assertTrue(captured.exception.outcome_unknown)
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        created.assert_called_once()

    def test_executor_unavailable_before_start_has_no_external_effect(self):
        with patch("sn2_mcp.development.subprocess.Popen", side_effect=OSError("Neutral unavailable helper")) as created:
            with self.assertRaises(DevelopmentError) as captured:
                execute_command(["fixed"], 30)
        self.assertEqual(captured.exception.code, "tool_unavailable")
        self.assertFalse(captured.exception.outcome_unknown)
        created.assert_called_once()

    def test_executor_preserves_complete_pipe_output(self):
        output = b"x" * (256 * 1024 + 1)
        process = Mock(stdout=io.BytesIO(output), stderr=io.BytesIO())
        process.wait.return_value = 0
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process):
            result = execute_command(["fixed"], 30)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, output.decode("utf-8"))
        self.assertEqual(result.stderr, "")
        process.kill.assert_not_called()

    def test_executor_utf8_failure_preserves_raw_channels_and_actual_exit(self):
        for stdout, stderr, returncode in ((b'{"state":"stopped"}', b"\xef\xf0\xee\xf6\xe5\xf1\xf1", 0),
                                          (b"\xff\xfeinvalid", b"neutral stderr", 2),
                                          (b"\xff", b"\xfe", 7)):
            with self.subTest(returncode=returncode):
                process = Mock(stdout=io.BytesIO(stdout), stderr=io.BytesIO(stderr))
                process.wait.return_value = returncode
                with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as created:
                    with self.assertRaises(DevelopmentError) as captured:
                        execute_command(["fixed"], 30)
                error = captured.exception
                self.assertEqual(error.code, "tool_output_decode_failed")
                self.assertTrue(error.outcome_unknown)
                receipt = error.receipt
                self.assertEqual(receipt["returncode"], returncode)
                self.assertTrue(receipt["helper_completed"])
                self.assertTrue(receipt["output_complete"])
                self.assertEqual(base64.b64decode(receipt["stdout_base64"], validate=True), stdout)
                self.assertEqual(base64.b64decode(receipt["stderr_base64"], validate=True), stderr)
                for name, raw in (("stdout", stdout), ("stderr", stderr)):
                    try:
                        decoded = raw.decode("utf-8")
                    except UnicodeDecodeError as decode_error:
                        detail = next(item for item in receipt["decoding_errors"] if item["stream"] == name)
                        self.assertEqual(detail, {"stream": name, "encoding": "utf-8",
                                                  "start": decode_error.start, "end": decode_error.end,
                                                  "reason": decode_error.reason})
                        self.assertNotIn(name, receipt["decoded_output"])
                    else:
                        self.assertEqual(receipt["decoded_output"][name], decoded)
                created.assert_called_once()
                process.wait.assert_called_once_with(timeout=30)
                process.kill.assert_not_called()

    def test_executor_keyboard_interrupt_settles_helper_without_replay(self):
        process = Mock(stdout=io.BytesIO(), stderr=io.BytesIO())
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch("sn2_mcp.development.subprocess.Popen", return_value=process) as created:
            with self.assertRaises(KeyboardInterrupt) as captured:
                execute_command(["fixed"], 30)
        created.assert_called_once()
        process.kill.assert_called_once()
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(captured.exception.__notes__)


if __name__ == "__main__":
    unittest.main()
