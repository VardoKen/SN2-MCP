#pragma once

#include <g1r_mcp/scalar_arguments.hpp>
#include <g1r_mcp/scalar_struct_values.hpp>
#include <array>
#include <cstddef>
#include <cstring>
#include <set>
#include <span>
#include <string>

namespace g1r_mcp::rotator_arguments {

using Layout=scalar_struct_values::Layout;
using Snapshot=scalar_struct_values::Snapshot;

inline void validate_layout(const Layout& layout) {
    try {scalar_struct_values::validate_layout(layout);}
    catch(const HandlerError&) {
        throw HandlerError("unsupported_rotator_layout","FRotator requires three own DoubleProperty fields and size 24");
    }
    if(layout.properties_size!=24 || layout.min_alignment!=8 || layout.has_super || layout.components.size()!=3)
        throw HandlerError("unsupported_rotator_layout","FRotator requires size 24 and alignment 8");
    std::set<std::string> names;
    std::set<int> offsets;
    for(const auto& component:layout.components) {
        if(component.layout.type!="DoubleProperty" || component.layout.element_size!=8
           || component.layout.array_dim!=1 || component.offset%8)
            throw HandlerError("unsupported_rotator_layout","FRotator requires separate DoubleProperty fields");
        names.insert(component.name);offsets.insert(component.offset);
    }
    if(names!=std::set<std::string>{"Pitch","Yaw","Roll"} || offsets!=std::set<int>{0,8,16})
        throw HandlerError("unsupported_rotator_layout","FRotator requires exact Pitch, Yaw and Roll components");
}

inline void validate(const Snapshot& value) {
    if(value.path!="/Script/CoreUObject.Rotator" || value.struct_name!="Rotator")
        throw HandlerError("unsupported_rotator_type","Only confirmed CoreUObject.Rotator is supported");
    if(value.element_size!=24 || value.array_dim!=1)
        throw HandlerError("unsupported_rotator_layout","A FRotator parameter requires size 24 and ArrayDim=1");
    rotator_arguments::validate_layout(value.layout);
}

inline std::array<std::byte,24> encode(const Layout& layout,const nlohmann::json& value) {
    rotator_arguments::validate_layout(layout);
    if(!value.is_object() || value.size()!=3 || !value.contains("Pitch") || !value.contains("Yaw") || !value.contains("Roll"))
        throw HandlerError("invalid_argument","FRotator requires only {Pitch:number,Yaw:number,Roll:number}");
    std::array<std::byte,24> result{};
    for(const auto& component:layout.components) {
        const auto number=double_argument(value.at(component.name));
        std::memcpy(result.data()+component.offset,&number,sizeof(number));
    }
    return result;
}

inline nlohmann::json decode(const Layout& layout,std::span<const std::byte> bytes) {
    rotator_arguments::validate_layout(layout);
    return scalar_struct_values::decode(layout,bytes);
}

} // namespace g1r_mcp::rotator_arguments
