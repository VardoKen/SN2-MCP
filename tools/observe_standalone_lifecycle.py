"""Read a captured Shipping lifecycle layout until exact process exit.

This tool never launches, stops, invokes or writes to the game. Supply the
current process identity and a source-correlated object/property layout.
Run it before requesting ordinary game shutdown in a separate process. Its
evidence is sequential memory sampling, not a hook or an atomic game snapshot.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
import time
from typing import Any

from sn2_mcp.process_observer import (
    ObservationError, ProcessLease, _Kernel32, _comparable_path,
    _expected_identity, _timestamp,
)

PROCESS_MEMORY_OBSERVER_ACCESS = 0x00101010
IDENTITY_KEYS = ("object", "object_class", "index", "name", "serial",
                 "class_index", "class_name", "class_serial")


class LayoutError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 0xffffffffffffffff) -> int:
    if isinstance(value, str):
        try:
            value = int(value, 0) if value.lower().startswith("0x") else int(value)
        except ValueError:
            raise LayoutError(f"{label} must be an integer") from None
    if type(value) is not int or not minimum <= value <= maximum:
        raise LayoutError(f"{label} is outside its required integer extent")
    return value


def number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LayoutError(f"{label} must be a finite number")
    return float(value)


def load_json(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise LayoutError("The input must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def process_identity(value: dict) -> dict:
    value = dict(value)
    created = value.get("creation_time", value.get("creation_filetime"))
    if type(created) is int or (isinstance(created, str) and created.isdecimal()):
        created = _timestamp(integer(created, "creation FILETIME", minimum=1, maximum=0x7fffffffffffffff))
    value["creation_time"] = created
    expected, _ = _expected_identity(value)
    return expected


def checked_layout(raw: dict, target: dict) -> dict:
    if type(raw.get("schema")) is not int or raw["schema"] != 1:
        raise LayoutError("Lifecycle layout schema must be 1")
    if integer(raw.get("process_id"), "layout process_id", minimum=1, maximum=0xffffffff) != target["pid"]:
        raise LayoutError("The layout belongs to a different process")
    _, target_creation = _expected_identity(target)
    if integer(raw.get("creation_filetime"), "layout creation_filetime", minimum=1) != target_creation:
        raise LayoutError("The layout belongs to a different process creation")
    registry = raw.get("registry")
    if not isinstance(registry, dict):
        raise LayoutError("The captured registry is required")
    base = integer(registry.get("module_base"), "module_base", minimum=1)
    rva = integer(registry.get("object_array_rva"), "object_array_rva", maximum=0xffffffff)
    if base + rva > 0xffffffffffffffff:
        raise LayoutError("The registry address overflows")
    result = {"schema": 1, "registry_address": base + rva, "objects": {}}
    objects = raw.get("objects")
    if not isinstance(objects, dict) or set(objects) != {"instance", "engine"}:
        raise LayoutError("Exactly the captured instance and engine objects are required")
    names: set[str] = set()
    for role, obj in objects.items():
        if not isinstance(obj, dict) or not isinstance(obj.get("identity"), dict):
            raise LayoutError("Each object requires its complete captured identity")
        identity = obj["identity"]
        if set(identity) != set(IDENTITY_KEYS):
            raise LayoutError("Each object identity requires all eight native identity fields")
        checked = {}
        for key in IDENTITY_KEYS:
            maximum = 0x7fffffff if key in {"index", "class_index", "serial", "class_serial"} else 0xffffffffffffffff
            checked[key] = integer(identity[key], f"{role}.{key}", maximum=maximum,
                                   minimum=1 if key in {"object", "object_class"} else 0)
        properties = obj.get("properties")
        if not isinstance(properties, list) or not properties:
            raise LayoutError("Each observed object requires property layouts")
        fields = []
        for prop in properties:
            if not isinstance(prop, dict) or not isinstance(prop.get("name"), str) or not prop["name"]:
                raise LayoutError("Each property requires a nonempty name")
            qualified = role + "." + prop["name"]
            if qualified in names:
                raise LayoutError("Observed property names must be unique")
            names.add(qualified)
            kind = prop.get("type")
            if kind not in {"bool", "float", "double"}:
                raise LayoutError("Only captured bool, float and double property layouts are supported")
            offset = integer(prop.get("offset"), qualified + ".offset", maximum=0x7fffffff)
            size = {"bool": 1, "float": 4, "double": 8}[kind]
            field = {"name": prop["name"], "qualified_name": qualified, "type": kind, "offset": offset, "size": size}
            if kind == "bool":
                field["byte_offset"] = integer(prop.get("byte_offset"), qualified + ".byte_offset", maximum=255)
                field["field_mask"] = integer(prop.get("field_mask"), qualified + ".field_mask", minimum=1, maximum=255)
                if field["field_mask"] != 255 and field["field_mask"] & (field["field_mask"] - 1):
                    raise LayoutError("The captured boolean mask must be 255 or one bit")
                for label in ("initial_value", "shutdown_value"):
                    if type(prop.get(label)) is not bool:
                        raise LayoutError(qualified + "." + label + " must be a boolean")
                    field[label] = prop[label]
            else:
                for label in ("initial_value", "shutdown_value"):
                    field[label] = number(prop.get(label), qualified + "." + label)
            if checked["object"] + offset + field.get("byte_offset", 0) + size > 0xffffffffffffffff:
                raise LayoutError("The captured property address overflows")
            fields.append(field)
        result["objects"][role] = {"identity": checked, "properties": fields}
    required = {"instance.Initialized", "instance.InitializationCount", "instance.ShutdownCount",
                "engine.FixedFrameRate", "engine.bUseFixedFrameRate"}
    if not required.issubset(names):
        raise LayoutError("The captured startup, shutdown counter and engine effect fields are required")
    fields = {prop["qualified_name"]: prop for obj in result["objects"].values() for prop in obj["properties"]}
    if fields["instance.Initialized"]["type"] != "bool" or fields["instance.Initialized"]["initial_value"] is not True or fields["instance.Initialized"]["shutdown_value"] is not False:
        raise LayoutError("Initialized must describe the required true to false transition")
    for name, initial, final in (("InitializationCount", 1.0, 1.0), ("ShutdownCount", 0.0, 1.0)):
        field = fields["instance." + name]
        if field["type"] != "double" or field["initial_value"] != initial or field["shutdown_value"] != final:
            raise LayoutError("The authored lifecycle counters require exactly one initialization and one shutdown")
    if fields["engine.FixedFrameRate"]["type"] != "float" or fields["engine.bUseFixedFrameRate"]["type"] != "bool":
        raise LayoutError("The engine effect requires its exact float and boolean layouts")
    if fields["engine.bUseFixedFrameRate"]["initial_value"] is not True:
        raise LayoutError("The selected startup effect requires fixed frame rate to be enabled")
    if all(fields[name]["initial_value"] == fields[name]["shutdown_value"]
           for name in ("engine.FixedFrameRate", "engine.bUseFixedFrameRate")):
        raise LayoutError("The selected engine effect must differ from its captured shutdown baseline")
    return result


class MemoryReader:
    def __init__(self, target: dict):
        self.kernel = _Kernel32()
        api = self.kernel.api
        api.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
        api.ReadProcessMemory.restype = wintypes.BOOL
        expected, created = _expected_identity(target)
        handle = api.OpenProcess(PROCESS_MEMORY_OBSERVER_ACCESS, False, expected["pid"])
        if handle in (None, 0, -1, ctypes.c_void_p(-1).value):
            raise ObservationError("process_capture_unavailable", "Could not retain the exact process for read-only memory sampling", self.kernel.error())
        try:
            pid, actual_created, executable = self.kernel.identity(handle)
            if pid != expected["pid"] or actual_created != created or _comparable_path(executable) != _comparable_path(expected["executable"]):
                raise ObservationError("process_identity_changed", "The PID, creation time or full EXE path changed")
        except BaseException:
            api.CloseHandle(handle)
            raise
        self.handle = handle
        self.lease = ProcessLease(self.kernel, handle, expected, created)

    def close(self) -> None:
        self.lease.close()

    def read(self, address: int, size: int) -> bytes:
        buffer = ctypes.create_string_buffer(size)
        read = ctypes.c_size_t()
        if not self.kernel.api.ReadProcessMemory(self.handle, address, buffer, size, ctypes.byref(read)) or read.value != size:
            raise ObservationError("memory_read_unavailable", "The captured process memory could not be read completely", self.kernel.error())
        return buffer.raw

    def slot(self, array: int, index: int) -> tuple[int, int]:
        header = self.read(array, 32)
        chunks = struct.unpack_from("<Q", header, 0)[0]
        count = struct.unpack_from("<i", header, 0x14)[0]
        chunk_count = struct.unpack_from("<i", header, 0x1c)[0]
        if not chunks or count < 0 or chunk_count < 0 or not 0 <= index < count or index >> 16 >= chunk_count:
            raise ObservationError("object_registry_changed", "The captured object slot is unavailable")
        chunk = struct.unpack("<Q", self.read(chunks + (index >> 16) * 8, 8))[0]
        if not chunk:
            raise ObservationError("object_registry_changed", "The captured object chunk is unavailable")
        item = self.read(chunk + (index & 65535) * 24, 24)
        return struct.unpack_from("<Q", item)[0], struct.unpack_from("<i", item, 0x10)[0]

    def identity(self, array: int, expected: dict) -> dict:
        pointer, serial = self.slot(array, expected["index"])
        if pointer != expected["object"]:
            raise ObservationError("object_identity_changed", "The captured object registry slot changed")
        obj = self.read(pointer, 32)
        flags = struct.unpack_from("<I", obj, 8)[0]
        index = struct.unpack_from("<i", obj, 12)[0]
        klass = struct.unpack_from("<Q", obj, 16)[0]
        name = struct.unpack_from("<Q", obj, 24)[0]
        if flags & 0x40018000 or not klass:
            raise ObservationError("object_identity_changed", "The captured UObject is no longer a valid live object")
        class_pointer, class_serial = self.slot(array, expected["class_index"])
        if class_pointer != klass:
            raise ObservationError("class_identity_changed", "The captured class registry slot changed")
        cls = self.read(klass, 32)
        if struct.unpack_from("<I", cls, 8)[0] & 0x40018000:
            raise ObservationError("class_identity_changed", "The captured class is no longer valid")
        current = {"object": pointer, "object_class": klass, "index": index, "name": name, "serial": serial,
                   "class_index": struct.unpack_from("<i", cls, 12)[0],
                   "class_name": struct.unpack_from("<Q", cls, 24)[0], "class_serial": class_serial}
        if current != expected:
            raise ObservationError("object_identity_changed", "The captured object or class identity changed")
        return current

    def sample(self, layout: dict) -> dict:
        array = layout["registry_address"]
        for obj in layout["objects"].values():
            self.identity(array, obj["identity"])
        values = {}
        for obj in layout["objects"].values():
            for field in obj["properties"]:
                address = obj["identity"]["object"] + field["offset"] + field.get("byte_offset", 0)
                raw = self.read(address, field["size"])
                value = bool(raw[0] & field["field_mask"]) if field["type"] == "bool" else struct.unpack("<f" if field["type"] == "float" else "<d", raw)[0]
                if field["type"] != "bool" and not math.isfinite(value):
                    raise ObservationError("property_value_unavailable", "A sampled property is not finite")
                values[field["qualified_name"]] = value
        for obj in layout["objects"].values():
            self.identity(array, obj["identity"])
        return values


def expected_values(layout: dict, phase: str) -> dict:
    return {field["qualified_name"]: field[phase + "_value"]
            for obj in layout["objects"].values() for field in obj["properties"]}


def values_match(actual: dict, expected: dict) -> bool:
    return actual.keys() == expected.keys() and all(
        actual[key] is value if isinstance(value, bool) else math.isclose(actual[key], value, rel_tol=1e-6, abs_tol=1e-6)
        for key, value in expected.items())


def observe(args: argparse.Namespace) -> dict:
    target_raw, target_hash = load_json(args.target_process)
    target = process_identity(target_raw)
    raw_layout, layout_hash = load_json(args.layout)
    layout = checked_layout(raw_layout, target)
    output = args.output.absolute()
    events_path = output.with_suffix(".events.jsonl")
    if not output.parent.is_dir():
        raise LayoutError("The output parent directory must exist")
    if output.exists() or events_path.exists():
        raise LayoutError("The output and event paths must be new files")
    report = {"kind": "sn2_standalone_lifecycle_memory_observation", "schema": 1,
              "state": "observing", "started_at": utc_now(), "target_process": target,
              "inputs": {"target_process": str(args.target_process.absolute()), "target_sha256": target_hash,
                         "layout": str(args.layout.absolute()), "layout_sha256": layout_hash},
              "method": "retained_exact_win32_handle_read_process_memory",
              "read_only": True, "game_start_requested": False, "game_stop_requested": False,
              "native_calls_performed": False, "events_invoked": False,
              "sampling_interval_ms": args.interval_ms, "timeout_seconds": args.timeout_seconds,
              "initial_state_observed": False, "shutdown_state_observed": False,
              "process_exit_confirmed": False, "samples": 0, "state_changes": 0,
              "maximum_sample_gap_ms": None,
              "events_path": str(events_path), "process_state": None,
              "zero_serial_aba_protection": False,
              "serial_coverage": {role: "object_and_class_nonzero" if obj["identity"]["serial"] and obj["identity"]["class_serial"] else "conditional_zero_serial"
                                  for role, obj in layout["objects"].items()},
              "limits": ["Sequential memory reads are not an atomic game-thread snapshot",
                         "A transition entirely between samples can be missed",
                         "A process exit alone does not prove that authored shutdown ran",
                         "No observer operation invokes shutdown; external shutdown method must be separately attributed",
                         "Matching zero serials cannot exclude ABA object reuse"]}
    reader = None
    start = time.monotonic()
    initial = expected_values(layout, "initial")
    final = expected_values(layout, "shutdown")
    previous = None
    previous_sample_time = None
    with events_path.open("x", encoding="utf-8", newline="\n") as events:
        def record(value: dict) -> None:
            value = {"utc": utc_now(), "elapsed_seconds": time.monotonic() - start, **value}
            events.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
            events.flush()
            report["state_changes"] += 1
        try:
            reader = MemoryReader(target)
            while time.monotonic() - start < args.timeout_seconds:
                state = reader.lease.poll()
                report["process_state"] = state
                if state["state"] != "running":
                    record({"type": "process_state", "state": state})
                    report["process_exit_confirmed"] = state["state"] == "exited"
                    break
                try:
                    values = reader.sample(layout)
                except ObservationError as error:
                    after = reader.lease.poll()
                    report["process_state"] = after
                    record({"type": "read_error", "code": error.code, "win32_error": error.win32_error, "process_state": after})
                    report["process_exit_confirmed"] = after["state"] == "exited"
                    report["read_error"] = {"code": error.code, "win32_error": error.win32_error}
                    if report["shutdown_state_observed"] and after["state"] == "running":
                        while time.monotonic() - start < args.timeout_seconds:
                            remaining = args.timeout_seconds - (time.monotonic() - start)
                            time.sleep(min(args.interval_ms / 1000, max(0.0, remaining)))
                            after = reader.lease.poll()
                            report["process_state"] = after
                            if after["state"] != "running":
                                record({"type": "process_state", "state": after})
                                report["process_exit_confirmed"] = after["state"] == "exited"
                                break
                    break
                report["samples"] += 1
                sampled_at = time.monotonic()
                if previous_sample_time is not None:
                    gap = (sampled_at - previous_sample_time) * 1000
                    report["maximum_sample_gap_ms"] = max(report["maximum_sample_gap_ms"] or 0.0, gap)
                previous_sample_time = sampled_at
                if report["samples"] == 1:
                    report["initial_state_observed"] = values_match(values, initial)
                    if not report["initial_state_observed"]:
                        record({"type": "initial_state_mismatch", "values": values, "expected": initial})
                        break
                    print(json.dumps({"observer_ready": True, "process": target, "values": values}), flush=True)
                    report["initial_sample_index"] = report["samples"]
                if values != previous:
                    record({"type": "values", "sample_index": report["samples"], "values": values})
                    previous = values
                if report["initial_state_observed"] and values_match(values, final):
                    report["shutdown_state_observed"] = True
                    report.setdefault("shutdown_observed_at", utc_now())
                    report.setdefault("shutdown_sample_index", report["samples"])
                remaining = args.timeout_seconds - (time.monotonic() - start)
                time.sleep(min(args.interval_ms / 1000, max(0.0, remaining)))
            report["state"] = "passed" if report["initial_state_observed"] and report["shutdown_state_observed"] and report["process_exit_confirmed"] else "unverified"
        except BaseException as error:
            report["state"] = "unverified"
            report["error"] = {"type": type(error).__name__, "code": getattr(error, "code", None), "message": str(error)}
            record({"type": "observer_error", "error": report["error"]})
            if isinstance(error, KeyboardInterrupt):
                report["interrupted"] = True
        finally:
            if reader is not None:
                try:
                    reader.close()
                    report["observer_handle_closed"] = True
                except ObservationError as error:
                    report["observer_handle_closed"] = False
                    report["close_error"] = error.code
                    report["state"] = "unverified"
            report["finished_at"] = utc_now()
            with output.open("x", encoding="utf-8", newline="\n") as summary:
                json.dump(report, summary, indent=2, ensure_ascii=False, allow_nan=False)
                summary.write("\n")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-process", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval-ms", type=float, default=5.0)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    if not math.isfinite(args.interval_ms) or args.interval_ms <= 0 or not math.isfinite(args.timeout_seconds) or not 0 < args.timeout_seconds <= 60:
        parser.error("The interval must be positive and the observation timeout must be between 0 and 60 seconds")
    try:
        report = observe(args)
        print(json.dumps(report, ensure_ascii=False, allow_nan=False))
        return 0 if report["state"] == "passed" else 2
    except (LayoutError, ObservationError, OSError, ValueError) as error:
        print(json.dumps({"code": getattr(error, "code", "lifecycle_observation_failed"), "message": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
