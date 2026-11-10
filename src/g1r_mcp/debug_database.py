"""Explicit read-only acquisition of the AngelScript runtime type database."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import math
import socket
import threading
import time
from typing import Any, Iterator

from .angelscript_debug_protocol import (
    MessageType, PayloadReader, ProtocolError, ServerFrameDecoder,
    decode_server_version, encode_client_frame,
)


class DebugDatabaseError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _decode_settings(payload: bytes) -> dict[str, Any]:
    result: dict[str, Any] = {"payload_bytes": len(payload), "payload_sha256": hashlib.sha256(payload).hexdigest()}
    reader = PayloadReader(payload)
    try:
        version = reader.read_int()
        result["version"] = version
        result["automatic_imports"] = reader.read_int() != 0
        for minimum, names in (
            (2, ("float_is_float64",)), (3, ("use_angelscript_haze",)),
            (5, ("deprecate_static_class", "disallow_static_class")),
            (6, ("expose_global_functions",)),
            (7, ("deprecate_actor_generics", "disallow_actor_generics")),
        ):
            if version >= minimum:
                for name in names:
                    result[name] = reader.read_int() != 0
        result["engine_supports_create_blueprint"] = version >= 4
    except ProtocolError as error:
        result["decode_error"] = str(error)
    remaining = payload[reader.offset:]
    result["remaining_bytes"] = len(remaining)
    if remaining:
        result["remaining_sha256"] = hashlib.sha256(remaining).hexdigest()
    return result


class DebugDatabaseCatalog:
    def __init__(self, host: str = "127.0.0.1", port: int = 27099):
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise DebugDatabaseError("invalid_endpoint", "The debug database endpoint must be a loopback IP address") from error
        if not address.is_loopback or type(port) is not int or not 1 <= port <= 65535:
            raise DebugDatabaseError("invalid_endpoint", "The debug database endpoint requires loopback and a valid TCP port")
        self.host, self.port = str(address), port
        self.chunks: list[dict[str, Any]] = []
        self.types: dict[str, Any] = {}
        self.duplicates: list[dict[str, Any]] = []
        self._state: dict[str, Any] = self._new_state()
        self._lock = threading.RLock()

    def _new_state(self) -> dict[str, Any]:
        return {"source": "live_angelscript_debug_database", "endpoint": {"host": self.host, "port": self.port},
                "complete": False, "partial": False, "refreshed": False, "chunk_count": 0, "type_count": 0,
                "definition_count": 0, "duplicate_count": 0, "received_bytes": 0, "payload_bytes": 0,
                "message_counts": {}, "settings": [], "server_version": None}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def refresh(self, timeout: float = 30.0) -> dict[str, Any]:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise DebugDatabaseError("invalid_arguments", "The debug database timeout must be finite and positive")
        with self._lock:
            self.chunks, self.types, self.duplicates = [], {}, []
            state = self._new_state()
            self._state = state
            state["refreshed"] = True
            started = time.monotonic()
            deadline = started + timeout
            connection: socket.socket | None = None
            decoder = ServerFrameDecoder(0xFFFFFFFF)
            try:
                connection = socket.create_connection((self.host, self.port), timeout=timeout)
                wait = deadline - time.monotonic()
                if wait <= 1:
                    raise DebugDatabaseError("timeout", "The timeout expired before the official database request delay")
                time.sleep(1.0)
                connection.settimeout(deadline - time.monotonic())
                connection.sendall(encode_client_frame(MessageType.REQUEST_DEBUG_DATABASE))
                while not state["complete"]:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise DebugDatabaseError("timeout", "The debug database completion marker was not received")
                    connection.settimeout(remaining)
                    chunk = connection.recv(65536)
                    if not chunk:
                        decoder.finish()
                        raise DebugDatabaseError("peer_closed", "The endpoint closed before the debug database completion marker")
                    state["received_bytes"] += len(chunk)
                    for frame in decoder.feed(chunk):
                        key = str(frame.message_type)
                        state["message_counts"][key] = state["message_counts"].get(key, 0) + 1
                        if frame.message_type == MessageType.DEBUG_DATABASE:
                            reader = PayloadReader(frame.payload)
                            text = reader.read_string()
                            reader.finish()
                            database = json.loads(text)
                            if not isinstance(database, dict):
                                raise DebugDatabaseError("invalid_database_chunk", "A debug database chunk must contain a JSON object of type definitions")
                            chunk_index = len(self.chunks)
                            self.chunks.append(database)
                            state["payload_bytes"] += len(frame.payload)
                            for name, definition in database.items():
                                if name in self.types:
                                    self.duplicates.append({"type_name": name, "chunk_index": chunk_index,
                                                            "previous": self.types[name], "replacement": definition})
                                self.types[name] = definition
                            state["definition_count"] += len(database)
                        elif frame.message_type == 31:
                            state["settings"].append(_decode_settings(frame.payload))
                        elif frame.message_type == MessageType.DEBUG_SERVER_VERSION:
                            state["server_version"] = decode_server_version(frame.payload)
                        elif frame.message_type == MessageType.DEBUG_DATABASE_FINISHED:
                            state["complete"] = True
                            break
                state["partial"] = False
            except (DebugDatabaseError, ProtocolError, json.JSONDecodeError, OSError, OverflowError, ValueError) as error:
                state["complete"] = False
                state["partial"] = bool(self.chunks)
                if isinstance(error, DebugDatabaseError):
                    code = error.code
                elif isinstance(error, socket.timeout):
                    code = "timeout"
                elif isinstance(error, ProtocolError):
                    code = "invalid_protocol"
                elif isinstance(error, json.JSONDecodeError):
                    code = "invalid_database_json"
                else:
                    code = "connection_error"
                state["error"] = {"code": code, "message": str(error)}
            finally:
                if connection is not None:
                    try:
                        connection.settimeout(min(float(timeout), 0.2))
                        connection.sendall(encode_client_frame(MessageType.DISCONNECT))
                    except OSError as error:
                        state["disconnect_error"] = str(error)
                    connection.close()
                state["chunk_count"] = len(self.chunks)
                state["type_count"] = len(self.types)
                state["duplicate_count"] = len(self.duplicates)
                state["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return copy.deepcopy(state)

    def _entries(self) -> Iterator[dict[str, Any]]:
        for type_name, definition in self.types.items():
            yield {"kind": "type", "type_name": type_name, "name": type_name, "data": definition}
            if not isinstance(definition, dict):
                continue
            properties = definition.get("properties", {})
            if isinstance(properties, dict):
                for name, data in properties.items():
                    yield {"kind": "property", "type_name": type_name, "name": name, "data": data}
            methods = definition.get("methods", {})
            if isinstance(methods, dict):
                method_items = methods.items()
            elif isinstance(methods, list):
                method_items = enumerate(methods)
            else:
                continue
            for key, data in method_items:
                name = data.get("name", str(key)) if isinstance(data, dict) else str(key)
                yield {"kind": "method", "type_name": type_name, "name": name, "data": data}

    def query(self, query: str = "", limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if not isinstance(query, str) or type(limit) is not int or limit < 1 or type(offset) is not int or offset < 0:
            raise DebugDatabaseError("invalid_arguments", "The catalog query requires text, a positive limit and a nonnegative offset")
        with self._lock:
            needle = query.casefold()
            items: list[dict[str, Any]] = []
            total = 0
            for entry in self._entries():
                if needle not in str(entry["type_name"]).casefold() and needle not in str(entry["name"]).casefold():
                    continue
                if offset <= total < offset + limit:
                    items.append(copy.deepcopy(entry))
                total += 1
            return {**copy.deepcopy(self._state), "query": query, "offset": offset, "limit": limit,
                    "items": items, "total_count": total, "total_matches": total, "returned_count": len(items),
                    "next_offset": offset + len(items) if offset + len(items) < total else None,
                    "has_more": offset + len(items) < total}
