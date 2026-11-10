"""Lexical AS function declarations with verbatim source prototypes."""

from bisect import bisect_right
import re
from collections.abc import Iterator


_TOKEN = re.compile(r"[A-Za-z_]\w*|::|->|[^\s]")
_PREFIXES = {"public", "private", "protected", "static", "mixin", "shared", "external", "const"}
_SUFFIXES = {"const", "override", "final", "property", "explicit"}
_EXCLUDED = {"default", "event", "delegate", "return", "if", "else", "for", "while", "switch", "case", "typedef", "funcdef"}
_ANNOTATIONS = {"UFUNCTION", "UCLASS", "USTRUCT", "UPROPERTY", "UENUM"}


def declarations(text: str, masked: str) -> Iterator[dict]:
    """Find declarations in global, namespace, and direct class/struct scope.

    This is not an AST: skip function bodies and keep parameters and qualifiers
    in the source prototype without claiming binding or compilation.
    """
    masked = re.sub(r"(?m)^[ \t]*#[^\n]*", lambda match: " " * len(match.group()), masked)
    tokens = list(_TOKEN.finditer(masked))
    values = [token.group() for token in tokens]
    newlines = [match.start() for match in re.finditer("\n", text)]

    def closing(index: int, left: str, right: str) -> int:
        depth = 0
        for position in range(index, len(tokens)):
            if values[position] == left:
                depth += 1
            elif values[position] == right:
                depth -= 1
                if depth == 0:
                    return position
        return len(tokens)

    def prefix_kind(start: int, opening: int, class_name: str) -> tuple[str, str] | None:
        prefix = values[start:opening]
        if not prefix or any(value in _EXCLUDED or value in {"=", ".", "->", ":", "(", ")"} for value in prefix):
            return None
        name = prefix[-1]
        if not re.fullmatch(r"[A-Za-z_]\w*", name):
            return None
        destructor = len(prefix) > 1 and prefix[-2] == "~"
        type_tokens = prefix[: -2 if destructor else -1]
        while type_tokens and type_tokens[0] in _PREFIXES:
            type_tokens = type_tokens[1:]
        if not type_tokens:
            if class_name and name == class_name:
                return ("~" + name if destructor else name, "destructor" if destructor else "constructor")
            return None
        if destructor or not re.fullmatch(r"[A-Za-z_]\w*", type_tokens[0]):
            return None
        # The type prefix permits qualified/template/ref/handle forms, but not calls.
        depth = 0
        previous = ""
        for value in type_tokens:
            if value == "<":
                depth += 1
            elif value == ">":
                depth -= 1
                if depth < 0:
                    return None
            elif value not in {"::", "&", "@", "+", "[", "]", ",", "*", "?"} and not re.fullmatch(r"[A-Za-z_]\w*", value):
                return None
            elif depth == 0 and re.fullmatch(r"[A-Za-z_]\w*", value) and previous and previous != "::" and value not in {"const", "in", "out", "inout"}:
                return None
            previous = value
        return (name, "function") if depth == 0 else None

    def scope(start: int, namespace: str = "", owner: str = "", class_name: str = "") -> Iterator[dict]:
        position = start
        statement = start
        while position < len(tokens):
            value = values[position]
            if value == "}":
                return position
            if value == ";" or (value == ":" and position == statement + 1 and values[statement] in {"public", "private", "protected"}):
                position += 1
                statement = position
                continue
            if position == statement and value in _ANNOTATIONS and position + 1 < len(tokens) and values[position + 1] == "(":
                position = closing(position + 1, "(", ")") + 1
                statement = position
                continue
            if value == "{":
                header = values[statement:position]
                namespace_header = header and header[0] == "namespace"
                class_index = next((index for index, entry in enumerate(header) if entry in {"class", "struct"}), None)
                if namespace_header:
                    nested = "".join(header[1:])
                    nested_namespace = "::".join(part for part in (namespace, nested) if part)
                    position = (yield from scope(position + 1, nested_namespace)) + 1
                elif class_index is not None and class_index + 1 < len(header):
                    nested_class = header[class_index + 1]
                    nested_owner = "::".join(part for part in (owner or namespace, nested_class) if part)
                    position = (yield from scope(position + 1, namespace, nested_owner, nested_class)) + 1
                else:
                    position = closing(position, "{", "}") + 1
                statement = position
                continue
            if value != "(":
                position += 1
                continue
            end_params = closing(position, "(", ")")
            if end_params >= len(tokens):
                return len(tokens)
            suffix = end_params + 1
            while suffix < len(tokens) and values[suffix] in _SUFFIXES:
                suffix += 1
            candidate = prefix_kind(statement, position, class_name)
            if candidate and suffix < len(tokens) and values[suffix] in {"{", ";"}:
                name, kind = candidate
                begin = tokens[statement].start()
                end = tokens[suffix - 1].end()
                yield {"name": name, "owner": owner, "namespace": namespace, "kind": kind,
                       "prototype": text[begin:end], "start_line": bisect_right(newlines, begin) + 1,
                       "end_line": bisect_right(newlines, end - 1) + 1}
                position = closing(suffix, "{", "}") + 1 if values[suffix] == "{" else suffix + 1
                statement = position
            else:
                position = end_params + 1
        return len(tokens)

    yield from scope(0)
