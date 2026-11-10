"""One shared connection to the official AngelScript debugger.

Wire formats follow Hazelight's open debugger adapter at revision
ffc7ab2948589abb04200b7dcedd326eaeaf7a4d. Query requests have no wire identifier;
the request lock and connection closure on reply timeout preserve FIFO matching.
Breakpoint messages have explicit identifiers and asynchronous verification.
"""

from __future__ import annotations

from collections import defaultdict, deque
import socket
import struct
import threading
import time
from typing import Any

from .angelscript_debug_protocol import (
    Frame,
    MessageType,
    PayloadReader,
    ServerFrameDecoder,
    decode_evaluate,
    decode_server_version,
    encode_client_frame,
    encode_string,
)


class DebugSessionError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _error_text(error: Exception) -> str:
    if isinstance(error, OSError):
        return (f"Debugger socket operation failed (errno={error.errno}, "
                f"winerror={getattr(error, 'winerror', None)})")
    return str(error)


class DebugSession:
    def __init__(self, host: str = "127.0.0.1", port: int = 27099):
        self.host = host
        self.port = port
        self._request_lock = threading.RLock()
        self._condition = threading.Condition(threading.RLock())
        self._socket: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._replies: dict[int, deque[Frame]] = defaultdict(deque)
        self._server_version = 0
        self._paused: bool | None = None
        self._event_sequence = 0
        self._events: deque[dict[str, Any]] = deque()
        self._last_event: dict[str, Any] | None = None
        self._last_error: dict[str, Any] | None = None
        self._next_breakpoint_id = 1
        self._breakpoints: dict[int, dict[str, Any]] = {}

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {
                "host": self.host,
                "port": self.port,
                "connected": self._socket is not None,
                "paused": self._paused,
                "server_version": self._server_version,
                "event_sequence": self._event_sequence,
                "last_event": dict(self._last_event) if self._last_event else None,
                "last_error": dict(self._last_error) if self._last_error else None,
            }

    def attach(self, timeout: float = 5.0) -> dict[str, Any]:
        with self._request_lock:
            with self._condition:
                if self._socket is not None:
                    return {**self.status(), "sent": False, "confirmed": False, "reused": True}
            self._join_reader()
            try:
                connection = socket.create_connection((self.host, self.port), timeout=timeout)
                connection.settimeout(0.5)
            except OSError as exc:
                error = DebugSessionError("connect_failed", _error_text(exc))
                self._record_error(error)
                raise error from exc
            with self._condition:
                self._socket = connection
                self._server_version = 0
                self._paused = None
                self._last_error = None
                self._last_event = None
                self._events.clear()
                self._replies.clear()
                self._breakpoints.clear()
                self._reader = threading.Thread(
                    target=self._read_loop,
                    args=(connection,),
                    name="g1r-angelscript-debugger",
                    daemon=True,
                )
                self._reader.start()
            self._send(MessageType.START_DEBUGGING, struct.pack("<i", 2))
            return {**self.status(), "sent": True, "confirmed": False, "reused": False}

    def detach(self) -> dict[str, Any]:
        with self._request_lock:
            before = self.status()
            sent = False
            resume_requested = False
            error: DebugSessionError | None = None
            if before["connected"]:
                try:
                    if before["paused"]:
                        self._send(MessageType.CONTINUE)
                        resume_requested = True
                    self._send(MessageType.STOP_DEBUGGING)
                    self._send(MessageType.DISCONNECT)
                    sent = True
                except DebugSessionError as exc:
                    error = exc
            self._close_connection(error, resume_paused=not resume_requested)
            self._join_reader()
            return {
                **self.status(),
                "sent": sent,
                "confirmed": False,
                "resume_requested": resume_requested,
            }

    def breakpoints(
        self, path: str, lines: list[int], module: str = "", timeout: float = 5.0
    ) -> dict[str, Any]:
        with self._request_lock:
            self._send(MessageType.CLEAR_BREAKPOINTS, encode_string(path) + encode_string(module))
            identifiers: list[int] = []
            with self._condition:
                self._breakpoints = {
                    identifier: breakpoint
                    for identifier, breakpoint in self._breakpoints.items()
                    if (breakpoint["requested_path"], breakpoint["module"]) != (path, module)
                }
            for line in lines:
                identifier = self._next_breakpoint_id
                self._next_breakpoint_id += 1
                payload = (
                    encode_string(path)
                    + struct.pack("<ii", line, identifier)
                    + encode_string(module)
                )
                with self._condition:
                    self._breakpoints[identifier] = {
                        "id": identifier,
                        "requested_path": path,
                        "requested_line": line,
                        "path": path,
                        "line": line,
                        "module": module,
                        "confirmed": False,
                        "verified": False,
                    }
                identifiers.append(identifier)
                self._send(MessageType.SET_BREAKPOINT, payload)
            results = self._wait_breakpoints(identifiers, timeout)
            return {
                "path": path,
                "module": module,
                "sent": True,
                "confirmed": bool(results) and all(breakpoint["confirmed"] for breakpoint in results),
                "breakpoints": results,
                "status": self.status(),
            }

    def control(self, action: str, timeout: float = 5.0) -> dict[str, Any]:
        actions = {
            "pause": MessageType.PAUSE,
            "continue": MessageType.CONTINUE,
            "step_over": MessageType.STEP_OVER,
            "step_in": MessageType.STEP_IN,
            "step_out": MessageType.STEP_OUT,
        }
        if action not in actions:
            raise DebugSessionError("invalid_action", "Unknown debugger control action", {"action": action})
        with self._request_lock:
            with self._condition:
                sequence = self._event_sequence
            self._send(actions[action])
            expected = "continued" if action == "continue" else "stopped"
            event = self._wait_event(expected, sequence, timeout)
            return {
                "action": action,
                "sent": True,
                "confirmed": event is not None,
                "event": event,
                "status": self.status(),
            }

    def stack(self, timeout: float = 5.0) -> dict[str, Any]:
        with self._request_lock:
            self._send(MessageType.REQUEST_CALLSTACK)
            frame = self._wait_reply(MessageType.CALLSTACK, timeout)
            reader = PayloadReader(frame.payload)
            count = reader.read_int()
            frames: list[dict[str, Any]] = []
            version = self.status()["server_version"]
            for identifier in range(count):
                name = reader.read_string()
                path = reader.read_string()
                line = reader.read_int()
                module = reader.read_string() if version > 0 else ""
                frames.append({"id": identifier, "name": name, "path": path, "line": line, "module": module})
            reader.finish()
            return {"sent": True, "confirmed": True, "frames": frames, "total_count": count}

    def variables(self, expression: str, timeout: float = 5.0) -> dict[str, Any]:
        with self._request_lock:
            self._send(MessageType.REQUEST_VARIABLES, encode_string(expression))
            frame = self._wait_reply(MessageType.VARIABLES, timeout)
            reader = PayloadReader(frame.payload)
            count = reader.read_int()
            variables: list[dict[str, Any]] = []
            version = self.status()["server_version"]
            for _ in range(count):
                variable = {
                    "name": reader.read_string(),
                    "value": reader.read_string(),
                    "type": reader.read_string(),
                    "has_members": reader.read_int() != 0,
                }
                if version >= 2:
                    address, value_size = struct.unpack("<Qb", reader._take(9))
                    variable.update(address=address, value_size=value_size)
                variables.append(variable)
            reader.finish()
            return {
                "sent": True,
                "confirmed": True,
                "expression": expression,
                "variables": variables,
                "total_count": count,
            }

    def evaluate(self, expression: str, frame_id: int, timeout: float = 5.0) -> dict[str, Any]:
        with self._request_lock:
            payload = encode_string(expression) + struct.pack("<i", frame_id)
            self._send(MessageType.REQUEST_EVALUATE, payload)
            frame = self._wait_reply(MessageType.EVALUATE, timeout)
            return {
                "sent": True,
                "confirmed": True,
                "expression": expression,
                "frame_id": frame_id,
                **decode_evaluate(frame.payload, allow_trailing=True),
            }

    def _send(self, message_type: int, payload: bytes = b"") -> None:
        with self._condition:
            connection = self._socket
        if connection is None:
            raise DebugSessionError("not_connected", "The debugger session is not connected", self.status())
        try:
            connection.sendall(encode_client_frame(message_type, payload))
        except OSError as exc:
            error = DebugSessionError("send_failed", _error_text(exc), {"message_type": int(message_type)})
            self._close_connection(error, connection)
            raise error from exc

    def _wait_reply(self, message_type: int, timeout: float) -> Frame:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self._replies[message_type]:
                    return self._replies[message_type].popleft()
                if self._socket is None:
                    raise DebugSessionError("connection_closed", "The debugger connection closed while waiting for a reply", self.status())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    error = DebugSessionError(
                        "reply_timeout", "Timed out waiting for the debugger reply",
                        {"message_type": int(message_type), "timeout": timeout},
                    )
                    self._close_connection(error)
                    raise error
                self._condition.wait(remaining)

    def _wait_breakpoints(self, identifiers: list[int], timeout: float) -> list[dict[str, Any]]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                results = [dict(self._breakpoints[identifier]) for identifier in identifiers]
                if all(breakpoint["confirmed"] for breakpoint in results):
                    return results
                if self._socket is None:
                    raise DebugSessionError("connection_closed", "The debugger connection closed while setting breakpoints", self.status())
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return results
                self._condition.wait(remaining)

    def _wait_event(self, kind: str, after_sequence: int, timeout: float) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for event in self._events:
                    if event["sequence"] > after_sequence and event["kind"] == kind:
                        return dict(event)
                if self._socket is None:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _read_loop(self, connection: socket.socket) -> None:
        decoder = ServerFrameDecoder(0xFFFFFFFF)
        try:
            while True:
                with self._condition:
                    if self._socket is not connection:
                        return
                try:
                    data = connection.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    decoder.finish()
                    raise DebugSessionError("connection_closed", "The debugger peer closed the connection")
                for frame in decoder.feed(data):
                    self._receive(frame, connection)
        except Exception as exc:
            error = exc if isinstance(exc, DebugSessionError) else DebugSessionError("receive_failed", _error_text(exc))
            self._close_connection(error, connection)

    def _receive(self, frame: Frame, connection: socket.socket) -> None:
        with self._condition:
            if self._socket is not connection:
                return
            if frame.message_type == MessageType.DEBUG_SERVER_VERSION:
                self._server_version = decode_server_version(frame.payload)
            elif frame.message_type in (MessageType.HAS_STOPPED, MessageType.CONTINUED):
                if frame.message_type == MessageType.HAS_STOPPED:
                    reader = PayloadReader(frame.payload)
                    event = {"kind": "stopped", "reason": reader.read_string(), "description": reader.read_string(), "text": reader.read_string()}
                    reader.finish()
                    self._paused = True
                else:
                    event = {"kind": "continued"}
                    self._paused = False
                self._event_sequence += 1
                event["sequence"] = self._event_sequence
                self._last_event = event
                self._events.append(event)
            elif frame.message_type == MessageType.SET_BREAKPOINT:
                reader = PayloadReader(frame.payload)
                path = reader.read_string()
                line = reader.read_int()
                identifier = reader.read_int()
                reader.finish()
                breakpoint = self._breakpoints.get(identifier)
                if breakpoint is not None:
                    breakpoint.update(path=path, line=line, confirmed=True, verified=line > 0)
                    self._event_sequence += 1
                    event = {"kind": "breakpoint", "sequence": self._event_sequence, **breakpoint}
                    self._last_event = event
                    self._events.append(event)
            elif frame.message_type in (MessageType.CALLSTACK, MessageType.VARIABLES, MessageType.EVALUATE):
                self._replies[frame.message_type].append(frame)
            self._condition.notify_all()

    def _record_error(self, error: DebugSessionError) -> None:
        with self._condition:
            self._last_error = {"code": error.code, "message": str(error), "details": error.details}

    def _close_connection(
        self,
        error: DebugSessionError | None = None,
        expected: socket.socket | None = None,
        *,
        resume_paused: bool = True,
    ) -> None:
        with self._condition:
            if expected is not None and self._socket is not expected:
                return
            connection = self._socket
            paused = self._paused
            self._socket = None
            self._paused = None
            self._replies.clear()
            if error is not None:
                self._record_error(error)
            self._condition.notify_all()
        if connection is not None:
            if resume_paused and paused:
                try:
                    connection.sendall(encode_client_frame(MessageType.CONTINUE))
                except OSError:
                    pass
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()

    def _join_reader(self) -> None:
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join()
        self._reader = None
