#pragma once
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#include <algorithm>
#include <cstdint>
#include <cwctype>
#include <limits>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace g1r::loader {

inline std::uint64_t parse_creation_filetime(std::wstring_view text) {
    if (text.empty() || text.size() > 20) throw std::runtime_error("invalid_creation_filetime");
    std::uint64_t value{};
    for (wchar_t digit : text) {
        if (digit < L'0' || digit > L'9') throw std::runtime_error("invalid_creation_filetime");
        const auto part = static_cast<std::uint64_t>(digit - L'0');
        if (value > (std::numeric_limits<std::uint64_t>::max() - part) / 10)
            throw std::runtime_error("invalid_creation_filetime");
        value = value * 10 + part;
    }
    if (!value) throw std::runtime_error("invalid_creation_filetime");
    return value;
}

inline std::uint64_t filetime_value(const FILETIME& value) noexcept {
    return static_cast<std::uint64_t>(value.dwHighDateTime) << 32 | value.dwLowDateTime;
}

inline std::uint64_t creation_filetime(HANDLE process) {
    FILETIME created{}, exited{}, kernel{}, user{};
    if (!GetProcessTimes(process, &created, &exited, &kernel, &user))
        throw std::runtime_error("process_identity_failed");
    return filetime_value(created);
}

inline std::wstring process_image(HANDLE process) {
    std::vector<wchar_t> text(32768);
    DWORD length = static_cast<DWORD>(text.size());
    if (!QueryFullProcessImageNameW(process, 0, text.data(), &length) || !length || length >= text.size())
        throw std::runtime_error("process_identity_failed");
    return std::wstring(text.data(), length);
}

// Compare already resolved paths without looking up the process by PID again.
inline std::wstring comparable_path(std::wstring text) {
    std::replace(text.begin(), text.end(), L'/', L'\\');
    if (text.starts_with(L"\\\\?\\UNC\\")) text = L"\\\\" + text.substr(8);
    else if (text.starts_with(L"\\\\?\\")) text.erase(0, 4);
    std::transform(text.begin(), text.end(), text.begin(), [](wchar_t value) {
        return static_cast<wchar_t>(towlower(value));
    });
    return text;
}

inline bool identity_matches(std::wstring_view actual_image, std::uint64_t actual_created,
                             std::wstring_view expected_image, std::uint64_t expected_created) {
    if (actual_created != expected_created) return false;
    const auto actual = comparable_path(std::wstring(actual_image));
    const auto expected = comparable_path(std::wstring(expected_image));
    if (actual.empty() || expected.empty() || actual.size() > 32768 || expected.size() > 32768) return false;
    return CompareStringOrdinal(actual.data(), static_cast<int>(actual.size()),
                                expected.data(), static_cast<int>(expected.size()), TRUE) == CSTR_EQUAL;
}

} // namespace g1r::loader
