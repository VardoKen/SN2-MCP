"""One-shot AngelScript channel diagnostic without request retries.

Protocol pinned to Hazelight MIT, Copyright 2019 Hazelight Games AB:
https://github.com/Hazelight/vscode-unreal-angelscript/tree/ffc7ab2948589abb04200b7dcedd326eaeaf7a4d
extension/src/unreal-debugclient.ts, extension/src/debug.ts,
language-server/src/server.ts (database request one second after connecting).
Standalone diagnostics, not a game mod or MCP server.
"""

import argparse
from dataclasses import dataclass
import json
import re
import socket
import struct
import sys
import time

from angelscript_debug_protocol import (
    MessageType, ProtocolError, ServerFrameDecoder, decode_evaluate,
    decode_server_version, encode_client_frame, encode_evaluate,
)


class ProbeError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProbeOptions:
    host: str = "127.0.0.1"
    port: int = 27099
    timeout: float = 5.0
    max_frame_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    max_messages: int = 256
    request_database: bool = False
    start_debugging: bool = False
    evaluate: str | None = None
    frame_id: int | None = None


def validate_options(options: ProbeOptions) -> None:
    if not 1 <= options.port <= 65535:
        raise ProbeError("invalid_arguments", "Port must be between 1 and 65535")
    if not 0 < options.timeout <= 300:
        raise ProbeError("invalid_arguments", "Timeout must be greater than 0 and at most 300 seconds")
    if not 1 <= options.max_frame_bytes <= 0xFFFFFFFF or options.max_total_bytes < 1 or options.max_messages < 1:
        raise ProbeError("invalid_arguments", "Diagnostic limits must be positive")
    if options.request_database and options.timeout <= 1:
        raise ProbeError("invalid_arguments", "Database requests require a timeout longer than one second")
    if options.evaluate is None:
        if options.frame_id is not None:
            raise ProbeError("invalid_arguments", "--frame-id is only used with --evaluate")
        return
    if not options.start_debugging or options.frame_id is None:
        raise ProbeError("invalid_arguments", "--evaluate requires explicit --start-debugging and --frame-id")
    expression = options.evaluate
    # Only syntactic literals; calls, properties, and operators are not executed.
    scalar = r"(?:true|false|null|[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)"
    quoted = r'''(?:"(?:[^"\\\r\n]|\\["\\nrt])*"|'(?:[^'\\\r\n]|\\['\\nrt])*')'''
    if not expression.isascii() or not re.fullmatch(f"(?:{scalar}|{quoted})", expression):
        raise ProbeError("invalid_arguments", "The initial probe only accepts a simple ASCII literal")
    try:
        encode_evaluate(expression, options.frame_id)
    except ProtocolError as error:
        raise ProbeError("invalid_arguments", str(error)) from error


def collect_probe(connection: socket.socket, options: ProbeOptions, result: dict) -> None:
    """Collect a bounded response from one connection without automatic replay."""
    decoder = ServerFrameDecoder(options.max_frame_bytes)
    started = time.monotonic()
    deadline = started + options.timeout
    database_due = started + 1.0
    database_sent = False
    evaluate_sent = False
    if options.start_debugging:
        connection.sendall(encode_client_frame(MessageType.START_DEBUGGING, struct.pack("<i", 2)))
        result["sent_messages"].append(int(MessageType.START_DEBUGGING))
    if options.evaluate is not None:
        connection.sendall(encode_evaluate(options.evaluate, options.frame_id))
        result["sent_messages"].append(int(MessageType.REQUEST_EVALUATE))
        evaluate_sent = True

    while True:
        now = time.monotonic()
        if options.request_database and not database_sent and now >= database_due:
            connection.sendall(encode_client_frame(MessageType.REQUEST_DEBUG_DATABASE))
            result["sent_messages"].append(int(MessageType.REQUEST_DEBUG_DATABASE))
            database_sent = True
        remaining = deadline - now
        if remaining <= 0:
            if decoder.pending_bytes:
                decoder.finish()
            raise ProbeError("timeout", "Diagnostic result deadline expired; requests were not retried")
        wait = min(remaining, max(0.001, database_due - now)) if options.request_database and not database_sent else remaining
        connection.settimeout(wait)
        try:
            chunk = connection.recv(min(65536, options.max_total_bytes - result["received_bytes"] + 1))
        except socket.timeout:
            continue
        if not chunk:
            decoder.finish()
            raise ProbeError("peer_closed", "Server closed the connection before the complete result")
        result["received_bytes"] += len(chunk)
        if result["received_bytes"] > options.max_total_bytes:
            raise ProbeError("limit_exceeded", "Total diagnostic receive limit exceeded")
        for frame in decoder.feed(chunk):
            if len(result["messages"]) >= options.max_messages:
                raise ProbeError("limit_exceeded", "Message count limit exceeded")
            result["messages"].append({"type": frame.message_type, "payload_bytes": len(frame.payload)})
            if frame.message_type == MessageType.DEBUG_SERVER_VERSION:
                result["server_version"] = decode_server_version(frame.payload)
            elif frame.message_type == MessageType.DEBUG_DATABASE:
                result["database_chunks"] += 1
                result["database_bytes"] += len(frame.payload)
            elif frame.message_type == MessageType.DEBUG_DATABASE_FINISHED and database_sent:
                result["database_finished"] = True
            elif frame.message_type == MessageType.EVALUATE and evaluate_sent:
                if result.get("evaluation") is not None:
                    raise ProtocolError("Repeated Evaluate has no confirmed correlation")
                result["evaluation"] = decode_evaluate(frame.payload)
        complete = (
            "server_version" in result
            and (not options.request_database or result["database_finished"])
            and (options.evaluate is None or result.get("evaluation") is not None)
        )
        if complete and not decoder.pending_bytes:
            result["elapsed_seconds"] = round(time.monotonic() - started, 6)
            return


