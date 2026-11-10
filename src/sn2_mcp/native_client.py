"""Sequential local native channel client without command retries."""

import ipaddress
import json
import math
import socket
import struct
import threading
import time
from typing import Any
import uuid


PROTOCOL_VERSION = 1
DEFAULT_PORT = 27223
MAX_FRAME_BYTES = 0xFFFFFFFF
MAX_TIMEOUT = 5.0


class NativeError(Exception):
    """Channel error; a lost response does not prove absence of effects."""

    def __init__(self, code: str, message: str, may_have_executed: bool = False,
                 request_id: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.may_have_executed = may_have_executed
        self.request_id = request_id
        self.details = dict(details or {})


def _timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Timeout must be a number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("Timeout must be finite and greater than zero")
    return value


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _reject_constant(value: str):
    raise ValueError("Invalid JSON constant")


class NativeClient:
    """One persistent connection, one in-flight request, and a shared deadline."""

    def __init__(self, host: str = "127.0.0.1", port: int = DEFAULT_PORT,
                 timeout: float = MAX_TIMEOUT):
        if host == "localhost":
            host = "127.0.0.1"
        try:
            address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            raise ValueError("Only a numeric loopback address or localhost is allowed") from None
        if not address.is_loopback:
            raise ValueError("The native channel is allowed only on a loopback address")
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("Port must be an integer between 1024 and 65535")
        self.host = str(address)
        self.port = port
        self.timeout = _timeout(timeout)
        self._family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        self._request_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._socket: socket.socket | None = None
        self._closed = False

    def _remaining(self, deadline: float, request_id: str, sent: bool) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeError("native_timeout", "Timed out waiting for the native response", sent, request_id)
        return remaining

    def _closed_error(self, request_id: str, sent: bool) -> NativeError:
        return NativeError("native_closed", "The native channel client is closed", sent, request_id)

    def _connect(self, deadline: float, request_id: str) -> socket.socket:
        with self._state_lock:
            if self._closed:
                raise self._closed_error(request_id, False)
            if self._socket is not None:
                return self._socket
            stream = socket.socket(self._family, socket.SOCK_STREAM)
            self._socket = stream
        try:
            stream.settimeout(self._remaining(deadline, request_id, False))
            stream.connect((self.host, self.port))
            with self._state_lock:
                if self._closed:
                    raise self._closed_error(request_id, False)
            return stream
        except NativeError:
            self._drop(stream)
            raise
        except TimeoutError:
            self._drop(stream)
            with self._state_lock:
                if self._closed:
                    raise self._closed_error(request_id, False) from None
            raise NativeError("native_timeout", "Timed out connecting to the native channel", False, request_id) from None
        except OSError:
            self._drop(stream)
            with self._state_lock:
                if self._closed:
                    raise self._closed_error(request_id, False) from None
            raise NativeError("native_unavailable", "The native channel is unavailable", False, request_id) from None

    def _drop(self, stream: socket.socket):
        with self._state_lock:
            if self._socket is stream:
                self._socket = None
        try:
            stream.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        stream.close()

    def _receive(self, stream: socket.socket, size: int, deadline: float,
                 request_id: str) -> bytes:
        result = bytearray()
        while len(result) < size:
            stream.settimeout(self._remaining(deadline, request_id, True))
            fragment = stream.recv(size - len(result))
            if not fragment:
                raise NativeError("native_disconnected", "The native channel closed before a response was received", True, request_id)
            result.extend(fragment)
        return bytes(result)

    def _decode(self, payload: bytes, request_id: str) -> dict[str, Any]:
        try:
            response = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_object,
                                  parse_constant=_reject_constant)
        except (UnicodeError, ValueError, RecursionError):
            raise NativeError("native_protocol_error", "The native response contains invalid UTF-8 JSON", True, request_id) from None
        if not isinstance(response, dict) or type(response.get("protocol")) is not int or response["protocol"] != PROTOCOL_VERSION:
            raise NativeError("native_protocol_error", "Invalid native response version or shape", True, request_id)
        if response.get("id") != request_id:
            raise NativeError("native_protocol_error", "The native response belongs to another request", True, request_id)
        if response.get("type") == "response" and isinstance(response.get("result"), dict):
            return response["result"]
        if response.get("type") == "error":
            error = response.get("error")
            if (isinstance(error, dict) and isinstance(error.get("code"), str)
                    and error["code"] and isinstance(error.get("message"), str)):
                # The format does not guarantee absence of effects after a handler error.
                details = error.get("details")
                if details is not None:
                    if not isinstance(details, dict) or any(
                        key in details and type(details[key]) is not bool
                        for key in ("executed", "may_have_executed", "do_not_retry")
                    ) or ("cause_code" in details and not isinstance(details["cause_code"], str)):
                        raise NativeError("native_protocol_error", "Invalid command effect information", True, request_id)
                    # A missing field does not prove absence of effects.
                    # Only two explicit false values permit a definite rejection before execution.
                    may_have_executed = not (details.get("executed") is False
                                             and details.get("may_have_executed") is False)
                else:
                    may_have_executed = True
                raise NativeError(error["code"], error["message"], may_have_executed, request_id, details)
        raise NativeError("native_protocol_error", "The native response contains neither a result nor a valid error", True, request_id)

    def request(self, op: str, params: dict[str, Any] | None = None,
                *, timeout: float | None = None) -> dict[str, Any]:
        """Send one command. A retry after failure requires a new explicit call."""
        if not isinstance(op, str) or not op or any(ord(char) < 32 for char in op):
            raise ValueError("The operation name must be nonempty UTF-8 text without control characters")
        try:
            op.encode("utf-8")
        except UnicodeError:
            raise ValueError("The operation name cannot be represented as UTF-8") from None
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise ValueError("Operation parameters must be an object")
        duration = self.timeout if timeout is None else _timeout(timeout)
        request_id = uuid.uuid4().hex
        try:
            payload = json.dumps({"protocol": PROTOCOL_VERSION, "id": request_id, "op": op, "params": params},
                                 ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise NativeError("invalid_arguments", "Operation parameters cannot be represented as valid JSON",
                              False, request_id,
                              {"executed": False, "may_have_executed": False, "do_not_retry": False,
                               "request_started": False, "stage": "serialization_not_sent"}) from None
        if len(payload) > MAX_FRAME_BYTES:
            raise ValueError("The request length cannot be represented by a four-byte frame header")
        deadline = time.monotonic() + duration
        if not self._request_lock.acquire(timeout=duration):
            raise NativeError("native_busy", "Timed out waiting for the previous native request", False, request_id)
        stream: socket.socket | None = None
        sent = False
        try:
            self._remaining(deadline, request_id, False)
            stream = self._connect(deadline, request_id)
            try:
                wait_timeout = json.dumps(self._remaining(deadline, request_id, False) * 1000,
                                          allow_nan=False).encode("utf-8")
                payload = payload[:-1] + b',"wait_timeout_ms":' + wait_timeout + b"}"
            except (ValueError, TypeError, UnicodeError, RecursionError):
                raise NativeError("invalid_arguments", "Operation parameters cannot be represented as valid JSON",
                                  False, request_id,
                                  {"executed": False, "may_have_executed": False, "do_not_retry": False,
                                   "request_started": False, "stage": "serialization_not_sent"}) from None
            if len(payload) > MAX_FRAME_BYTES:
                raise ValueError("The request length cannot be represented by a four-byte frame header")
            stream.settimeout(self._remaining(deadline, request_id, False))
            # Even a partial sendall cannot guarantee absence of execution.
            sent = True
            stream.sendall(struct.pack(">I", len(payload)) + payload)
            length = struct.unpack(">I", self._receive(stream, 4, deadline, request_id))[0]
            if not 0 < length <= MAX_FRAME_BYTES:
                raise NativeError("native_protocol_error", "The native frame has an invalid size", True, request_id)
            return self._decode(self._receive(stream, length, deadline, request_id), request_id)
        except NativeError as error:
            if stream is not None and error.code in {"native_timeout", "native_disconnected", "native_protocol_error", "native_closed"}:
                self._drop(stream)
            raise
        except TimeoutError:
            if stream is not None:
                self._drop(stream)
            raise NativeError("native_timeout", "Timed out waiting for the native response", sent, request_id) from None
        except OSError:
            if stream is not None:
                self._drop(stream)
            with self._state_lock:
                closed = self._closed
            if closed:
                raise self._closed_error(request_id, sent) from None
            raise NativeError("native_disconnected", "The native channel was disconnected", sent, request_id) from None
        finally:
            self._request_lock.release()

    def status(self, *, timeout: float | None = None) -> dict[str, Any]:
        """Request the current status without using a readiness cache."""
        return self.request("status", {}, timeout=timeout)

    def close(self):
        """Terminate the connection, including any pending response, and close the client."""
        with self._state_lock:
            self._closed = True
            stream = self._socket
            self._socket = None
        if stream is not None:
            self._drop(stream)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
