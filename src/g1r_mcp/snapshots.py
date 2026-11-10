"""Compare snapshot copies without accessing the game environment.

Client-supplied provenance fields are client claims.
Equality of selected values does not prove that the game world is unchanged.
"""
from __future__ import annotations

import json
from typing import Any

MAX_OBJECT_INDEX = 2**31 - 1
ENUM_INTEGER_RANGES = {
    "IntProperty": (-(2**31), 2**31 - 1),
    "Int64Property": (-(2**63), 2**63 - 1),
    "UInt32Property": (0, 2**32 - 1),
    "UInt64Property": (0, 2**64 - 1),
    "ByteProperty": (0, 255),
}


class SnapshotError(Exception):
    """Invalid snapshot copy."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> None:
    raise SnapshotError("invalid_snapshot", message)


def _text(value: Any, label: str, *, optional: bool = False) -> None:
    if optional and value is None:
        return
    if not isinstance(value, str) or not value:
        _invalid(f"{label}: a nonempty string is required")


def _integer(value: Any, label: str, *, positive: bool = False) -> None:
    if type(value) is not int or value < (1 if positive else 0):
        _invalid(f"{label}: a {'positive' if positive else 'nonnegative'} integer is required")


def _error(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"code", "message"}:
        _invalid("An object or property error must contain code and message")
    _text(value["code"], "Error code")
    if not isinstance(value["message"], str):
        _invalid("The error message must be a string")


def _object_value(value: Any, session_id: str, property_type: str) -> None:
    """Validate an opaque reference copy without resolving it in the game environment."""
    if value is None:
        return
    expected = {"handle", "index", "name", "path", "class", "session_id"}
    if not isinstance(value, dict) or not expected <= value.keys() or not value.keys() <= expected | {"world_generation", "script_generation"}:
        _invalid(f"{property_type} must contain null or an exact object descriptor")
    _text(value["handle"], f"Value reference for {property_type}")
    _integer(value["index"], f"Value index for {property_type}")
    if value["index"] > MAX_OBJECT_INDEX:
        _invalid(f"Value index for {property_type} cannot be represented as a nonnegative int32")
    for key in ("name", "class", "path"):
        _text(value[key], f"{property_type}.{key}")
    _text(value["session_id"], f"Value session for {property_type}")
    if value["session_id"] != session_id:
        _invalid(f"Descriptor {property_type} belongs to another snapshot session")
    if "world_generation" in value and value["world_generation"] is not None:
        _integer(value["world_generation"], f"{property_type}.world_generation", positive=True)
    if "script_generation" in value and value["script_generation"] is not None:
        _integer(value["script_generation"], f"{property_type}.script_generation")


def _value_kind(prop: dict[str, Any]) -> str:
    if prop["type"] in {"ObjectProperty", "ClassProperty", "WeakObjectProperty"}:
        return "class_reference" if prop["type"] == "ClassProperty" else "object_reference"
    return type(prop["value"]).__name__


def _enum_metadata(metadata: Any, prop: dict[str, Any]) -> None:
    expected = {"path", "underlying_type", "name_status", "value_name", "matched_count", "ambiguous", "reason"}
    if not isinstance(metadata, dict) or set(metadata) != expected:
        _invalid("Invalid enum metadata field set")
    if prop.get("type") not in {"EnumProperty", "ByteProperty"}:
        _invalid("Enum metadata is allowed only for EnumProperty or ByteProperty")
    underlying = metadata["underlying_type"]
    if not isinstance(underlying, str) or underlying not in ENUM_INTEGER_RANGES:
        _invalid("Unknown enum numeric type")
    if prop["type"] == "ByteProperty" and underlying != "ByteProperty":
        _invalid("ByteProperty requires a matching enum underlying type")
    if prop["supported"]:
        minimum, maximum = ENUM_INTEGER_RANGES[underlying]
        if type(prop.get("value")) is not int or not minimum <= prop["value"] <= maximum:
            _invalid("The enum numeric value does not match its underlying type")
    for key in ("path", "value_name", "reason"):
        if metadata[key] is not None:
            _text(metadata[key], f"enum.{key}")
            if "\0" in metadata[key]:
                _invalid(f"enum.{key}: the string contains NUL")
    count = metadata["matched_count"]
    _integer(count, "Enum match count")
    if type(metadata["ambiguous"]) is not bool or metadata["ambiguous"] != (count > 1):
        _invalid("Invalid enum match information")
    status = metadata["name_status"]
    if not isinstance(status, str) or status not in {"matched", "not_found", "unavailable", "value_unrepresentable"}:
        _invalid("Unknown enum name status")
    if status == "matched":
        if count == 0 or metadata["value_name"] is None or metadata["path"] is None or metadata["reason"] is not None:
            _invalid("matched requires a name, path, and match without an error")
    elif metadata["value_name"] is not None:
        _invalid("An unread enum name must be null")
    if status in {"not_found", "value_unrepresentable"} and count != 0:
        _invalid("An enum status without matches requires matched_count=0")
    if status == "not_found" and metadata["path"] is None:
        _invalid("not_found requires a known enum path")
    if status == "value_unrepresentable" and (
        underlying != "UInt64Property" or not prop["supported"] or prop["value"] <= 2**63 - 1
    ):
        _invalid("value_unrepresentable requires a uint64 outside the int64 range")


def _encoded_size(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, allow_nan=False,
                              separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        _invalid("The snapshot must be finite UTF-8 JSON without NaN or Infinity")


def _snapshot(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        _invalid("The snapshot must be a JSON object")
    _encoded_size(value)
    allowed = {"snapshot_id", "source", "session_id", "process_id", "thread_id", "tick_count",
               "consistency", "objects", "truncated", "partial", "limits", "world_generation", "script_generation"}
    required = allowed - {"source", "limits", "world_generation", "script_generation"}
    if not required <= value.keys() or not value.keys() <= allowed:
        _invalid("Invalid snapshot field set")
    for key in ("snapshot_id", "session_id", "consistency"):
        _text(value[key], key)
    if "source" in value:
        _text(value["source"], "source")
    for key in ("process_id", "thread_id"):
        _integer(value[key], key, positive=True)
    _integer(value["tick_count"], "tick_count")
    for generation in ("world_generation", "script_generation"):
        if generation in value:
            _integer(value[generation], generation)
    for key in ("truncated", "partial"):
        if type(value[key]) is not bool:
            _invalid(f"{key}: a boolean value is required")
    if "limits" in value:
        limits = value["limits"]
        expected = {"objects", "properties_per_object", "scan_fields_per_object",
                    "property_value_bytes", "snapshot_bytes"}
        if not isinstance(limits, dict) or set(limits) != expected:
            _invalid("Invalid snapshot declared limit field set")
        for key in expected:
            if limits[key] is not None:
                _integer(limits[key], f"limits.{key}", positive=True)
    objects = value["objects"]
    if not isinstance(objects, list):
        _invalid("Selected snapshot objects must be a list")
    result: dict[str, dict[str, Any]] = {}
    for obj in objects:
        if not isinstance(obj, dict) or not obj.keys() <= {
            "handle", "path", "class", "properties", "error", "partial", "truncated"
        }:
            _invalid("Invalid object shape in the snapshot")
        _text(obj.get("handle"), "handle")
        if obj["handle"] in result:
            _invalid("The snapshot contains a duplicate object reference")
        if "error" in obj:
            _error(obj["error"])
        else:
            for key in ("path", "class"):
                _text(obj.get(key), key)
        for key in ("path", "class"):
            if key in obj:
                _text(obj[key], key)
        for key in ("partial", "truncated"):
            if key in obj and type(obj[key]) is not bool:
                _invalid(f"Object information {key}: a boolean value is required")
        properties = obj.get("properties", [] if "error" in obj else None)
        if not isinstance(properties, list):
            _invalid("Selected object properties must be a list")
        names: set[str] = set()
        for prop in properties:
            if not isinstance(prop, dict) or not prop.keys() <= {"name", "type", "supported", "value", "error", "enum"}:
                _invalid("Invalid property shape in the snapshot")
            _text(prop.get("name"), "Property name")
            if prop["name"] in names:
                _invalid("The object contains a duplicate property name")
            names.add(prop["name"])
            if type(prop.get("supported")) is not bool:
                _invalid("supported: a boolean value is required")
            if "type" in prop:
                _text(prop["type"], "Property type", optional=not prop["supported"])
            if "error" in prop:
                _error(prop["error"])
            if prop["supported"]:
                if "value" not in prop or "type" not in prop or "error" in prop:
                    _invalid("A supported property must have a type and value without an error")
                if prop["type"] in {"ObjectProperty", "ClassProperty", "WeakObjectProperty"}:
                    _object_value(prop["value"], value["session_id"], prop["type"])
                elif type(prop["value"]) not in {bool, int, float, str}:
                    _invalid("A supported value must be a scalar")
            elif "value" in prop and prop["value"] is not None:
                _invalid("An unsupported property must not claim that a value was read")
            if "enum" in prop:
                _enum_metadata(prop["enum"], prop)
            elif prop.get("type") == "EnumProperty" and prop["supported"] and (
                type(prop["value"]) is not int or not -(2**63) <= prop["value"] <= 2**64 - 1
            ):
                _invalid("An EnumProperty value must be an integer within the int64/uint64 range")
            if prop.get("type") == "NameProperty" and prop["supported"] and not isinstance(prop["value"], str):
                _invalid("A NameProperty value must be a string")
            if prop.get("type") == "ByteProperty" and prop["supported"] and (
                type(prop["value"]) is not int or not 0 <= prop["value"] <= 255
            ):
                _invalid("A ByteProperty value must be an integer between 0 and 255")
        result[obj["handle"]] = obj
    return result


def compare_snapshots(before: dict[str, Any], after: dict[str, Any], limit: int = 100) -> dict[str, Any]:
    """Compare selected scalar fields of copies without verifying their game provenance."""
    if type(limit) is not int or limit < 1:
        raise SnapshotError("invalid_limit", "The comparison limit must be a positive integer")
    old = _snapshot(before)
    new = _snapshot(after)
    old_incomplete = sum(bool(obj.get("partial") or obj.get("truncated")) for obj in old.values())
    new_incomplete = sum(bool(obj.get("partial") or obj.get("truncated")) for obj in new.values())
    result: dict[str, Any] = {
        "source": "copied_snapshots", "validation": "client_supplied_copies", "provenance": "unverified",
        "before_snapshot_id": before["snapshot_id"], "after_snapshot_id": after["snapshot_id"],
        "comparable": True, "reason": "same_session_process", "items": [], "total_items": 0,
        "metadata_annotations": [], "metadata_annotation_count": 0,
        "value_changes": 0, "type_changes": 0, "unknown_count": 0, "coverage_changes": 0,
        "truncated": False, "equal_selected_values": None,
        "coverage": {"before_partial": before["partial"], "after_partial": after["partial"],
                     "before_truncated": before["truncated"], "after_truncated": after["truncated"],
                     "before_targets": len(old), "after_targets": len(new),
                     "before_incomplete_objects": old_incomplete, "after_incomplete_objects": new_incomplete,
                     "complete": not any((before["partial"], after["partial"], before["truncated"],
                                          after["truncated"], old_incomplete, new_incomplete))},
    }
    for label, snapshot in (("before", before), ("after", after)):
        for generation in ("world_generation", "script_generation"):
            if generation in snapshot:
                result[f"{label}_{generation}"] = snapshot[generation]
    for field, reason in (("process_id", "different_process"), ("session_id", "different_session")):
        if before[field] != after[field]:
            result.update(comparable=False, reason=reason)
            result["coverage"]["complete"] = False
            return result
    if ("world_generation" in before and "world_generation" in after
            and before["world_generation"] != after["world_generation"]):
        result.update(comparable=False, reason="different_world_generation")
        result["coverage"]["complete"] = False
        return result
    if ("script_generation" in before and "script_generation" in after
            and before["script_generation"] != after["script_generation"]):
        result.update(comparable=False, reason="different_script_generation")
        result["coverage"]["complete"] = False
        return result

    entries: list[dict[str, Any]] = []
    annotations: list[dict[str, Any]] = []

    def annotate(handle: str, name: str, a: dict[str, Any], b: dict[str, Any]) -> None:
        left_enum, right_enum = a.get("enum"), b.get("enum")
        if left_enum is None and right_enum is None:
            return
        unavailable = any(metadata is None or metadata["name_status"] != "matched"
                          for metadata in (left_enum, right_enum))
        if unavailable or left_enum != right_enum:
            annotations.append({
                "kind": "enum_metadata_unavailable" if unavailable else "enum_metadata_changed",
                "handle": handle, "property": name, "before": left_enum, "after": right_enum,
            })

    def emit(kind: str, **fields: Any) -> None:
        entries.append({"kind": kind, **fields})
        if kind == "value_changed":
            result["value_changes"] += 1
        elif kind == "type_changed":
            result["type_changes"] += 1
        elif kind in {"coverage_added", "coverage_removed"}:
            result["coverage_changes"] += 1
        else:
            result["unknown_count"] += 1

    source_known = before.get("source") == after.get("source") == "live_native"
    if not source_known:
        result["coverage"]["complete"] = False
        emit("unknown", reason="source_not_claimed_live_native")
    if before["consistency"] != "single_tick_sequential" or after["consistency"] != "single_tick_sequential":
        result["coverage"]["complete"] = False
        emit("unknown", reason="unsupported_consistency")
        source_known = False

    for handle in sorted(old.keys() | new.keys()):
        if handle not in old or handle not in new:
            emit("coverage_added" if handle in new else "coverage_removed", handle=handle,
                 reason="selected_target_set_changed")
            continue
        left, right = old[handle], new[handle]
        if "error" in left or "error" in right:
            errors = [obj["error"]["code"] for obj in (left, right) if "error" in obj]
            kind = "object_unavailable" if any(code in {"stale_handle", "object_destroying"} for code in errors) else "unknown"
            emit(kind, handle=handle, reason="object_read_failed", before_error=left.get("error"),
                 after_error=right.get("error"))
            continue
        if any(left[key] != right[key] for key in ("path", "class")):
            emit("unknown", handle=handle, reason="identity_mismatch")
            continue
        left_props = {prop["name"]: prop for prop in left["properties"]}
        right_props = {prop["name"]: prop for prop in right["properties"]}
        for name in sorted(left_props.keys() | right_props.keys()):
            if name not in left_props or name not in right_props:
                emit("unknown", handle=handle, property=name, reason="selected_property_coverage_changed")
                continue
            a, b = left_props[name], right_props[name]
            annotate(handle, name, a, b)
            if not source_known or not a["supported"] or not b["supported"]:
                emit("unknown", handle=handle, property=name,
                     reason="source_not_claimed_live_native" if not source_known else "property_not_readable",
                     before_error=a.get("error"), after_error=b.get("error"))
                continue
            if a["type"] == b["type"] and a["type"] in {"ObjectProperty", "ClassProperty", "WeakObjectProperty"}:
                av, bv = a["value"], b["value"]
                if av is not None and bv is not None and av["handle"] == bv["handle"] and av != bv:
                    emit("unknown", handle=handle, property=name, reason="object_reference_identity_mismatch")
                    continue
                if av != bv:
                    emit("value_changed", handle=handle, property=name, before=av, after=bv)
                continue
            if a["type"] != b["type"] or _value_kind(a) != _value_kind(b):
                emit("type_changed", handle=handle, property=name,
                     before_type=a["type"], after_type=b["type"],
                     before_value_kind=_value_kind(a), after_value_kind=_value_kind(b))
            if type(a["value"]) is not type(b["value"]) or a["value"] != b["value"]:
                emit("value_changed", handle=handle, property=name, before=a["value"], after=b["value"])

    result["total_items"] = len(entries)
    result["metadata_annotation_count"] = len(annotations)
    result["coverage"]["complete"] &= not bool(result["unknown_count"] or result["coverage_changes"])
    result["equal_selected_values"] = (
        not bool(result["value_changes"] or result["type_changes"])
        if result["coverage"]["complete"] else None
    )
    result["items"] = entries[:limit]
    remaining = limit - len(result["items"])
    result["metadata_annotations"] = annotations[:remaining]
    result["truncated"] = (len(result["items"]) < len(entries)
                           or len(result["metadata_annotations"]) < len(annotations))
    # Copy all nested response values: the caller does not receive
    # mutable input snapshot elements, including error records.
    return json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
