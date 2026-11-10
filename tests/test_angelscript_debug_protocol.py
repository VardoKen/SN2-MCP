"""Автономные проверки wire-формата; живой игровой сервер не используется."""

from pathlib import Path
import socket
import struct
import sys
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

from angelscript_debug_protocol import (
    ProtocolError, ServerFrameDecoder, PayloadReader, encode_client_frame,
    encode_ascii_string, encode_evaluate, decode_evaluate, decode_server_version,
)
from probe_angelscript_debugger import ProbeOptions, run_probe, validate_options, ProbeError


def server_frame(kind, payload=b""):
    return struct.pack("<IB", len(payload), kind) + payload


def string_utf8(value):
    raw = value.encode("utf-8") + b"\0"
    return struct.pack("<i", len(raw)) + raw


class ProtocolTests(unittest.TestCase):
    def test_outgoing_lengths_include_type(self):
        self.assertEqual(encode_client_frame(25), b"\x01\0\0\0\x19")
        self.assertEqual(encode_client_frame(3, struct.pack("<i", 2)), b"\x05\0\0\0\x03\x02\0\0\0")
        expected = b"\x0b\0\0\0\x13\x02\0\0\0" + b"1\0" + b"\xff\xff\xff\xff"
        self.assertEqual(encode_evaluate("1", -1), expected)

    def test_incoming_lengths_exclude_type_and_fragmentation(self):
        wire = server_frame(33, struct.pack("<i", 7)) + server_frame(26)
        decoder = ServerFrameDecoder(4)
        found = []
        for byte in wire:
            found.extend(decoder.feed(bytes([byte])))
        self.assertEqual([(f.message_type, f.payload) for f in found], [(33, struct.pack("<i", 7)), (26, b"")])
        decoder.finish()

    def test_coalesced_frames_larger_than_single_bound(self):
        decoder = ServerFrameDecoder(1)
        frames = decoder.feed(server_frame(2, b"a") * 100)
        self.assertEqual(len(frames), 100)
        self.assertEqual(decoder.pending_bytes, 0)

    def test_payload_limit_rejected_from_header_without_payload(self):
        decoder = ServerFrameDecoder(8)
        with self.assertRaises(ProtocolError):
            decoder.feed(struct.pack("<IB", 9, 2))
        self.assertLessEqual(decoder.pending_bytes, 5)

    def test_truncated_header_and_payload(self):
        for wire in (b"\x04\0", server_frame(33, b"abcd")[:-1]):
            decoder = ServerFrameDecoder(8)
            decoder.feed(wire)
            with self.assertRaises(ProtocolError):
                decoder.finish()

    def test_utf8_utf16_and_optional_nul(self):
        text = "Мир 😀"
        utf16 = (text + "\0").encode("utf-16le")
        wire = string_utf8(text) + struct.pack("<i", -(len(utf16) // 2)) + utf16 + struct.pack("<i", 1) + b"x"
        reader = PayloadReader(wire)
        self.assertEqual([reader.read_string() for _ in range(3)], [text, text, "x"])
        reader.finish()

    def test_malformed_strings_and_trailing_data(self):
        for wire in (struct.pack("<i", 8) + b"a", struct.pack("<i", -2) + b"a\0", struct.pack("<i", 1) + b"\xff", struct.pack("<i", -0x80000000)):
            with self.assertRaises(ProtocolError):
                PayloadReader(wire).read_string()
        with self.assertRaises(ProtocolError):
            decode_server_version(struct.pack("<i", 2) + b"x")

    def test_eval_schema_and_ascii_only_writer(self):
        payload = b"".join(string_utf8(x) for x in ("1", "1", "int")) + struct.pack("<i", 0)
        self.assertEqual(decode_evaluate(payload), {"name": "1", "value": "1", "type": "int", "has_members": False})
        for text in ("Мир", "a\0b"):
            with self.assertRaises(ProtocolError):
                encode_ascii_string(text)

    def test_evaluate_is_explicit_literal_and_frame(self):
        for options in (ProbeOptions(evaluate="1"), ProbeOptions(evaluate="Func()", frame_id=0, start_debugging=True), ProbeOptions(evaluate="obj.x", frame_id=0, start_debugging=True)):
            with self.assertRaises(ProbeError):
                validate_options(options)
        validate_options(ProbeOptions(evaluate="1", frame_id=-1, start_debugging=True))

    def test_invalid_arguments_do_not_connect(self):
        attempted = []
        result = run_probe(ProbeOptions(max_frame_bytes=0x100000000), connector=lambda *a, **k: attempted.append(a))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "invalid_arguments")
        self.assertEqual(attempted, [])


class TransportTests(unittest.TestCase):
    def test_database_requested_once_without_start_debugging(self):
        client, server = socket.socketpair()
        received = []

        def fake_server():
            try:
                server.sendall(server_frame(33, struct.pack("<i", 2)))
                request = b""
                while len(request) < 5:
                    request += server.recv(5 - len(request))
                received.append(request)
                server.sendall(server_frame(2, b"private data") + server_frame(26))
                while chunk := server.recv(64):
                    received.append(chunk)
            finally:
                server.close()

        worker = threading.Thread(target=fake_server)
        worker.start()
        result = run_probe(ProbeOptions(timeout=2, request_database=True), connector=lambda *a, **k: client)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result["ok"])
        self.assertEqual(result["sent_messages"], [1, 25])
        self.assertEqual(result["database_chunks"], 1)
        self.assertEqual(result["database_bytes"], 12)
        self.assertNotIn("private data", repr(result))
        self.assertEqual(b"".join(received), b"\x01\0\0\0\x01\x01\0\0\0\x19")

    def test_passive_probe_sends_only_disconnect(self):
        client, server = socket.socketpair()
        received = []

        def fake_server():
            try:
                server.sendall(server_frame(33, struct.pack("<i", 4)))
                while chunk := server.recv(64):
                    received.append(chunk)
            finally:
                server.close()

        worker = threading.Thread(target=fake_server)
        worker.start()
        result = run_probe(ProbeOptions(timeout=1), connector=lambda *a, **k: client)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(result["ok"])
        self.assertEqual(result["server_version"], 4)
        self.assertEqual(b"".join(received), b"\x01\0\0\0\x19")

    def test_timeout_does_not_repeat_evaluate(self):
        client, server = socket.socketpair()
        received = []

        def fake_server():
            try:
                server.sendall(server_frame(33, struct.pack("<i", 2)))
                while chunk := server.recv(64):
                    received.append(chunk)
            finally:
                server.close()

        worker = threading.Thread(target=fake_server)
        worker.start()
        result = run_probe(ProbeOptions(timeout=0.05, start_debugging=True, evaluate="1", frame_id=0), connector=lambda *a, **k: client)
        worker.join(2)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "timeout")
        self.assertEqual(result["sent_messages"], [3, 19, 25])
        expected = b"\x05\0\0\0\x03\x02\0\0\0" + encode_evaluate("1", 0) + b"\x01\0\0\0\x19"
        self.assertEqual(b"".join(received), expected)

    def test_partial_frame_at_timeout_is_protocol_error(self):
        client, server = socket.socketpair()
        server.sendall(b"\x04\0\0")
        try:
            result = run_probe(ProbeOptions(timeout=0.02), connector=lambda *a, **k: client)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "protocol_error")
        finally:
            server.close()

    def test_oversized_response_is_structured_failure(self):
        client, server = socket.socketpair()
        server.sendall(struct.pack("<IB", 100, 2))
        try:
            result = run_probe(ProbeOptions(timeout=0.1, max_frame_bytes=8), connector=lambda *a, **k: client)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"]["code"], "protocol_error")
            self.assertEqual(result["sent_messages"], [25])
        finally:
            server.close()


if __name__ == "__main__":
    unittest.main()
