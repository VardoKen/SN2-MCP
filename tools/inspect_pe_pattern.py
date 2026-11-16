"""Inspect exact executable bytes and unwind ownership without opening a process."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import struct


def inspect(path: Path, pattern: str) -> dict:
    raw = path.read_bytes()
    pe = struct.unpack_from("<I", raw, 0x3c)[0]
    if raw[pe:pe + 4] != b"PE\0\0":
        raise ValueError("The input is not a PE image")
    count, optional = struct.unpack_from("<H", raw, pe + 6)[0], struct.unpack_from("<H", raw, pe + 20)[0]
    sections = []
    for index in range(count):
        offset = pe + 24 + optional + index * 40
        name = raw[offset:offset + 8].rstrip(b"\0").decode("ascii")
        virtual_size, rva, size, file_offset = struct.unpack_from("<IIII", raw, offset + 8)
        sections.append((name, rva, virtual_size, file_offset, size))
    def file_position(rva: int) -> int:
        for _, start, _, offset, size in sections:
            if start <= rva < start + size:
                return offset + rva - start
        raise ValueError("The RVA is not raw-backed")
    unwind = []
    for name, _, _, offset, size in sections:
        if name == ".pdata":
            for record in range(offset, offset + size - 11, 12):
                begin, end, info = struct.unpack_from("<III", raw, record)
                if begin:
                    unwind.append((begin, end, info))
    tokens = pattern.split()
    data = bytes(int(token, 16) if token != "??" else 0 for token in tokens)
    selected = [index for index, token in enumerate(tokens) if token != "??"]
    seed = max(selected, key=lambda index: len(data[index:index + 1]))
    matches = []
    for name, rva, _, offset, size in sections:
        if name != ".text":
            continue
        position = offset
        while True:
            position = raw.find(data[seed:seed + 1], position, offset + size)
            if position < 0:
                break
            start = position - seed
            if start >= offset and start + len(data) <= offset + size and all(raw[start + index] == data[index] for index in selected):
                match = rva + start - offset
                owners = [(begin, end, info) for begin, end, info in unwind if begin <= match < end]
                matches.append({"rva": hex(match), "raw_offset": hex(start), "owners": [
                    {"begin": hex(begin), "end": hex(end), "unwind": hex(info),
                     "bytes": raw[file_position(begin):file_position(begin) + min(end - begin, 256)].hex(" ")}
                    for begin, end, info in owners]})
            position += 1
    return {"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest(), "pattern": pattern, "matches": matches}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("pattern")
    args = parser.parse_args()
    print(json.dumps(inspect(args.path, args.pattern), indent=2))
