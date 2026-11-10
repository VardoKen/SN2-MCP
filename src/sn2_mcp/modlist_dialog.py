"""Discover and accept the game's native Slate mod-list mismatch dialog.

The instruction layouts and current-frame result relationship were confirmed by
the project's tools/native-mod-dialog utilities. Locations are derived from the
current PE code, without executable version, hash or address tables.
"""

from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path
import struct
from typing import Any


_PATTERNS = {
    "optional_store": "4c8b01488d4108493bc074174180780100740541c64001000fb60041880041c6400101",
    "execute": "4883ec284883c118e8????????488bc24883c428c3",
    "execute_if_safe": "488b5118488d4120483bc27413807a01007404c64201000fb6008802c6420101b001c3",
    "show_setup": "488d442430488945b8488d9508020000488d45b8488985f0010000488d8de8010000488d05????????488985e8010000e8????????",
    "is_set": "488b01807801000f94c0c3",
}


class ModlistDialogError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class _Module(c.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("th32ModuleID", w.DWORD), ("th32ProcessID", w.DWORD),
        ("GlblcntUsage", w.DWORD), ("ProccntUsage", w.DWORD),
        ("modBaseAddr", c.c_void_p), ("modBaseSize", w.DWORD),
        ("hModule", w.HMODULE), ("szModule", w.WCHAR * 256), ("szExePath", w.WCHAR * 260),
    ]


class _Thread(c.Structure):
    _fields_ = [
        ("dwSize", w.DWORD), ("cntUsage", w.DWORD), ("th32ThreadID", w.DWORD),
        ("th32OwnerProcessID", w.DWORD), ("tpBasePri", c.c_long),
        ("tpDeltaPri", c.c_long), ("dwFlags", w.DWORD),
    ]


class _ThreadBasic(c.Structure):
    _fields_ = [
        ("ExitStatus", c.c_long), ("TebBaseAddress", c.c_void_p),
        ("UniqueProcess", c.c_void_p), ("UniqueThread", c.c_void_p),
        ("AffinityMask", c.c_size_t), ("Priority", c.c_long), ("BasePriority", c.c_long),
    ]


class _Region(c.Structure):
    _fields_ = [
        ("BaseAddress", c.c_void_p), ("AllocationBase", c.c_void_p),
        ("AllocationProtect", w.DWORD), ("PartitionId", w.WORD),
        ("RegionSize", c.c_size_t), ("State", w.DWORD), ("Protect", w.DWORD), ("Type", w.DWORD),
    ]


