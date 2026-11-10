"""Изолированные TCP-проверки нативного клиента, без запуска игры."""

import json
import socket
import struct
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from g1r_mcp.native_client import MAX_FRAME_BYTES, NativeClient, NativeError


def receive(stream, count):
    result = bytearray()
    while len(result) < count:
        fragment = stream.recv(count - len(result))
        if not fragment:
            raise EOFError("Клиент закрыл соединение")
        result.extend(fragment)
    return bytes(result)


def request(stream):
    length = struct.unpack(">I", receive(stream, 4))[0]
    return json.loads(receive(stream, length).decode("utf-8"))


def frame(value):
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return struct.pack(">I", len(payload)) + payload


def response(value, result):
    return {"protocol": 1, "id": value["id"], "type": "response", "result": result}


class FakeServer:
    def __init__(self, handler):
        self.handler = handler
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.listener.listen(4)
        self.listener.settimeout(0.1)
        self.stop = threading.Event()
        self.errors = []
        self.connections = []
        self.accepted = 0
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                stream, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.accepted += 1
            self.connections.append(stream)
            stream.settimeout(1)
            try:
                self.handler(stream)
            except Exception as error:
                if not self.stop.is_set():
                    self.errors.append(error)
            finally:
                stream.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.stop.set()
        self.listener.close()
        for stream in self.connections:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            stream.close()
        self.thread.join(2)
        if self.thread.is_alive() and exc_type is None:
            raise AssertionError("Изолированный сервер не завершился")
        if self.errors and exc_type is None:
            raise self.errors[0]


