"""Create a standard unified diff for official Script/ without writing source files.

Hashes refer to original bytes; line endings and UTF-8 BOM are not normalized.
The standard diff format does not verify application by the game loader.
"""
from __future__ import annotations

import difflib
import hashlib
from pathlib import Path
import re

from .workspace import WorkspaceError, _checked, _guard_directories, _parts, _read_bytes


class PatchError(Exception):
    """Predictable patch creation error without disclosing an absolute path."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ScriptPatchBuilder:
    def __init__(self, modkit_root: str | Path, revision: str | None = None):
        try:
            self.root = _checked(Path(modkit_root), directory=True)
            self.script_root = _checked(self.root / "Script", self.root, directory=True)
        except WorkspaceError as error:
            raise PatchError(error.code, str(error)) from None
        self.revision = revision

    def _source(self, source_path: str) -> tuple[str, bytes]:
        try:
            parts = _parts(source_path)
            if len(parts) < 2 or parts[0] != "Script" or Path(parts[-1]).suffix.casefold() != ".as":
                raise PatchError("unsupported_file", "The patch source must be an official Modkit Script/**/*.as file")
            path = _checked(self.root.joinpath(*parts), self.script_root)
            with _guard_directories(path.parent, self.script_root):
                raw = _read_bytes(path, self.script_root, None)
        except WorkspaceError as error:
            raise PatchError(error.code, str(error)) from None
        self._text(raw)
        return "/".join(parts), raw

    @staticmethod
    def _text(raw: bytes) -> str:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise PatchError("invalid_encoding", "The source and replacement must be valid UTF-8") from None
        if "\0" in text:
            raise PatchError("invalid_text", "Binary content containing NUL is not supported")
        if "\r" in text.replace("\r\n", ""):
            raise PatchError("unsupported_line_endings", "LF and CRLF are supported; standalone CR is not supported")
        return text

    def source_info(self, source_path: str) -> dict:
        path, raw = self._source(source_path)
        return {"source": "official_modkit", "revision": self.revision, "path": path,
                "bytes": len(raw), "source_sha256": hashlib.sha256(raw).hexdigest(),
                "max_source_bytes": None}

    def build_script_patch(self, source_path: str, replacement_content: str,
                           expected_source_sha256: str, context_lines: int = 3) -> dict:
        if (not isinstance(expected_source_sha256, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_source_sha256)):
            raise PatchError("invalid_arguments", "A source SHA-256 containing 64 hexadecimal characters is required")
        if isinstance(context_lines, bool) or not isinstance(context_lines, int) or context_lines < 0:
            raise PatchError("invalid_arguments", "The context line count must be a nonnegative integer")
        if not isinstance(replacement_content, str):
            raise PatchError("invalid_arguments", "The replacement must be UTF-8 text")
        try:
            replacement = replacement_content.encode("utf-8")
        except UnicodeEncodeError:
            raise PatchError("invalid_encoding", "The replacement must be valid UTF-8") from None
        path, source = self._source(source_path)
        source_sha = hashlib.sha256(source).hexdigest()
        if source_sha != expected_source_sha256.lower():
            raise PatchError("conflict", "The source changed; read it and its SHA-256 again")
        before_text, after_text = self._text(source), self._text(replacement)
        # split('\n') preserves CRLF and treats only LF as a diff line boundary.
        def lines(text: str) -> list[str]:
            pieces = text.split("\n")
            return [piece + "\n" for piece in pieces[:-1]] + ([pieces[-1]] if pieces[-1] else [])
        patch_path = path.removeprefix("Script/")
        generated = difflib.unified_diff(lines(before_text), lines(after_text),
                                         fromfile="a/" + patch_path, tofile="b/" + patch_path,
                                         n=context_lines, lineterm="\n")
        chunks: list[str] = []
        hunks = 0
        patch_size = 0
        for line in generated:
            if line.startswith("@@ "):
                hunks += 1
            if not line.endswith("\n"):
                line += "\n\\ No newline at end of file\n"
            patch_size += len(line.encode("utf-8"))
            chunks.append(line)
        patch_text = "".join(chunks)
        return {"source": "official_modkit", "revision": self.revision, "path": path,
                "source_sha256": source_sha, "replacement_sha256": hashlib.sha256(replacement).hexdigest(),
                "source_bytes": len(source), "replacement_bytes": len(replacement),
                "changed": source != replacement, "hunks": hunks, "context_lines": context_lines,
                "patch_text": patch_text, "patch_bytes": patch_size,
                "validation": "standard_unified_diff", "engine_apply": "not_performed",
                "compiled": False}