class _Process:
    def __init__(self, pid: int, write: bool = False):
        if not hasattr(c, "WinDLL"):
            raise ModlistDialogError("unsupported_platform", "The native dialog reader requires Windows")
        self.pid = pid
        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.ntdll = c.WinDLL("ntdll", use_last_error=True)
        kernel = self.kernel
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.OpenThread.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenThread.restype = w.HANDLE
        kernel.CloseHandle.argtypes = [w.HANDLE]
        kernel.CloseHandle.restype = w.BOOL
        kernel.CreateToolhelp32Snapshot.argtypes = [w.DWORD, w.DWORD]
        kernel.CreateToolhelp32Snapshot.restype = w.HANDLE
        for name in ("Module32FirstW", "Module32NextW"):
            function = getattr(kernel, name)
            function.argtypes = [w.HANDLE, c.POINTER(_Module)]
            function.restype = w.BOOL
        for name in ("Thread32First", "Thread32Next"):
            function = getattr(kernel, name)
            function.argtypes = [w.HANDLE, c.POINTER(_Thread)]
            function.restype = w.BOOL
        kernel.ReadProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
        kernel.ReadProcessMemory.restype = w.BOOL
        kernel.WriteProcessMemory.argtypes = [w.HANDLE, c.c_void_p, c.c_void_p, c.c_size_t, c.POINTER(c.c_size_t)]
        kernel.WriteProcessMemory.restype = w.BOOL
        kernel.VirtualQueryEx.argtypes = [w.HANDLE, c.c_void_p, c.POINTER(_Region), c.c_size_t]
        kernel.VirtualQueryEx.restype = c.c_size_t
        self.ntdll.NtQueryInformationThread.argtypes = [w.HANDLE, c.c_ulong, c.c_void_p, c.c_ulong, c.POINTER(c.c_ulong)]
        self.ntdll.NtQueryInformationThread.restype = c.c_long
        self.handle = kernel.OpenProcess(0x410 | (0x28 if write else 0), False, pid)
        if not self.handle:
            raise ModlistDialogError("open_process_failed", "Could not open the game process (winerror=" + str(c.get_last_error()) + ")", {"pid": pid})

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def read(self, address: int, size: int, *, exact: bool = True) -> bytes:
        buffer = c.create_string_buffer(size)
        received = c.c_size_t()
        succeeded = self.kernel.ReadProcessMemory(self.handle, address, buffer, size, c.byref(received))
        if exact and (not succeeded or received.value != size):
            raise ModlistDialogError("memory_read_failed", "Cannot read the current process memory", {"address": hex(address), "size": size})
        return buffer.raw[:received.value]

    def qword(self, address: int) -> int:
        return struct.unpack("<Q", self.read(address, 8))[0]

    def snapshot(self, flags: int, pid: int) -> Any:
        handle = self.kernel.CreateToolhelp32Snapshot(flags, pid)
        if handle == c.c_void_p(-1).value:
            raise ModlistDialogError("snapshot_failed", "Could not enumerate the game process (winerror=" + str(c.get_last_error()) + ")")
        return handle

    def module(self) -> tuple[int, int, str]:
        snapshot = self.snapshot(0x18, self.pid)
        try:
            module = _Module()
            module.dwSize = c.sizeof(module)
            present = self.kernel.Module32FirstW(snapshot, c.byref(module))
            while present:
                if module.szModule.lower() == "g1r-win64-shipping.exe":
                    return int(module.modBaseAddr), int(module.modBaseSize), module.szExePath
                present = self.kernel.Module32NextW(snapshot, c.byref(module))
        finally:
            self.kernel.CloseHandle(snapshot)
        raise ModlistDialogError("shipping_module_missing", "The game executable module was not found", {"pid": self.pid})

    def threads(self) -> list[int]:
        snapshot = self.snapshot(0x4, 0)
        result: list[int] = []
        try:
            thread = _Thread()
            thread.dwSize = c.sizeof(thread)
            present = self.kernel.Thread32First(snapshot, c.byref(thread))
            while present:
                if thread.th32OwnerProcessID == self.pid:
                    result.append(int(thread.th32ThreadID))
                present = self.kernel.Thread32Next(snapshot, c.byref(thread))
        finally:
            self.kernel.CloseHandle(snapshot)
        return result

    def stack(self, tid: int) -> tuple[int, int] | None:
        handle = self.kernel.OpenThread(0x40, False, tid)
        if not handle:
            return None
        try:
            basic = _ThreadBasic()
            needed = c.c_ulong()
            result = self.ntdll.NtQueryInformationThread(handle, 0, c.byref(basic), c.sizeof(basic), c.byref(needed))
            if result != 0 or int(basic.UniqueProcess or 0) != self.pid or int(basic.UniqueThread or 0) != tid:
                return None
            tib = self.read(int(basic.TebBaseAddress), 0x20, exact=False)
            if len(tib) != 0x20:
                return None
            base, limit = struct.unpack_from("<QQ", tib, 8)
            return (limit, base) if limit < base else None
        finally:
            self.kernel.CloseHandle(handle)

    def region(self, address: int) -> _Region | None:
        region = _Region()
        result = self.kernel.VirtualQueryEx(self.handle, address, c.byref(region), c.sizeof(region))
        return region if result == c.sizeof(region) else None


