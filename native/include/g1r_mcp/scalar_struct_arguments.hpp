#pragma once

#include <g1r_mcp/scalar_arguments.hpp>
#include <g1r_mcp/scalar_struct_values.hpp>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <vector>

namespace g1r_mcp::scalar_struct_arguments {

// Encode only an owned POD copy; the caller confirms the captured struct identity.
inline std::vector<std::byte> encode(const scalar_struct_values::Layout& layout,
                                    const nlohmann::json& value) {
    scalar_struct_values::validate_layout(layout);
    if(!value.is_object() || value.size()!=layout.components.size())
        throw HandlerError("invalid_argument","Scalar DTO requires exactly the reflected field names");
    for(const auto& component:layout.components) {
        if(!value.contains(component.name))
            throw HandlerError("invalid_argument","Scalar DTO requires exactly the reflected field names");
    }

    std::vector<std::byte> result(static_cast<std::size_t>(layout.properties_size),std::byte{});
    for(const auto& component:layout.components) {
        const auto& field=component.layout;
        const auto& input=value.at(component.name);
        auto* destination=result.data()+component.offset;
        const auto write=[destination](auto number) {
            std::memcpy(destination,&number,sizeof(number));
        };
        if(field.type=="BoolProperty") {
            if(!input.is_boolean())
                throw HandlerError("invalid_argument","A boolean DTO field is required");
            validate_bool_layout(field.element_size,field.byte_offset,field.byte_mask);
            auto& byte=destination[field.byte_offset];
            const auto previous=std::to_integer<std::uint8_t>(byte);
            const auto mask=field.byte_mask;
            byte=static_cast<std::byte>((previous&static_cast<std::uint8_t>(~mask))
                | (input.get<bool>()?mask:std::uint8_t{}));
        } else if(field.type=="IntProperty") {
            write(int32_argument(input));
        } else if(field.type=="Int64Property") {
            write(int64_argument(input));
        } else if(field.type=="UInt32Property") {
            write(uint32_argument(input));
        } else if(field.type=="UInt64Property") {
            write(uint64_argument(input));
        } else if(field.type=="FloatProperty") {
            write(float_argument(input));
        } else if(field.type=="DoubleProperty") {
            write(double_argument(input));
        }
    }
    return result;
}

inline std::vector<std::byte> encode(const scalar_struct_values::Snapshot& snapshot,
                                    const nlohmann::json& value) {
    return encode(snapshot.layout,value);
}

} // namespace g1r_mcp::scalar_struct_arguments
