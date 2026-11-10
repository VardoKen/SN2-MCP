"""Compatibility imports for the shared AngelScript protocol codec."""
from g1r_mcp.angelscript_debug_protocol import (
    ProtocolError, MessageType, Frame, encode_client_frame, encode_ascii_string, encode_string,
    encode_evaluate, ServerFrameDecoder, PayloadReader, decode_server_version,
    decode_evaluate,
)
