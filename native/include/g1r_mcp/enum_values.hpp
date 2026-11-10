#pragma once

#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>
#include <variant>
#include <vector>

namespace g1r::enum_values {

// Read only supplied copies. The caller validates UObject and UEnum.
using Integer = std::variant<std::int64_t, std::uint64_t>;

struct IntegerLayout {
    std::size_t bytes;
    bool is_unsigned;
};

inline IntegerLayout integer_layout(std::string_view type, int element_size) {
    IntegerLayout result{};
    if (type == "IntProperty") result = {4, false};
    else if (type == "Int64Property") result = {8, false};
    else if (type == "UInt32Property") result = {4, true};
    else if (type == "UInt64Property") result = {8, true};
    else if (type == "ByteProperty") result = {1, true};
    else throw std::invalid_argument("unsupported_enum_underlying_type");
    if (element_size < 0 || result.bytes != static_cast<std::size_t>(element_size))
        throw std::invalid_argument("unsupported_enum_underlying_layout");
    return result;
}

template<class T> inline T copied_value(std::span<const std::byte> bytes) {
    static_assert(std::is_trivially_copyable_v<T>);
    static_assert(std::endian::native == std::endian::little);
    if (bytes.size() != sizeof(T)) throw std::invalid_argument("invalid_enum_value_bytes");
    T result{};
    std::memcpy(&result, bytes.data(), sizeof(T));
    return result;
}

inline Integer decode_integer(std::string_view type, int element_size, std::span<const std::byte> bytes) {
    const auto layout = integer_layout(type, element_size);
    if (bytes.size() != layout.bytes) throw std::invalid_argument("invalid_enum_value_bytes");
    if (type == "IntProperty") return static_cast<std::int64_t>(copied_value<std::int32_t>(bytes));
    if (type == "Int64Property") return copied_value<std::int64_t>(bytes);
    if (type == "UInt32Property") return static_cast<std::uint64_t>(copied_value<std::uint32_t>(bytes));
    if (type == "UInt64Property") return copied_value<std::uint64_t>(bytes);
    return static_cast<std::uint64_t>(copied_value<std::uint8_t>(bytes));
}

inline bool label_value(const Integer& value, std::int64_t& result) noexcept {
    if (const auto* signed_value = std::get_if<std::int64_t>(&value)) {
        result = *signed_value;
        return true;
    }
    const auto unsigned_value = std::get<std::uint64_t>(value);
    if (unsigned_value > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())) return false;
    result = static_cast<std::int64_t>(unsigned_value);
    return true;
}

// MemberVariableLayout: TTuple<FName,int64>; FName is 8 bytes and each copied table entry is 16 bytes.
struct Entry {
    std::uint64_t name;
    std::int64_t value;
};
static_assert(sizeof(Entry) == 16 && offsetof(Entry, value) == 8);

inline std::size_t table_byte_size(std::int32_t count, std::int32_t capacity, bool has_data) {
    if (count < 0 || capacity < count || (count > 0 && !has_data)
        || static_cast<std::size_t>(count) > std::numeric_limits<std::size_t>::max() / sizeof(Entry))
        throw std::invalid_argument("invalid_enum_names_table");
    return static_cast<std::size_t>(count) * sizeof(Entry);
}

inline std::vector<Entry> decode_entries(std::span<const std::byte> bytes, std::int32_t count,
                                         std::int32_t capacity) {
    const auto expected = table_byte_size(count, capacity, !bytes.empty());
    if (bytes.size() != expected) throw std::invalid_argument("invalid_enum_names_bytes");
    std::vector<Entry> result(static_cast<std::size_t>(count));
    if (expected) std::memcpy(result.data(), bytes.data(), expected);
    return result;
}

inline bool valid_label(std::string_view value) noexcept {
    if (value.empty()) return false;
    std::size_t position = 0;
    while (position < value.size()) {
        const auto first = static_cast<unsigned char>(value[position++]);
        if (first == 0) return false;
        if (first < 0x80) continue;
        int trailing = 0;
        std::uint32_t codepoint = 0, minimum = 0;
        if (first >= 0xC2 && first <= 0xDF) { trailing = 1; codepoint = first & 0x1F; minimum = 0x80; }
        else if (first >= 0xE0 && first <= 0xEF) { trailing = 2; codepoint = first & 0x0F; minimum = 0x800; }
        else if (first >= 0xF0 && first <= 0xF4) { trailing = 3; codepoint = first & 7; minimum = 0x10000; }
        else return false;
        if (static_cast<std::size_t>(trailing) > value.size() - position) return false;
        while (trailing--) {
            const auto byte = static_cast<unsigned char>(value[position++]);
            if ((byte & 0xC0) != 0x80) return false;
            codepoint = (codepoint << 6) | (byte & 0x3F);
        }
        if (codepoint < minimum || codepoint > 0x10FFFF || (codepoint >= 0xD800 && codepoint <= 0xDFFF)) return false;
    }
    return true;
}

struct LabelMatch {
    std::string status; // matched, not_found, value_unrepresentable or unavailable
    std::string name;
    std::size_t matched_count = 0;
    bool ambiguous = false;
    std::string reason;
};

template<class NameDecoder>
inline LabelMatch find_label(const Integer& value, std::span<const Entry> entries, NameDecoder&& decode_name) {
    std::int64_t signed_value{};
    if (!label_value(value, signed_value)) return {"value_unrepresentable", "", 0, false, "unsigned_outside_int64"};
    std::size_t first_match = 0, count = 0;
    for (std::size_t index = 0; index < entries.size(); ++index) {
        if (entries[index].value != signed_value) continue;
        if (!count) first_match = index;
        ++count;
    }
    if (!count) return {"not_found", "", 0, false, ""};
    try {
        auto label = std::invoke(std::forward<NameDecoder>(decode_name), entries[first_match].name);
        if (!valid_label(label)) return {"unavailable", "", count, count > 1, "invalid_enum_label"};
        return {"matched", std::move(label), count, count > 1, ""};
    } catch (...) {
        return {"unavailable", "", count, count > 1, "enum_name_decode_failed"};
    }
}

} // namespace g1r::enum_values