def _pattern_matches(data: bytes, pattern: str) -> list[int]:
    tokens = [pattern[index:index + 2] for index in range(0, len(pattern), 2)]
    fixed = [(index, int(token, 16)) for index, token in enumerate(tokens) if token != "??"]
    anchor = bytes(value for index, value in fixed if index < next((i for i, token in enumerate(tokens) if token == "??"), len(tokens)))
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = data.find(anchor, cursor)
        if offset < 0:
            return offsets
        if offset + len(tokens) <= len(data) and all(data[offset + index] == value for index, value in fixed):
            offsets.append(offset)
        cursor = offset + 1


def _code_locations(path: str) -> tuple[dict[str, int], dict[str, bytes]]:
    image = Path(path)
    identity = image.stat()
    return _cached_code_locations(str(image.resolve()), identity.st_mtime_ns, identity.st_size)


@lru_cache(maxsize=8)
def _cached_code_locations(path: str, mtime_ns: int, file_size: int) -> tuple[dict[str, int], dict[str, bytes]]:
    data = Path(path).read_bytes()
    if data[:2] != b"MZ":
        raise ModlistDialogError("invalid_pe", "The executable has no DOS header")
    pe = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe:pe + 4] != b"PE\0\0" or struct.unpack_from("<H", data, pe + 4)[0] != 0x8664:
        raise ModlistDialogError("invalid_pe", "The executable is not a Windows x64 PE")
    count = struct.unpack_from("<H", data, pe + 6)[0]
    optional_size = struct.unpack_from("<H", data, pe + 20)[0]
    section_table = pe + 24 + optional_size
    matches: dict[str, list[tuple[int, bytes]]] = {name: [] for name in _PATTERNS}
    for index in range(count):
        header = section_table + index * 40
        virtual_address, raw_size, raw_pointer = struct.unpack_from("<III", data, header + 12)
        characteristics = struct.unpack_from("<I", data, header + 36)[0]
        if not characteristics & 0x20000000:
            continue
        code = data[raw_pointer:raw_pointer + raw_size]
        for name, pattern in _PATTERNS.items():
            length = len(pattern) // 2
            for offset in _pattern_matches(code, pattern):
                matches[name].append((virtual_address + offset, code[offset:offset + length]))
    ambiguous = {name: len(values) for name, values in matches.items() if len(values) != 1}
    if ambiguous:
        raise ModlistDialogError("code_layout_unavailable", "The native dialog instruction layouts were not uniquely identified", ambiguous)
    locations = {name: values[0][0] for name, values in matches.items()}
    observed = {name: values[0][1] for name, values in matches.items()}
    setup = observed["show_setup"]
    lea_offset = setup.index(b"\x48\x8d\x05")
    is_set_target = locations["show_setup"] + lea_offset + 7 + struct.unpack_from("<i", setup, lea_offset + 3)[0]
    execute_target = locations["execute"] + 13 + struct.unpack_from("<i", observed["execute"], 9)[0]
    if is_set_target != locations["is_set"] or execute_target != locations["optional_store"]:
        raise ModlistDialogError("code_layout_unavailable", "The modal setup and delegate do not reference the confirmed optional-result routines")
    locations["modal_return"] = locations["show_setup"] + len(setup)
    return locations, observed


