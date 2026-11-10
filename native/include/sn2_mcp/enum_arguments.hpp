#pragma once

#include "sn2_mcp/enum_values.hpp"
#include "sn2_mcp/transport.hpp"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <span>
#include <string_view>
#include <type_traits>

namespace sn2_mcp::enum_arguments {

struct EncodedInteger {
    sn2_mcp::enum_values::Integer value;
    std::array<std::byte, 8> bytes{};
    std::size_t size;
};

template<class T>
inline EncodedInteger encoded(T integer) {
    static_assert(std::is_integral_v<T> && !std::is_same_v<T, bool>);
    static_assert(sizeof(T) <= 8);
    static_assert(std::endian::native == std::endian::little);
    EncodedInteger result{{}, {}, sizeof(T)};
    if constexpr (std::is_unsigned_v<T>) result.value = static_cast<std::uint64_t>(integer);
    else result.value = static_cast<std::int64_t>(integer);
    std::memcpy(result.bytes.data(), &integer, sizeof(T));
    return result;
}

inline EncodedInteger encode_integer(std::string_view type, int element_size, const nlohmann::json& value) {
    sn2_mcp::enum_values::IntegerLayout layout{};
    try { layout = sn2_mcp::enum_values::integer_layout(type, element_size); }
    catch (const std::invalid_argument& error) {
        throw HandlerError(error.what(), "Unsupported numeric enum argument layout");
    }
    if (!value.is_number_integer())
        throw HandlerError("invalid_argument", "Enum or Byte requires an integer JSON number, excluding booleans and fractions");

    if (layout.is_unsigned) {
        std::uint64_t integer{};
        if (value.is_number_unsigned()) integer = value.get<std::uint64_t>();
        else {
            const auto signed_integer = value.get<std::int64_t>();
            if (signed_integer < 0) throw HandlerError("invalid_argument", "A negative number cannot be represented by an unsigned enum or Byte");
            integer = static_cast<std::uint64_t>(signed_integer);
        }
        if (layout.bytes == 1) {
            if (integer > std::numeric_limits<std::uint8_t>::max())
                throw HandlerError("invalid_argument", "The argument is outside the ByteProperty range");
            return encoded(static_cast<std::uint8_t>(integer));
        }
        if (layout.bytes == 4) {
            if (integer > std::numeric_limits<std::uint32_t>::max())
                throw HandlerError("invalid_argument", "The argument is outside the UInt32Property range");
            return encoded(static_cast<std::uint32_t>(integer));
        }
        return encoded(integer);
    }

    std::int64_t integer{};
    if (value.is_number_unsigned()) {
        const auto unsigned_integer = value.get<std::uint64_t>();
        if (unsigned_integer > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
            throw HandlerError("invalid_argument", "The unsigned number is outside the signed enum range");
        integer = static_cast<std::int64_t>(unsigned_integer);
    } else integer = value.get<std::int64_t>();
    if (layout.bytes == 4) {
        if (integer < std::numeric_limits<std::int32_t>::min() || integer > std::numeric_limits<std::int32_t>::max())
            throw HandlerError("invalid_argument", "The argument is outside the IntProperty range");
        return encoded(static_cast<std::int32_t>(integer));
    }
    return encoded(integer);
}

// Operate only on a validated UEnum.Names copy. Names, aliases and indices do not change the integer value.
inline bool contains_value(const sn2_mcp::enum_values::Integer& value,
                           std::span<const sn2_mcp::enum_values::Entry> entries) {
    std::int64_t signed_value{};
    if (!sn2_mcp::enum_values::label_value(value, signed_value)) return false;
    for (const auto& entry : entries) if (entry.value == signed_value) return true;
    return false;
}

} // namespace sn2_mcp::enum_arguments
