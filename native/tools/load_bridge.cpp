#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <Windows.h>
#include <TlHelp32.h>
#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
#include "../include/g1r_mcp/process_identity.hpp"

namespace fs = std::filesystem;
static_assert(sizeof(void*) == 8, "The loader requires Windows x64");
namespace {
struct Handle {
    HANDLE value{};
    ~Handle() { if (value && value != INVALID_HANDLE_VALUE) CloseHandle(value); }
    operator HANDLE() const { return value; }
};
std::wstring normalized(const fs::path& path) {
    return g1r::loader::comparable_path(fs::canonical(path).wstring());
}
bool suffix(const std::wstring& path, const std::wstring& end) {
    return path.size() >= end.size() && path.compare(path.size() - end.size(), end.size(), end) == 0;
}
std::wstring module_path(HMODULE module) {
    std::vector<wchar_t> buffer(32768);
    auto count = GetModuleFileNameW(module, buffer.data(), static_cast<DWORD>(buffer.size()));
    if (!count || count >= buffer.size()) throw std::runtime_error("module_path_failed");
    return std::wstring(buffer.data(), count);
}
struct PEIdentity { WORD machine{}; DWORD timestamp{}; DWORD image_size{}; };
PEIdentity pe_identity(const fs::path& path) {
    std::ifstream file(path, std::ios::binary);
    IMAGE_DOS_HEADER dos{}; file.read(reinterpret_cast<char*>(&dos), sizeof(dos));
    if (!file || dos.e_magic != IMAGE_DOS_SIGNATURE || dos.e_lfanew < sizeof(dos) || dos.e_lfanew > 1024 * 1024) throw std::runtime_error("invalid_pe");
    file.seekg(dos.e_lfanew); DWORD signature{}; IMAGE_FILE_HEADER header{};
    file.read(reinterpret_cast<char*>(&signature), sizeof(signature));
    file.read(reinterpret_cast<char*>(&header), sizeof(header));
    IMAGE_OPTIONAL_HEADER64 optional{}; file.read(reinterpret_cast<char*>(&optional), sizeof(optional));
    if (!file || signature != IMAGE_NT_SIGNATURE || optional.Magic != IMAGE_NT_OPTIONAL_HDR64_MAGIC || header.Machine != IMAGE_FILE_MACHINE_AMD64) throw std::runtime_error("not_x64_pe");
    return {header.Machine, header.TimeDateStamp, optional.SizeOfImage};
}
std::vector<MODULEENTRY32W> modules(DWORD pid) {
    Handle snapshot{CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)};
    if (snapshot.value == INVALID_HANDLE_VALUE) throw std::runtime_error("module_snapshot_failed");
    MODULEENTRY32W entry{}; entry.dwSize = sizeof(entry);
    std::vector<MODULEENTRY32W> result;
    if (!Module32FirstW(snapshot, &entry)) throw std::runtime_error("module_snapshot_empty");
    do { result.push_back(entry); entry.dwSize = sizeof(entry); } while (Module32NextW(snapshot, &entry));
    return result;
}
bool remote_identity_matches(HANDLE process, uintptr_t base, const PEIdentity& expected) {
    IMAGE_DOS_HEADER dos{}; SIZE_T received{};
    if (!ReadProcessMemory(process, reinterpret_cast<void*>(base), &dos, sizeof(dos), &received) || received != sizeof(dos) ||
        dos.e_magic != IMAGE_DOS_SIGNATURE || dos.e_lfanew < sizeof(dos) || dos.e_lfanew > 1024 * 1024) return false;
    IMAGE_NT_HEADERS64 headers{};
    if (!ReadProcessMemory(process, reinterpret_cast<void*>(base + dos.e_lfanew), &headers, sizeof(headers), &received) || received != sizeof(headers)) return false;
    return headers.Signature == IMAGE_NT_SIGNATURE && headers.OptionalHeader.Magic == IMAGE_NT_OPTIONAL_HDR64_MAGIC &&
        headers.FileHeader.Machine == expected.machine && headers.FileHeader.TimeDateStamp == expected.timestamp &&
        headers.OptionalHeader.SizeOfImage == expected.image_size;
}
bool already_loaded(const std::vector<MODULEENTRY32W>& list, const std::wstring& dll_path) {
    bool loaded = false;
    for (const auto& entry : list) {
        if (_wcsicmp(entry.szModule, L"G1R_MCP.dll") != 0) continue;
        if (normalized(entry.szExePath) != dll_path) throw std::runtime_error("other_bridge_loaded");
        loaded = true;
    }
    return loaded;
}
int error(const char* code, const char* message, bool unknown = false) {
    std::cout << "{\"ok\":false,\"code\":\"" << code << "\",\"message\":\"" << message << "\",\"outcome_unknown\":" << (unknown ? "true" : "false") << "}\n";
    return unknown ? 3 : 2;
}
}

