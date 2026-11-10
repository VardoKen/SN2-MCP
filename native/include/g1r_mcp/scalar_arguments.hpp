#pragma once

#include <g1r_mcp/transport.hpp>
#include <cmath>
#include <algorithm>
#include <cstdint>
#include <limits>

namespace g1r_mcp {

inline int parameter_extent(int current, int offset, int size, int array_dim) {
    if (current<0 || offset<0 || size<1
        || offset>std::numeric_limits<int>::max()-size || array_dim!=1)
        throw HandlerError("unsupported_signature", "Invalid reflected parameters");
    return std::max(current,offset+size);
}

inline void validate_bool_layout(int size, std::uint8_t byte_offset, std::uint8_t mask) {
    if (size < 1 || byte_offset >= size || mask == 0)
        throw HandlerError("unsupported_property_layout", "BoolProperty exceeds the field bounds or has an empty mask");
}

inline std::int32_t int32_argument(const nlohmann::json& value) {
    if (!value.is_number_integer())
        throw HandlerError("invalid_argument", "An integer int32 argument is required");
    if (value.is_number_unsigned()) {
        const auto integer = value.get<std::uint64_t>();
        if (integer > static_cast<std::uint64_t>(std::numeric_limits<std::int32_t>::max()))
            throw HandlerError("invalid_argument", "The argument is outside the int32 range");
        return static_cast<std::int32_t>(integer);
    }
    const auto integer = value.get<std::int64_t>();
    if (integer < std::numeric_limits<std::int32_t>::min()
        || integer > std::numeric_limits<std::int32_t>::max())
        throw HandlerError("invalid_argument", "The argument is outside the int32 range");
    return static_cast<std::int32_t>(integer);
}

inline std::int64_t int64_argument(const nlohmann::json& value) {
    if (!value.is_number_integer())
        throw HandlerError("invalid_argument", "An integer int64 argument is required");
    if (value.is_number_unsigned()) {
        const auto integer=value.get<std::uint64_t>();
        if(integer>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
            throw HandlerError("invalid_argument", "The argument is outside the int64 range");
        return static_cast<std::int64_t>(integer);
    }
    return value.get<std::int64_t>();
}

inline std::uint64_t uint64_argument(const nlohmann::json& value) {
    if (!value.is_number_integer())
        throw HandlerError("invalid_argument", "An integer uint64 argument is required");
    if(value.is_number_unsigned())return value.get<std::uint64_t>();
    const auto integer=value.get<std::int64_t>();
    if(integer<0)throw HandlerError("invalid_argument", "A negative argument cannot be represented by uint64");
    return static_cast<std::uint64_t>(integer);
}

inline std::uint32_t uint32_argument(const nlohmann::json& value) {
    const auto integer=uint64_argument(value);
    if(integer>std::numeric_limits<std::uint32_t>::max())
        throw HandlerError("invalid_argument", "The argument is outside the uint32 range");
    return static_cast<std::uint32_t>(integer);
}

inline double double_argument(const nlohmann::json& value) {
    if (!value.is_number()) throw HandlerError("invalid_argument", "A numeric double argument is required");
    const auto converted = value.get<double>();
    if (!std::isfinite(converted))
        throw HandlerError("invalid_argument", "A finite double argument is required");
    return converted;
}

inline float float_argument(const nlohmann::json& value) {
    const auto converted = double_argument(value);
    constexpr auto maximum = static_cast<double>(std::numeric_limits<float>::max());
    if (converted > maximum || converted < -maximum)
        throw HandlerError("invalid_argument", "The argument is outside the finite float range");
    const auto result = static_cast<float>(converted);
    if (!std::isfinite(result))
        throw HandlerError("invalid_argument", "A finite float argument is required");
    return result;
}

} // namespace g1r_mcp