class NativeClientTests(unittest.TestCase):
    def test_confirmed_error_effects_distinguish_validation_from_result_failure(self):
        cases = [
            ("incompatible_object_argument", {"executed": False, "may_have_executed": False, "do_not_retry": False}),
            ("game_result_error", {"executed": True, "may_have_executed": True,
                                   "do_not_retry": True, "cause_code": "stale_handle"}),
        ]
        for code, details in cases:
            with self.subTest(code=code):
                def handler(stream):
                    value = request(stream)
                    stream.sendall(frame({"protocol": 1, "id": value["id"], "type": "error",
                                          "error": {"code": code, "message": "Проверка", "details": details}}))
                with FakeServer(handler) as server, NativeClient(port=server.port) as client:
                    with self.assertRaises(NativeError) as caught:
                        client.request("invoke")
                    self.assertEqual(caught.exception.details, details)
                    self.assertEqual(caught.exception.may_have_executed, details["may_have_executed"])
                    self.assertEqual(server.accepted, 1)

    def test_invalid_error_effect_metadata_is_protocol_failure(self):
        def handler(stream):
            value = request(stream)
            stream.sendall(frame({"protocol": 1, "id": value["id"], "type": "error",
                                  "error": {"code": "failed", "message": "Ошибка",
                                            "details": {"executed": "false"}}}))
        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.request("invoke")
            self.assertEqual(caught.exception.code, "native_protocol_error")
            self.assertTrue(caught.exception.may_have_executed)

    def test_incomplete_error_effects_remain_uncertain_and_do_not_replay(self):
        cases = [
            ({}, True),
            ({"cause_code": "result_processing_failed"}, True),
            ({"executed": False}, True),
            ({"may_have_executed": False}, True),
            ({"executed": False, "do_not_retry": False}, True),
            ({"executed": False, "may_have_executed": True, "do_not_retry": True}, True),
            ({"executed": True, "may_have_executed": False}, True),
            ({"executed": False, "may_have_executed": False}, False),
        ]
        for details, uncertain in cases:
            with self.subTest(details=details):
                received = []

                def handler(stream):
                    value = request(stream)
                    received.append(value)
                    stream.sendall(frame({"protocol": 1, "id": value["id"], "type": "error",
                                          "error": {"code": "invoke_failed", "message": "Ошибка обработчика",
                                                    "details": details}}))
                    status = request(stream)
                    received.append(status)
                    stream.sendall(frame(response(status, {"ready": True})))

                with FakeServer(handler) as server, NativeClient(port=server.port) as client:
                    with self.assertRaises(NativeError) as caught:
                        client.request("invoke")
                    self.assertIs(caught.exception.may_have_executed, uncertain)
                    self.assertEqual(caught.exception.details, details)
                    self.assertEqual(caught.exception.request_id, received[0]["id"])
                    self.assertEqual(client.status(), {"ready": True})
                    self.assertEqual(server.accepted, 1)
                self.assertEqual([entry["op"] for entry in received], ["invoke", "status"])
                self.assertNotEqual(received[0]["id"], received[1]["id"])

    def test_persistent_unique_requests_and_fragmented_unicode_reply(self):
        received = []
        operation = "x" * 65

        def handler(stream):
            for _ in range(2):
                value = request(stream)
                received.append(value)
                wire = frame(response(value, {"text": "Мир 😀", "op": value["op"]}))
                for start in range(0, len(wire), 3):
                    stream.sendall(wire[start:start + 3])

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            self.assertEqual(client.request(operation, {"text": "Жизнь"}, timeout=6)["text"], "Мир 😀")
            self.assertEqual(client.status()["op"], "status")
            self.assertEqual(server.accepted, 1)
        self.assertNotEqual(received[0]["id"], received[1]["id"])
        self.assertEqual(received[0]["protocol"], 1)
        self.assertEqual(received[0]["op"], operation)
        self.assertEqual(received[0]["params"], {"text": "Жизнь"})
        self.assertGreater(received[0]["wait_timeout_ms"], 0)
        self.assertLessEqual(received[0]["wait_timeout_ms"], 6_000)
        self.assertGreater(received[1]["wait_timeout_ms"], 0)
        self.assertLessEqual(received[1]["wait_timeout_ms"], 5_000)

    def test_request_and_echo_larger_than_one_mib_keep_connection_without_retry(self):
        received = []
        text = "Мир 🐉\n" * 100_000
        self.assertGreater(len(text.encode("utf-8")), 1024 * 1024)
        self.assertEqual(MAX_FRAME_BYTES, 0xFFFFFFFF)

        def handler(stream):
            value = request(stream)
            received.append(value)
            wire = frame(response(value, {"text": value["params"]["text"], "executed": True}))
            self.assertGreater(len(wire) - 4, 1024 * 1024)
            stream.sendall(wire)
            following = request(stream)
            received.append(following)
            stream.sendall(frame(response(following, {"ready": True, "accepted_requests": len(received)})))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            echoed = client.request("invoke", {"text": text})
            self.assertEqual(echoed, {"text": text, "executed": True})
            self.assertEqual(client.status(), {"ready": True, "accepted_requests": 2})
            self.assertEqual(server.accepted, 1)
        self.assertEqual([value["op"] for value in received], ["invoke", "status"])
        self.assertEqual(received[0]["params"]["text"].encode("utf-8"), text.encode("utf-8"))
        self.assertEqual(len({value["id"] for value in received}), 2)

    def test_timeout_after_send_does_not_retry_and_drops_connection(self):
        received = []
        ended = threading.Event()

        def handler(stream):
            received.append(request(stream))
            self.assertEqual(stream.recv(1), b"")
            ended.set()

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.request("action", {}, timeout=0.1)
            self.assertEqual(caught.exception.code, "native_timeout")
            self.assertTrue(caught.exception.may_have_executed)
            self.assertIsNotNone(caught.exception.request_id)
            self.assertTrue(ended.wait(1))
            self.assertEqual(len(received), 1)
            self.assertEqual(server.accepted, 1)

    def test_late_reply_after_timeout_cannot_satisfy_next_request(self):
        accepted = threading.Event()
        release_late = threading.Event()
        attempted_late = threading.Event()
        received = []

        def handler(stream):
            value = request(stream)
            received.append(value)
            if value["op"] == "invoke":
                accepted.set()
                self.assertTrue(release_late.wait(1))
                try:
                    stream.sendall(frame(response(value, {"executed": True, "late_marker": "old-action"})))
                except OSError:
                    # TCP может уже заметить shutdown клиента; поздний результат всё равно не принимается.
                    pass
                finally:
                    attempted_late.set()
            else:
                stream.sendall(frame(response(value, {"ready": True, "fresh_marker": "new-status"})))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.request("invoke", {"action": "isolated"}, timeout=0.1)
            self.assertTrue(accepted.is_set())
            self.assertEqual(caught.exception.code, "native_timeout")
            self.assertTrue(caught.exception.may_have_executed)
            old_id = caught.exception.request_id
            release_late.set()
            result = client.status(timeout=1)
            self.assertTrue(attempted_late.wait(1))
            self.assertEqual(result, {"ready": True, "fresh_marker": "new-status"})
            self.assertEqual(server.accepted, 2)
        self.assertEqual([entry["op"] for entry in received], ["invoke", "status"])
        self.assertEqual(received[0]["id"], old_id)
        self.assertNotEqual(received[0]["id"], received[1]["id"])

    def test_eof_after_acceptance_is_uncertain(self):
        with FakeServer(lambda stream: request(stream)) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_disconnected")
            self.assertTrue(caught.exception.may_have_executed)

    def test_wrong_id_disconnects_without_following_other_response(self):
        def handler(stream):
            value = request(stream)
            wire = frame({"protocol": 1, "id": "another", "type": "response", "result": {}})
            wire += frame(response(value, {"wrong": "must not be accepted"}))
            stream.sendall(wire)

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_protocol_error")
            self.assertTrue(caught.exception.may_have_executed)

    def test_zero_length_header_rejected_without_receiving_body(self):
        def handler(stream):
            request(stream)
            stream.sendall(struct.pack(">I", 0))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_protocol_error")

    def test_malformed_reply_variants(self):
        makers = (
            lambda value: b"\xff",
            lambda value: b'{"protocol":1,"protocol":1}',
            lambda value: json.dumps({"protocol": True, "id": value["id"], "type": "response", "result": {}}).encode(),
            lambda value: json.dumps({"protocol": 1, "id": value["id"], "type": "response", "result": []}).encode(),
            lambda value: b"[1,2]",
            lambda value: ('{"protocol":1,"id":"' + value["id"] + '","type":"response","result":{"x":NaN}}').encode(),
        )
        for make in makers:
            with self.subTest(make=make):
                def handler(stream):
                    payload = make(request(stream))
                    stream.sendall(struct.pack(">I", len(payload)) + payload)

                with FakeServer(handler) as server, NativeClient(port=server.port) as client:
                    with self.assertRaises(NativeError) as caught:
                        client.status()
                    self.assertEqual(caught.exception.code, "native_protocol_error")

    def test_structured_error_keeps_connection_and_does_not_invent_no_effects(self):
        code = "world_not_ready_" + "x" * 129
        message = "Мир не готов 😀\n" + "x" * 10_011
        def handler(stream):
            value = request(stream)
            stream.sendall(frame({"protocol": 1, "id": value["id"], "type": "error",
                                  "error": {"code": code, "message": message}}))
            value = request(stream)
            stream.sendall(frame(response(value, {"ready": True})))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            with self.assertRaises(NativeError) as caught:
                client.request("read")
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.message, message)
            self.assertTrue(caught.exception.may_have_executed)
            self.assertEqual(client.status(), {"ready": True})
            self.assertEqual(server.accepted, 1)

    def test_connection_refused_is_not_execution(self):
        with NativeClient(timeout=0.3) as client, patch("g1r_mcp.native_client.socket.socket") as opened:
            opened.return_value.connect.side_effect = ConnectionRefusedError()
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_unavailable")
            self.assertFalse(caught.exception.may_have_executed)
            opened.return_value.sendall.assert_not_called()

    def test_connect_timeout_has_no_execution_or_send(self):
        with NativeClient(timeout=0.1) as client, patch("g1r_mcp.native_client.socket.socket") as opened:
            opened.return_value.connect.side_effect = socket.timeout()
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_timeout")
            self.assertFalse(caught.exception.may_have_executed)
            opened.return_value.sendall.assert_not_called()
            opened.return_value.close.assert_called_once()

    def test_invalid_options_and_payload_do_not_open_socket(self):
        for values in ({"host": "example.com"}, {"host": "192.0.2.1"}, {"port": True},
                       {"port": 80}, {"timeout": float("inf")}, {"timeout": False}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                NativeClient(**values)
        with NativeClient(timeout=6) as client, patch("g1r_mcp.native_client.socket.socket") as opened:
            self.assertEqual(client.timeout, 6)
            for operation, params in (("", {}), ("\ud800", {}), ("x", [])):
                with self.subTest(operation=operation[:10]), self.assertRaises(ValueError):
                    client.request(operation, params)
            with self.assertRaises(NativeError) as caught:
                client.request("x", {"n": float("nan")})
            self.assertEqual(caught.exception.code, "invalid_arguments")
            self.assertFalse(caught.exception.may_have_executed)
            self.assertEqual(caught.exception.details["stage"], "serialization_not_sent")
            opened.assert_not_called()

    def test_close_interrupts_pending_response_and_prevents_new_connection(self):
        received = threading.Event()
        failures = []

        def handler(stream):
            request(stream)
            received.set()
            stream.recv(1)

        with FakeServer(handler) as server:
            client = NativeClient(port=server.port)
            def waiting():
                try:
                    client.request("action")
                except NativeError as error:
                    failures.append(error)
            worker = threading.Thread(target=waiting)
            worker.start()
            self.assertTrue(received.wait(1))
            client.close()
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertTrue(failures[0].may_have_executed)
            with self.assertRaises(NativeError) as caught:
                client.status()
            self.assertEqual(caught.exception.code, "native_closed")
            self.assertFalse(caught.exception.may_have_executed)

    def test_waiting_request_timeout_does_not_cancel_current_request(self):
        received = threading.Event()
        release = threading.Event()
        results = []

        def handler(stream):
            value = request(stream)
            received.set()
            self.assertTrue(release.wait(1))
            stream.sendall(frame(response(value, {"done": True})))

        with FakeServer(handler) as server, NativeClient(port=server.port) as client:
            worker = threading.Thread(target=lambda: results.append(client.request("first")))
            worker.start()
            self.assertTrue(received.wait(1))
            with self.assertRaises(NativeError) as caught:
                client.request("second", timeout=0.05)
            self.assertEqual(caught.exception.code, "native_busy")
            self.assertFalse(caught.exception.may_have_executed)
            release.set()
            worker.join(1)
            self.assertEqual(results, [{"done": True}])
            self.assertEqual(server.accepted, 1)


if __name__ == "__main__":
    unittest.main()
