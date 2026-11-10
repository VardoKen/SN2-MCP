#pragma once

#include <sn2_mcp/linear_color_layout.hpp>
#include <sn2_mcp/scalar_arguments.hpp>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <span>

namespace sn2_mcp::linear_color_arguments {

struct EncodedLinearColor {
    alignas(4) std::array<std::byte,16> bytes{};
    int size{16};
};

static_assert(alignof(EncodedLinearColor) >= 4);

// Only a POD copy; the caller validates LinearColor identity.
// Values are not restricted to a color range and no gamma conversion is applied.
inline EncodedLinearColor encode(const linear_color_layout::Layout& layout,
    const nlohmann::json& value) {
    linear_color_layout::validate_layout(layout);
    if (!value.is_object() || value.size() != 4
        || !value.contains("R") || !value.contains("G")
        || !value.contains("B") || !value.contains("A"))
        throw HandlerError("invalid_argument", "FLinearColor requires only {R:number,G:number,B:number,A:number}");
    EncodedLinearColor result;
    for (const auto& component : layout.components) {
        const auto number = float_argument(value.at(component.name));
        std::memcpy(result.bytes.data() + component.offset, &number, sizeof(number));
    }
    return result;
}

inline nlohmann::json decode(const linear_color_layout::Layout& layout,
    std::span<const std::byte> bytes) {
    linear_color_layout::validate_layout(layout);
    if (bytes.size() != 16)
        throw HandlerError("invalid_struct_copy", "The FLinearColor copy must be 16 bytes");
    nlohmann::json result = nlohmann::json::object();
    for (const auto& component : layout.components) {
        float number{};
        std::memcpy(&number, bytes.data() + component.offset, sizeof(number));
        if (!std::isfinite(number))
            throw HandlerError("invalid_struct_value", "The FLinearColor component is not finite");
        result[component.name] = number;
    }
    return result;
}

} // namespace sn2_mcp::linear_color_arguments