def _discover(process: _Process) -> dict[str, Any]:
    base, image_size, path = process.module()
    locations, observed = _code_locations(path)
    for name, code in observed.items():
        if process.read(base + locations[name], len(code)) != code:
            raise ModlistDialogError("live_code_changed", "The loaded dialog code differs from its current executable", {"function": name})
    modal_return = base + locations["modal_return"]
    needle = struct.pack("<Q", modal_return)
    active: list[dict[str, Any]] = []
    thread_ids = process.threads()
    for tid in thread_ids:
        stack = process.stack(tid)
        if stack is None:
            continue
        limit, stack_base = stack
        cursor = limit
        while cursor < stack_base:
            region = process.region(cursor)
            if region is None:
                break
            end = min(stack_base, int(region.BaseAddress or 0) + region.RegionSize)
            if end <= cursor:
                break
            if region.State == 0x1000 and not region.Protect & (0x100 | 0x01):
                chunk = process.read(cursor, end - cursor, exact=False)
                offset = chunk.find(needle)
                while offset >= 0:
                    return_slot = cursor + offset
                    target = return_slot + 0x38
                    if return_slot % 8 == 0 and target + 2 <= stack_base:
                        value = process.read(target, 2, exact=False)
                        if len(value) == 2 and value[1] == 0:
                            if process.qword(return_slot) == modal_return and process.read(target, 2)[1] == 0:
                                active.append({"tid": tid, "return_slot": hex(return_slot), "result_target": hex(target), "choice": value[0], "is_set": False})
                    offset = chunk.find(needle, offset + 1)
            cursor = end
    return {
        "pid": process.pid,
        "status": "active" if len(active) == 1 else "ambiguous" if active else "not_active",
        "active": len(active) == 1,
        "thread_count": len(thread_ids),
        "frames": active,
        "executable": path,
        "image_base": hex(base),
        "image_size": image_size,
        "modal_return": hex(modal_return),
        "method": "native_slate_current_show_frame",
    }


def status(pid: int) -> dict[str, Any]:
    process = _Process(pid)
    try:
        return _discover(process)
    finally:
        process.close()


def accept_continue(pid: int) -> dict[str, Any]:
    process = _Process(pid, write=True)
    report: dict[str, Any] = {"pid": pid, "sent": False, "confirmed": False, "write_began": False, "writes": []}
    try:
        discovery = _discover(process)
        report["dialog"] = discovery
        if not discovery["active"]:
            return {**report, "status": discovery["status"]}
        frame = discovery["frames"][0]
        return_slot = int(frame["return_slot"], 16)
        target = int(frame["result_target"], 16)
        expected_return = int(discovery["modal_return"], 16)
        stack = process.stack(frame["tid"])
        region = process.region(target)
        if stack is None or not stack[0] <= return_slot < target + 2 <= stack[1]:
            raise ModlistDialogError("frame_changed", "The modal result is no longer in the current thread stack")
        if region is None or region.State != 0x1000 or region.Protect & 0x100 or region.Protect & 0xFF not in (0x04, 0x08, 0x40, 0x80):
            raise ModlistDialogError("result_not_writable", "The current modal result is not in writable committed memory")
        if process.qword(return_slot) != expected_return or process.read(target, 2)[1] != 0:
            raise ModlistDialogError("frame_changed", "The current modal frame or result changed before publication")
        buffer = c.create_string_buffer(b"\x01", 1)
        for label, address in (("choice", target), ("is_set", target + 1)):
            if label == "is_set" and (process.qword(return_slot) != expected_return or process.read(target, 2) != b"\x01\x00"):
                raise ModlistDialogError("frame_changed", "The modal result changed after the choice store")
            written = c.c_size_t()
            report["write_began"] = True
            succeeded = process.kernel.WriteProcessMemory(process.handle, address, buffer, 1, c.byref(written))
            report["writes"].append({"field": label, "value": 1, "success": bool(succeeded), "bytes_written": written.value})
            if not succeeded or written.value != 1:
                raise ModlistDialogError("memory_write_failed", "The modal result byte could not be published", {"field": label})
        report.update(status="continue_published", sent=True, confirmed=False, do_not_retry=True)
        return report
    except Exception as exc:
        if report["write_began"]:
            report.update(status="write_failed_do_not_retry", do_not_retry=True)
            message = (f"Dialog file operation failed (errno={exc.errno}, winerror={getattr(exc, 'winerror', None)})"
                       if isinstance(exc, OSError) else str(exc))
            report["error"] = {"code": getattr(exc, "code", "publication_failed"), "message": message, "details": getattr(exc, "details", {})}
            return report
        raise
    finally:
        process.close()