int wmain(int argc, wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);
    try {
        DWORD pid{}; fs::path dll, expected_exe;
        std::uint64_t expected_created{};
        bool has_pid{}, has_dll{}, has_expected_exe{}, has_expected_created{};
        for (int index = 1; index < argc; ++index) {
            std::wstring argument = argv[index];
            if (argument == L"--pid" && index + 1 < argc) {
                if (has_pid) return error("invalid_arguments", "PID was specified more than once");
                has_pid = true;
                std::wstring value = argv[++index]; size_t consumed{}; auto parsed = std::stoull(value, &consumed);
                if (consumed != value.size() || parsed == 0 || parsed > MAXDWORD) throw std::runtime_error("invalid_pid");
                pid = static_cast<DWORD>(parsed);
            } else if (argument == L"--dll" && index + 1 < argc) {
                if (has_dll) return error("invalid_arguments", "DLL was specified more than once");
                has_dll = true; dll = argv[++index];
            } else if (argument == L"--expected-exe" && index + 1 < argc) {
                if (has_expected_exe) return error("invalid_arguments", "EXE was specified more than once");
                has_expected_exe = true; expected_exe = argv[++index];
            } else if (argument == L"--expected-creation-filetime" && index + 1 < argc) {
                if (has_expected_created) return error("invalid_arguments", "Creation time was specified more than once");
                has_expected_created = true; expected_created = g1r::loader::parse_creation_filetime(argv[++index]);
            } else return error("invalid_arguments", "Required: --pid PID --dll path; instance validation: --expected-exe path --expected-creation-filetime FILETIME");
        }
        if (has_expected_exe != has_expected_created ||
            (has_expected_exe && (expected_exe.empty() || !expected_exe.is_absolute())))
            return error("invalid_arguments", "Expected absolute EXE path and FILETIME must be supplied together");
        if (!pid || dll.empty() || !dll.is_absolute() || !fs::is_regular_file(dll) || _wcsicmp(dll.filename().c_str(), L"G1R_MCP.dll") != 0)
            return error("invalid_arguments", "An exact PID and absolute path to an existing G1R_MCP.dll are required");
        auto dll_path = normalized(dll); pe_identity(dll);
        Handle process{OpenProcess(PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_WRITE | PROCESS_VM_READ, FALSE, pid)};
        if (!process.value) return error("open_process_failed", "Could not open the game process");
        auto process_path = normalized(g1r::loader::process_image(process));
        if (has_expected_exe && !g1r::loader::identity_matches(process_path, g1r::loader::creation_filetime(process),
                                                           normalized(expected_exe), expected_created))
            return error("process_identity_mismatch", "The EXE path or creation time does not match the selected game instance");
        if (!suffix(process_path, L"\\g1r\\binaries\\win64\\g1r-win64-shipping.exe")) return error("wrong_process", "PID does not belong to the Gothic 1 Remake internal Win64 executable");
        auto identity = pe_identity(process_path); (void)identity;
        USHORT process_machine{}, native_machine{};
        auto wow64 = reinterpret_cast<BOOL(WINAPI*)(HANDLE, USHORT*, USHORT*)>(GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "IsWow64Process2"));
        if (!wow64 || !wow64(process, &process_machine, &native_machine) || process_machine != IMAGE_FILE_MACHINE_UNKNOWN || native_machine != IMAGE_FILE_MACHINE_AMD64)
            return error("wrong_architecture", "A native Windows x64 process is required");
        auto remote_modules = modules(pid);
        if (already_loaded(remote_modules, dll_path)) { std::cout << "{\"ok\":true,\"status\":\"already_loaded\",\"pid\":" << pid << "}\n"; return 0; }
        auto local_loader = GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "LoadLibraryW");
        MEMORY_BASIC_INFORMATION memory{};
        if (!local_loader || !VirtualQuery(local_loader, &memory, sizeof(memory))) return error("loader_address_failed", "Could not identify the LoadLibraryW module");
        auto local_module = static_cast<HMODULE>(memory.AllocationBase);
        auto local_path = normalized(module_path(local_module)); auto owner_name = fs::path(local_path).filename().wstring();
        auto local_identity = pe_identity(local_path);
        uintptr_t remote_base{};
        for (const auto& entry : remote_modules) {
            if (_wcsicmp(entry.szModule, owner_name.c_str()) != 0) continue;
            auto remote_path = normalized(entry.szExePath); auto remote_identity = pe_identity(remote_path);
            if (remote_path != local_path || remote_identity.timestamp != local_identity.timestamp || remote_identity.image_size != local_identity.image_size)
                return error("loader_module_mismatch", "The loader module in the process differs");
            remote_base = reinterpret_cast<uintptr_t>(entry.modBaseAddr); break;
        }
        auto offset = reinterpret_cast<uintptr_t>(local_loader) - reinterpret_cast<uintptr_t>(local_module);
        if (!remote_base || offset >= local_identity.image_size || !remote_identity_matches(process, remote_base, local_identity))
            return error("loader_module_missing", "A compatible system loader module was not found");
        auto remote_loader = reinterpret_cast<LPTHREAD_START_ROUTINE>(remote_base + offset);
        size_t bytes = (dll_path.size() + 1) * sizeof(wchar_t);
        void* remote_buffer = VirtualAllocEx(process, nullptr, bytes, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
        if (!remote_buffer) return error("allocation_failed", "Could not allocate the DLL path buffer");
        SIZE_T written{};
        if (!WriteProcessMemory(process, remote_buffer, dll_path.c_str(), bytes, &written) || written != bytes) { VirtualFreeEx(process, remote_buffer, 0, MEM_RELEASE); return error("write_failed", "Could not write the DLL path"); }
        Handle thread{CreateRemoteThread(process, nullptr, 0, remote_loader, remote_buffer, 0, nullptr)};
        if (!thread.value) { VirtualFreeEx(process, remote_buffer, 0, MEM_RELEASE); return error("thread_failed", "Could not start the DLL loader"); }
        auto wait = WaitForSingleObject(thread, 10000);
        if (wait != WAIT_OBJECT_0) return error("load_outcome_unknown", "DLL loading wait timed out; the outcome is unknown and no retry will be made", true);
        VirtualFreeEx(process, remote_buffer, 0, MEM_RELEASE);
        if (!already_loaded(modules(pid), dll_path)) return error("load_failed", "DLL was not found among loaded modules after the loader completed");
        std::cout << "{\"ok\":true,\"status\":\"loaded\",\"pid\":" << pid << "}\n"; return 0;
    } catch (const fs::filesystem_error&) {
        return error("path_check_failed", "Could not validate the file path");
    } catch (const std::exception& exception) {
        std::string code = exception.what();
        if (code.empty() || !std::all_of(code.begin(), code.end(), [](unsigned char value) { return (value >= 'a' && value <= 'z') || value == '_'; })) code = "validation_failed";
        return error(code.c_str(), "DLL validation or loading did not complete");
    }
}
