"""Bounded codec for the AngelScript debugger diagnostic protocol.

Independent implementation based on the open Hazelight client format, MIT,
Copyright 2019 Hazelight Games AB; source code was not copied.
Revision: ffc7ab2948589abb04200b7dcedd326eaeaf7a4d.
https://github.com/Hazelight/vscode-unreal-angelscript/blob/ffc7ab2948589abb04200b7dcedd326eaeaf7a4d/extension/src/unreal-debugclient.ts
https://github.com/Hazelight/vscode-unreal-angelscript/blob/ffc7ab2948589abb04200b7dcedd326eaeaf7a4d/extension/src/debug.ts

Direction headers differ: client length includes type,
while server length includes only payload. G1R compatibility requires a separate probe.
"""

from dataclasses import dataclass
from enum import IntEnum
import struct


class ProtocolError(ValueError):
    """A message violates the validated format or diagnostic limit."""


class MessageType(IntEnum):
    DIAGNOSTICS = 0
    REQUEST_DEBUG_DATABASE = 1
    DEBUG_DATABASE = 2
    START_DEBUGGING = 3
    STOP_DEBUGGING = 4
    PAUSE = 5
    CONTINUE = 6
    REQUEST_CALLSTACK = 7
    CALLSTACK = 8
    CLEAR_BREAKPOINTS = 9
    SET_BREAKPOINT = 10
    HAS_STOPPED = 11
    CONTINUED = 12
    STEP_OVER = 13
    STEP_IN = 14
    STEP_OUT = 15
    REQUEST_VARIABLES = 17
    VARIABLES = 18
    REQUEST_EVALUATE = 19
    EVALUATE = 20
    DISCONNECT = 25
    DEBUG_DATABASE_FINISHED = 26
    DEBUG_SERVER_VERSION = 33


@dataclass(frozen=True)
class Frame:
    """Server message without assumptions about unknown payload."""

    message_type: int
    payload: bytes


def encode_client_frame(message_type: int, payload: bytes = b"") -> bytes:
    """Build an outgoing frame: length includes the one-byte type."""
    if not 0 <= message_type <= 255:
        raise ProtocolError("Message type must fit in one byte")
    if len(payload) > 0xFFFFFFFE:
        raise ProtocolError("Outgoing payload exceeds the uint32 format")
    return struct.pack("<IB", len(payload) + 1, message_type) + payload


def encode_ascii_string(value: str) -> bytes:
    """Encode a confirmed outgoing ASCII FString with a trailing NUL."""
    if "\0" in value:
        raise ProtocolError("Outgoing string contains an embedded NUL")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as error:
        raise ProtocolError("Outgoing Unicode format is not confirmed; ASCII is supported") from error
    if len(encoded) >= 0x7FFFFFFF:
        raise ProtocolError("Outgoing string exceeds the int32 format")
    return struct.pack("<i", len(encoded) + 1) + encoded + b"\0"


def encode_string(value: str) -> bytes:
    """Encode a protocol FString, preserving Unicode with UTF-16 units."""
    if "\0" in value:
        raise ProtocolError("Outgoing string contains an embedded NUL")
    if value.isascii():
        return encode_ascii_string(value)
    encoded = value.encode("utf-16le")
    return struct.pack("<i", -(len(encoded) // 2 + 1)) + encoded + b"\0\0"


def encode_evaluate(expression: str, frame_id: int) -> bytes:
    """Build one Evaluate request; frame_id has no confirmed default."""
    if not -0x80000000 <= frame_id <= 0x7FFFFFFF:
        raise ProtocolError("frame_id must fit in int32")
    return encode_client_frame(
        MessageType.REQUEST_EVALUATE,
        encode_string(expression) + struct.pack("<i", frame_id),
    )


class ServerFrameDecoder:
    """Incrementally read server frames with a limit for each payload."""

    def __init__(self, max_payload_bytes: int):
        if not 1 <= max_payload_bytes <= 0xFFFFFFFF:
            raise ValueError("Payload limit must be a positive uint32")
        self.max_payload_bytes = max_payload_bytes
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> list[Frame]:
        """Consume a fragment; multiple complete frames may follow one another."""
        frames = []
        offset = 0
        while offset < len(data):
            if len(self._buffer) < 5:
                needed = 5 - len(self._buffer)
            else:
                payload_length = struct.unpack_from("<I", self._buffer)[0]
                if payload_length > self.max_payload_bytes:
                    raise ProtocolError("Server payload exceeds the diagnostic limit")
                needed = 5 + payload_length - len(self._buffer)
            take = min(needed, len(data) - offset)
            self._buffer.extend(data[offset:offset + take])
            offset += take
            if len(self._buffer) < 5:
                continue
            payload_length, message_type = struct.unpack_from("<IB", self._buffer)
            if payload_length > self.max_payload_bytes:
                raise ProtocolError("Server payload exceeds the diagnostic limit")
            if len(self._buffer) == 5 + payload_length:
                frames.append(Frame(message_type, bytes(self._buffer[5:])))
                self._buffer.clear()
        return frames

    def finish(self) -> None:
        """Confirm that no incomplete frame remains on close or timeout."""
        if self._buffer:
            raise ProtocolError("An incomplete server frame was received")


class PayloadReader:
    """Validated int32 and FString values inside an already bounded payload."""

    def __init__(self, payload: bytes):
        self.payload = payload
        self.offset = 0

    def _take(self, count: int) -> bytes:
        if count > len(self.payload) - self.offset:
            raise ProtocolError("Payload is truncated while reading a value")
        value = self.payload[self.offset:self.offset + count]
        self.offset += count
        return value

    def read_int(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def read_string(self) -> str:
        count = self.read_int()
        if count == -0x80000000:
            raise ProtocolError("Invalid FString length")
        raw = self._take(-count * 2 if count < 0 else count)
        try:
            value = raw.decode("utf-16le" if count < 0 else "utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ProtocolError("FString contains invalid encoding") from error
        # Upstream removes a trailing NUL but does not require it to be present.
        return value[:-1] if value.endswith("\0") else value

    def finish(self) -> None:
        if self.offset != len(self.payload):
            raise ProtocolError("Unexpected bytes remain in a known payload")


def decode_server_version(payload: bytes) -> int:
    reader = PayloadReader(payload)
    version = reader.read_int()
    reader.finish()
    return version


def decode_evaluate(payload: bytes, *, allow_trailing: bool = False) -> dict:
    """Read the adapter's evaluation prefix; preserve optional server extensions.

    The official adapter's receiveEvaluate reads three strings and a bool, then
    ignores the remaining frame payload. Extension bytes have no confirmed
    semantics and are returned verbatim when that compatibility is requested.
    """
    reader = PayloadReader(payload)
    result = {
        "name": reader.read_string(),
        "value": reader.read_string(),
        "type": reader.read_string(),
        "has_members": reader.read_int() != 0,
    }
    if allow_trailing and reader.offset < len(payload):
        result["additional_payload_hex"] = payload[reader.offset:].hex()
    else:
        reader.finish()
    return result
