#pragma once

#include <g1r_mcp/scalar_arguments.hpp>
#include <g1r_mcp/struct_values.hpp>
#include <g1r_mcp/vector_layout.hpp>
#include <array>
#include <cstddef>
#include <cstring>
#include <span>
#include <string>

namespace g1r_mcp::vector_arguments {

// Only an owned POD copy. The caller confirms UScriptStruct identity.
struct EncodedVector {
    std::array<std::byte,24> bytes{};
    int size{};
};

inline void validate_layout(const struct_values::Layout& layout) {
    vector_layout::validate_layout(layout);
}

inline EncodedVector encode(const struct_values::Layout& layout,const nlohmann::json& value) {
    vector_arguments::validate_layout(layout);
    if(!value.is_object() || value.size()!=3 || !value.contains("X") || !value.contains("Y") || !value.contains("Z"))
        throw HandlerError("invalid_argument","FVector requires only {X:number,Y:number,Z:number}");
    EncodedVector result;
    result.size=layout.properties_size;
    for(const auto& component:layout.components) {
        const auto& input=value.at(component.name);
        if(component.element_size==4) {
            // Rounding follows the existing scalar float input policy.
            const auto number=float_argument(input);
            std::memcpy(result.bytes.data()+component.offset,&number,sizeof(number));
        } else {
            const auto number=double_argument(input);
            std::memcpy(result.bytes.data()+component.offset,&number,sizeof(number));
        }
    }
    return result;
}

inline nlohmann::json decode(const struct_values::Layout& layout,std::span<const std::byte> bytes) {
    vector_arguments::validate_layout(layout);
    const auto components=struct_values::copied_components(layout,bytes);
    nlohmann::json result=nlohmann::json::object();
    for(const auto& component:components)
        result[component.at("name").get<std::string>()]=component.at("value");
    return result;
}

} // namespace g1r_mcp::vector_arguments