def run_probe(options: ProbeOptions, connector=socket.create_connection) -> dict:
    """Connect once; send only Disconnect when finished."""
    result = {
        "ok": False, "endpoint": {"host": options.host, "port": options.port},
        "sent_messages": [], "messages": [], "received_bytes": 0,
        "database_chunks": 0, "database_bytes": 0, "database_finished": False,
        "limitations": [
            "This probe determines G1R compatibility; an open client does not",
            "Evaluate is matched FIFO to the only request; the protocol has no request_id",
            "A literal result does not confirm function calls, the game thread, or a mod handler",
            "Database results contain only message counts and sizes, without source code",
        ],
    }
    connection = None
    try:
        validate_options(options)
        connection = connector((options.host, options.port), timeout=options.timeout)
        connection.settimeout(options.timeout)
        collect_probe(connection, options, result)
        result["ok"] = True
    except ProbeError as error:
        result["error"] = {"code": error.code, "message": str(error)}
    except ProtocolError as error:
        result["error"] = {"code": "protocol_error", "message": str(error)}
    except (OSError, OverflowError, ValueError) as error:
        result["error"] = {"code": "connection_error", "message": (f"The debugger connection failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})" if isinstance(error, OSError) else str(error))}
    finally:
        if connection is not None:
            try:
                connection.settimeout(min(options.timeout, 0.2))
                connection.sendall(encode_client_frame(MessageType.DISCONNECT))
                result["sent_messages"].append(int(MessageType.DISCONNECT))
            except OSError as error:
                result["disconnect_error"] = f"The debugger disconnect failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})"
            finally:
                connection.close()
    return result


class JsonArgumentParser(argparse.ArgumentParser):
    def format_help(self):
        return super().format_help().replace("usage: ", "Usage: ", 1)

    def error(self, message):
        translated = message.replace("unrecognized arguments:", "Unknown arguments:")
        translated = translated.replace("argument ", "Argument ").replace("expected one argument", "expected one value")
        translated = re.sub(r"invalid int value: (.*)", r"expected an integer: \1", translated)
        translated = re.sub(r"invalid float value: (.*)", r"expected a number: \1", translated)
        raise ProbeError("invalid_arguments", translated)


def main(argv=None) -> int:
    parser = JsonArgumentParser(
        description="One-shot AngelScript probe without Pause, Continue, or command retries",
        add_help=False, usage="%(prog)s [options]",
    )
    parser._optionals.title = "Options"
    parser.add_argument("-h", "--help", action="help", help="Show help and exit")
    parser.add_argument("--host", default="127.0.0.1", help="Server address; defaults to loopback")
    parser.add_argument("--port", type=int, default=27099, help="Port; defaults to 27099")
    parser.add_argument("--timeout", type=float, default=5.0, help="Connection and result collection deadline in seconds")
    parser.add_argument("--max-frame-bytes", type=int, default=16 * 1024 * 1024, help="Maximum server payload size")
    parser.add_argument("--max-total-bytes", type=int, default=64 * 1024 * 1024, help="Total received byte limit")
    parser.add_argument("--max-messages", type=int, default=256, help="Received message limit")
    parser.add_argument("--request-database", action="store_true", help="Request database metadata once, one second after connecting")
    parser.add_argument("--start-debugging", action="store_true", help="Explicitly send StartDebugging with adapter version 2")
    parser.add_argument("--evaluate", help="One simple ASCII literal; arbitrary expressions are not accepted")
    parser.add_argument("--frame-id", type=int, help="Explicit frame_id for Evaluate; no default is confirmed")
    try:
        options = ProbeOptions(**vars(parser.parse_args(argv)))
        result = run_probe(options)
    except ProbeError as error:
        result = {"ok": False, "error": {"code": error.code, "message": str(error)}}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
