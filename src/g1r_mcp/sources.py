"""Bounded read-only catalog of official AngelScript sources.

The revision is a caller-supplied server label rather than independently verified Git data.
The lexical type list does not replace the compiler or describe native bindings.
Traversal is sequential: the result is not an atomic snapshot of the entire Modkit.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import hashlib
import os
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .source_functions import declarations as function_declarations
from .source_identity import inspect as inspect_source_identity


class SourceError(Exception):
    """Predictable source error without absolute user paths."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _offset(value: int) -> int:
    if type(value) is not int or value < 0:
        raise SourceError("invalid_arguments", "The offset must be a nonnegative integer")
    return value


def _page(total: int, offset: int, returned: int) -> dict:
    next_index = offset + returned
    has_more = next_index < total
    return {"offset": offset, "next_offset": next_index if has_more else None,
            "has_more": has_more, "pagination_limit_reached": False,
            "max_offset": None}


@dataclass(frozen=True)
class SourceFile:
    path: Path
    relative: str
    root_kind: str


def _check_path(path: Path, boundary: Path | None = None, *, directory: bool = False) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        parts = (absolute, *absolute.parents)
        for part in parts:
            info = part.lstat()
            if (part.is_symlink() or (hasattr(part, "is_junction") and part.is_junction())
                    or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                raise SourceError("unsafe_path", "The source path contains a link or reparse point")
            if boundary is not None and part == boundary:
                break
        resolved = absolute.resolve(strict=True)
        if boundary is not None and not resolved.is_relative_to(boundary):
            raise SourceError("unsafe_path", "The source path is outside the allowed directory")
        if directory and not resolved.is_dir():
            raise SourceError("invalid_path", "The source directory has an invalid type")
        if not directory and not resolved.is_file():
            raise SourceError("invalid_path", "The source has an invalid type")
        return resolved
    except SourceError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise SourceError("source_unavailable", "The source is missing or inaccessible") from None


def _read_source_bytes(file: SourceFile, boundary: Path, maximum: int | None = None) -> bytes:
    try:
        path = _check_path(file.path, boundary)
        before = path.stat()
        if maximum is not None and before.st_size > maximum:
            raise SourceError("file_too_large", "The source exceeds the selected reading mode limit")
        with path.open("rb") as stream:
            raw = stream.read() if maximum is None else stream.read(maximum + 1)
        if maximum is not None and len(raw) > maximum:
            raise SourceError("file_too_large", "The source exceeds the selected reading mode limit")
        _check_path(path, boundary)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise SourceError("source_changed", "The source changed during reading; repeat the request")
        return raw
    except SourceError:
        raise
    except (OSError, ValueError):
        raise SourceError("source_unavailable", "The source is missing or inaccessible") from None


def _read_source(file: SourceFile, boundary: Path) -> tuple[str, str]:
    raw = _read_source_bytes(file, boundary)
    try:
        return raw.decode("utf-8-sig", errors="strict"), hashlib.sha256(raw).hexdigest()
    except UnicodeDecodeError:
        raise SourceError("invalid_encoding", "The source is not valid UTF-8") from None


def _read_text(file: SourceFile, boundary: Path) -> str:
    return _read_source(file, boundary)[0]


def _discovered_file(path: Path, boundary: Path) -> Path:
    """Validate a file in an already checked os.walk directory without traversing ancestors."""
    try:
        info = path.lstat()
        if (path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise SourceError("unsafe_path", "The source contains a link or reparse point")
        if not path.is_relative_to(boundary):
            raise SourceError("unsafe_path", "The source is outside the allowed directory")
        if not path.is_file():
            raise SourceError("invalid_path", "The source has an invalid type")
        return path
    except SourceError:
        raise
    except (OSError, ValueError, RuntimeError):
        raise SourceError("source_unavailable", "The source is missing or inaccessible") from None


def _limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceError("invalid_arguments", "The limit must be a positive integer")
    return value


def _query(value: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value):
        raise SourceError("invalid_arguments", "The query must be a nonempty string without NUL")
    return value


def _mask_noncode(text: str) -> str:
    """Preserve positions/lines while excluding comments and string literals."""
    pattern = r'//[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
    return re.sub(pattern, lambda match: "".join("\n" if c == "\n" else " " for c in match.group()), text)


def _types(text: str) -> Iterator[dict]:
    """Extract class/struct/enum declarations with best-effort namespaces."""
    masked = _mask_noncode(text)
    tokens = re.finditer(
        r"\bnamespace\s+(?P<namespace>[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\s*\{"
        r"|(?P<declaration>\b(?:(?:shared|external|abstract|final)\s+)*"
        r"(?P<kind>class|struct|enum)\s+(?P<name>[A-Za-z_]\w*))|[{}]", masked)
    header_delimiters = re.compile(r"[{};]")
    namespace_stack: list[tuple[int, str]] = []
    depth = 0
    line_number = 1
    previous_offset = 0
    for token in tokens:
        line_number += masked.count("\n", previous_offset, token.start())
        previous_offset = token.start()
        if token.group("namespace"):
            depth += 1
            namespace_stack.append((depth, token.group("namespace")))
        elif token.group("kind"):
            name = token.group("name")
            namespace = "::".join(item[1] for item in namespace_stack)
            delimiter = header_delimiters.search(masked, token.end())
            header_end = token.end() if delimiter is None else delimiter.start()
            base_end = token.end() + len(masked[token.end():header_end].rstrip())
            header_end = token.end() + len(text[token.end():header_end].rstrip())
            base_type = None
            if token.group("kind") != "enum":
                colon = re.search(r"(?<!:):(?!:)", masked[token.end():base_end])
                if colon is not None:
                    base_start = token.end() + colon.end()
                    first_code = re.search(r"\S", masked[base_start:base_end])
                    if first_code is not None:
                        base_type = text[base_start + first_code.start():base_end]
            yield {"kind": token.group("kind"), "name": name,
                   "qualified_name": f"{namespace}::{name}" if namespace else name,
                   "line": line_number + masked.count("\n", token.start(), token.start("kind")),
                   "declaration": text[token.start():header_end], "base_type": base_type,
                   "start_line": line_number,
                   "end_line": line_number + masked.count("\n", token.start(), header_end)}
        elif token.group() == "{":
            depth += 1
        else:
            if namespace_stack and namespace_stack[-1][0] == depth:
                namespace_stack.pop()
            depth = max(0, depth - 1)


class SourceCatalog:
    """Allowed Script/ and Examples/<example>/Script/ directories of the explicitly selected Modkit."""

    def __init__(self, modkit_root: str | Path, *, revision: str | None = None):
        self.root = _check_path(Path(modkit_root), directory=True)
        self.script_root = _check_path(self.root / "Script", self.root, directory=True)
        if revision is not None and (not isinstance(revision, str) or any(ord(c) < 32 for c in revision)):
            raise SourceError("invalid_arguments", "The revision label has an unsupported format")
        self.revision = revision

    def source_identity(self) -> dict:
        """Read the selected Modkit Git identity separately from the source index."""
        return inspect_source_identity(self.root, self.revision)

    def _files(self) -> list[SourceFile]:
        _check_path(self.root, directory=True)
        roots = [(self.script_root, "game_script")]
        examples = self.root / "Examples"
        if examples.exists() or examples.is_symlink():
            examples = _check_path(examples, self.root, directory=True)
            try:
                for example in sorted(examples.iterdir(), key=lambda p: p.name.casefold()):
                    if example.is_symlink() or (hasattr(example, "is_junction") and example.is_junction()):
                        raise SourceError("unsafe_path", "The example directory contains a symbolic link or junction")
                    example_script = example / "Script"
                    if example.is_dir() and (example_script.exists() or example_script.is_symlink()):
                        roots.append((_check_path(example / "Script", self.root, directory=True), "example_script"))
            except SourceError:
                raise
            except OSError:
                raise SourceError("source_unavailable", "The examples directory is inaccessible") from None
        result: list[SourceFile] = []
        def walk_error(error: OSError) -> None:
            raise SourceError("source_unavailable", "The source directory is inaccessible")
        for source_root, kind in roots:
            for directory, dirs, names in os.walk(source_root, followlinks=False, onerror=walk_error):
                parent = _check_path(Path(directory), self.root, directory=True)
                for name in dirs:
                    _check_path(parent / name, self.root, directory=True)
                for name in names:
                    if Path(name).suffix.casefold() != ".as":
                        continue
                    path = _discovered_file(parent / name, self.root)
                    result.append(SourceFile(path, path.relative_to(self.root).as_posix(), kind))
        if not result:
            raise SourceError("no_sources", "No AngelScript sources were found in the allowed directories")
        return sorted(result, key=lambda file: file.relative)

    def _result(self, items: list[dict], total: int, scanned: int, **extra) -> dict:
        return {"items": items, "truncated": total > len(items), "truncation_scope": "items", "total_matches": total,
                "scanned_files": scanned, "scan_complete": True,
                "source": "official_modkit_static", "validation": "source_only",
                "revision": self.revision, "revision_source": "caller_label",
                "snapshot_kind": "sequential_file_reads", **extra}

    def _scan_result(self, items: list[dict], total: int, files: list[SourceFile], skipped: list[dict], **extra) -> dict:
        result = self._result(items, total, len(files), **extra)
        result.update(skipped_files=skipped, skipped_count=len(skipped), skipped_list_truncated=False)
        if skipped:
            result["scan_complete"] = False
            result["total_matches"] = None
            result["total_matches_lower_bound"] = total
        return result

    @staticmethod
    def _item(file: SourceFile, **values) -> dict:
        return {"path": file.relative, "root_kind": file.root_kind, **values}

    def search_files(self, query: str, limit: int = 50, offset: int = 0) -> dict:
        needle = _query(query).casefold()
        _limit(limit)
        _offset(offset)
        files = self._files()
        total = 0
        items = []
        for file in files:
            if needle in file.relative.casefold():
                total += 1
                if total > offset and len(items) < limit:
                    items.append(self._item(file))
        return self._result(items, total, len(files), **_page(total, offset, len(items)))

    def search_text(self, query: str, limit: int = 50, case_sensitive: bool = False, offset: int = 0) -> dict:
        query = _query(query)
        _limit(limit)
        _offset(offset)
        if not isinstance(case_sensitive, bool):
            raise SourceError("invalid_arguments", "The case sensitivity parameter must be a boolean")
        needle = query if case_sensitive else query.casefold()
        files = self._files()
        items = []
        total = 0
        skipped = []
        for file in files:
            try:
                text = _read_text(file, self.root)
            except SourceError as error:
                if error.code == "unsafe_path":
                    raise
                skipped.append(self._item(file, error_code=error.code))
                continue
            for line_number, line in enumerate(text.splitlines(), 1):
                if needle in (line if case_sensitive else line.casefold()):
                    total += 1
                    if total > offset and len(items) < limit:
                        items.append(self._item(file, line=line_number, text=line, text_truncated=False))
        return self._scan_result(items, total, files, skipped, query_kind="literal", **_page(total, offset, len(items)))

    def _source_file(self, relative_path: str) -> SourceFile:
        if (not isinstance(relative_path, str) or not relative_path or "\\" in relative_path
                or "\0" in relative_path or ":" in relative_path):
            raise SourceError("unsafe_path", "A relative path with forward slash separators is required")
        path = PurePosixPath(relative_path)
        winpath = PureWindowsPath(relative_path)
        if path.is_absolute() or winpath.drive or winpath.root or any(part in (".", "..") for part in relative_path.split("/")):
            raise SourceError("unsafe_path", "Absolute paths or traversal outside the source directory are prohibited")
        normalized = path.as_posix()
        parts = path.parts
        game_script = len(parts) >= 2 and parts[0] == "Script"
        example_script = len(parts) >= 4 and parts[0] == "Examples" and parts[2] == "Script"
        if path.suffix.casefold() != ".as" or not (game_script or example_script):
            raise SourceError("source_not_found", "The file is missing from the allowed AngelScript directory")
        _check_path(self.root, directory=True)
        try:
            source_path = _check_path(self.root / normalized, self.root)
        except SourceError as error:
            if error.code == "source_unavailable":
                raise SourceError("source_not_found", "The file is missing from the allowed AngelScript directory") from None
            raise
        return SourceFile(source_path, normalized, "game_script" if game_script else "example_script")

    def _selected_source_files(self, relative_path: str | None) -> list[SourceFile]:
        if relative_path is None:
            return self._files()
        if (not isinstance(relative_path, str) or not relative_path or "\\" in relative_path
                or "\0" in relative_path or ":" in relative_path):
            raise SourceError("unsafe_path", "A relative path with forward slash separators is required")
        path = PurePosixPath(relative_path)
        parts = path.parts
        if path.is_absolute() or any(part in (".", "..") for part in relative_path.split("/")):
            raise SourceError("unsafe_path", "Absolute paths or traversal outside the source directory are prohibited")
        if not (parts[0] == "Script" or len(parts) >= 3 and parts[0] == "Examples" and parts[2] == "Script"):
            raise SourceError("source_not_found", "The path does not belong to the allowed AngelScript sources")
        selected_path = self.root / path
        selected = _check_path(selected_path, self.root, directory=selected_path.is_dir())
        normalized = path.as_posix()
        if selected.is_file() and path.suffix.casefold() == ".as":
            return [self._source_file(relative_path)]
        return [file for file in self._files() if file.relative == normalized
                or selected.is_dir() and file.relative.startswith(normalized + "/")]

    def read_file_exact(self, relative_path: str) -> dict:
        """Read the full UTF-8 source without changing the BOM, line endings, or end of file."""
        file = self._source_file(relative_path)
        raw = _read_source_bytes(file, self.root)
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            raise SourceError("invalid_encoding", "The source is not valid UTF-8") from None
        item = self._item(file, text=text, sha256=hashlib.sha256(raw).hexdigest(),
                          bytes=len(raw), encoding="UTF-8", text_mode="exact",
                          contains_bom=raw.startswith(b"\xef\xbb\xbf"), content_truncated=False,
                          max_text_bytes=None)
        return self._result([item], 1, 1, **item)

    def read_file(self, relative_path: str, start_line: int = 1, line_count: int = 100) -> dict:
        file = self._source_file(relative_path)
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise SourceError("invalid_arguments", "The first line number must be a positive integer")
        if isinstance(line_count, bool) or not isinstance(line_count, int) or line_count < 1:
            raise SourceError("invalid_arguments", "The line count must be a positive integer")
        source_text, source_sha256 = _read_source(file, self.root)
        lines = source_text.splitlines()
        selected = lines[start_line - 1:start_line - 1 + line_count]
        returned = selected
        text = "\n".join(returned)
        item = self._item(file, sha256=source_sha256, text_mode="display_lines",
                          exact_replacement_available=True, exact_max_text_bytes=None,
                          start_line=start_line, end_line=start_line + len(returned) - 1 if returned else None,
                          total_lines=len(lines), text=text, returned_lines=len(returned),
                          returned_utf8_bytes=len(text.encode("utf-8")), max_text_bytes=None,
                          last_line_truncated=False,
                          content_truncated=start_line > 1 or start_line - 1 + line_count < len(lines))
        return self._result([item], 1, 1, **item)

    def list_types(self, query: str = "", limit: int = 50, offset: int = 0,
                   relative_path: str | None = None) -> dict:
        needle = _query(query, empty=True).casefold()
        _limit(limit)
        _offset(offset)
        files = self._selected_source_files(relative_path)
        items = []
        total = 0
        skipped = []
        for file in files:
            try:
                text, file_sha256 = _read_source(file, self.root)
            except SourceError as error:
                if error.code == "unsafe_path":
                    raise
                skipped.append(self._item(file, error_code=error.code))
                continue
            for declaration in _types(text):
                if needle in declaration["qualified_name"].casefold():
                    total += 1
                    if total > offset and len(items) < limit:
                        items.append(self._item(file, **declaration, file_sha256=file_sha256))
        return self._scan_result(items, total, files, skipped, parser_kind="lexical",
                            limitations=["Incomplete lexical index, not a compiler AST", "Native bindings are not included", "Game calls have not been verified"],
                            **_page(total, offset, len(items)))

    def list_functions(self, query: str = "", relative_path: str | None = None,
                       limit: int = 50, offset: int = 0) -> dict:
        """Find full lexical AS prototypes in a file or source subtree."""
        if not isinstance(query, str):
            raise SourceError("invalid_arguments", "The query must be a string")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise SourceError("invalid_arguments", "The limit must be a positive integer")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise SourceError("invalid_arguments", "The offset must be a nonnegative integer")
        files = self._selected_source_files(relative_path)
        items = []
        skipped = []
        total = 0
        needle = query.casefold()
        for file in files:
            try:
                raw = _read_source_bytes(file, self.root, file.path.stat().st_size)
                text = raw.decode("utf-8-sig", errors="strict")
            except UnicodeDecodeError:
                skipped.append(self._item(file, error_code="invalid_encoding"))
                continue
            except (OSError, SourceError) as error:
                if isinstance(error, SourceError) and error.code == "unsafe_path":
                    raise
                skipped.append(self._item(file, error_code=getattr(error, "code", "source_unavailable")))
                continue
            file_sha256 = hashlib.sha256(raw).hexdigest()
            for declaration in function_declarations(text, _mask_noncode(text)):
                if not any(needle in declaration[key].casefold() for key in ("name", "owner", "prototype")):
                    continue
                total += 1
                if total > offset and len(items) < limit:
                    items.append(self._item(file, **declaration, file_sha256=file_sha256))
        next_offset = offset + len(items)
        result = self._result(items, total, len(files), parser_kind="lexical", offset=offset,
                              next_offset=next_offset if next_offset < total else None,
                              has_more=next_offset < total, file_count=len(files), source_revision=self.revision,
                              limitations=["Lexical declarations, not a compiler AST", "Native bindings are not included",
                                           "Availability and behavior in the game have not been verified"],
                              skipped_files=skipped, skipped_count=len(skipped), skipped_list_truncated=False)
        if skipped:
            result.update(scan_complete=False, total_matches=None, total_matches_lower_bound=total)
        return result
