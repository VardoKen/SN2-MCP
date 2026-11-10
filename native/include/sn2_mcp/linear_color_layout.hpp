#pragma once

#include <sn2_mcp/object_serial.hpp>
#include <sn2_mcp/struct_values.hpp>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace sn2_mcp::linear_color_layout {

using Layout=struct_values::Layout;

inline void validate_layout(const Layout& layout) {
    if(layout.properties_size!=16 || layout.min_alignment!=4 || layout.has_super || layout.components.size()!=4)
        throw HandlerError("unsupported_linear_color_layout","FLinearColor requires four own FloatProperty fields with size 16 and alignment 4");
    const std::map<std::string,int> offsets{{"R",0},{"G",4},{"B",8},{"A",12}};
    std::set<std::string> names;
    for(const auto& component:layout.components) {
        const auto expected=offsets.find(component.name);
        if(expected==offsets.end() || !names.insert(component.name).second || component.type!="FloatProperty"
           || component.element_size!=4 || component.array_dim!=1 || component.offset!=expected->second)
            throw HandlerError("unsupported_linear_color_layout","FLinearColor requires exact R/G/B/A FloatProperty fields at offsets 0/4/8/12");
    }
}

inline void validate(std::string_view path,std::string_view struct_name,const Layout& layout) {
    if(path!="/Script/CoreUObject.LinearColor" || struct_name!="LinearColor")
        throw HandlerError("unsupported_linear_color_type","Only exact CoreUObject.LinearColor is supported");
    linear_color_layout::validate_layout(layout);
}

struct ComponentMetadata {
    std::uintptr_t pointer{},next{},field_class{};
    std::uint64_t owner{},name{},type_name{},flags{};
    std::int32_t field_flags{};
    struct_values::Component component;
    bool operator==(const ComponentMetadata&) const=default;
};

struct Snapshot {
    std::uintptr_t field{},next{},field_class{},structure{},head{},super{};
    std::uint64_t field_owner{},field_name{},field_type_name{},flags{};
    int element_size{},array_dim{},offset{},field_flags{};
    ObjectSerialIdentity identity;
    std::string path,struct_name;
    Layout layout;
    std::vector<ComponentMetadata> components;
    bool operator==(const Snapshot&) const=default;
};

inline std::uintptr_t checked_address(std::uintptr_t base,std::size_t offset,std::size_t bytes) {
    if(!base || offset>std::numeric_limits<std::uintptr_t>::max()-base)
        throw HandlerError("invalid_memory","The FLinearColor metadata address overflows");
    const auto address=base+offset;
    if(bytes && bytes-1>std::numeric_limits<std::uintptr_t>::max()-address)
        throw HandlerError("invalid_memory","The FLinearColor metadata range overflows");
    return address;
}

template<class Read,class NameDecoder,class ObjectCapture,class ClassValidator,class PathResolver>
Snapshot capture(std::uintptr_t field,int element_size,int array_dim,Read&& read,
    NameDecoder&& decode,ObjectCapture&& capture_identity,ClassValidator&& validate_class,PathResolver&& object_path) {
    if(element_size!=16 || array_dim!=1)
        throw HandlerError("unsupported_linear_color_layout","A FLinearColor parameter requires size 16 and ArrayDim=1");
    const auto pointer=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uintptr_t>(checked_address(base,offset,sizeof(std::uintptr_t)));
    };
    const auto raw_name=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uint64_t>(checked_address(base,offset,sizeof(std::uint64_t)));
    };
    const auto integer=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::int32_t>(checked_address(base,offset,sizeof(std::int32_t)));
    };
    Snapshot result;
    result.field=field;result.next=pointer(field,0x18);result.field_class=pointer(field,8);
    result.field_owner=raw_name(field,0x10);result.field_name=raw_name(field,0x20);
    result.field_type_name=raw_name(result.field_class,0);result.flags=raw_name(field,0x38);
    result.field_flags=integer(field,0x28);result.element_size=integer(field,0x34);
    result.array_dim=integer(field,0x30);result.offset=integer(field,0x44);
    if(std::invoke(decode,result.field_type_name)!="StructProperty")
        throw HandlerError("unsupported_linear_color_type","A FLinearColor parameter must be an exact StructProperty");
    if(result.element_size!=element_size || result.array_dim!=array_dim || result.offset<0 || result.offset%4 || result.next==field)
        throw HandlerError("unsupported_linear_color_layout","The outer FLinearColor layout does not match");
    result.structure=pointer(field,0x70);
    result.identity=std::invoke(capture_identity,result.structure);
    if(result.identity.object!=result.structure || !result.identity.object_class || !same_serial_identity(result.identity,result.identity))
        throw HandlerError("unsupported_linear_color_type","FLinearColor identity does not match the reflected struct");
    std::invoke(validate_class,result.identity.object_class);
    result.path=std::invoke(object_path,result.structure);result.struct_name=std::invoke(decode,result.identity.name);
    result.head=pointer(result.structure,0x50);result.super=pointer(result.structure,0x40);
    result.layout.properties_size=integer(result.structure,0x58);result.layout.min_alignment=static_cast<std::int32_t>(read.template operator()<std::int16_t>(checked_address(result.structure,0x5c,sizeof(std::int16_t))));
    result.layout.has_super=result.super!=0;
    if(result.path!="/Script/CoreUObject.LinearColor" || result.struct_name!="LinearColor")
        throw HandlerError("unsupported_linear_color_type","The parameter does not refer to CoreUObject.LinearColor");
    std::set<std::uintptr_t> visited;
    for(auto current=result.head;current;) {
        if(visited.size()>=4 || current==field || !visited.insert(current).second)
            throw HandlerError("unsupported_linear_color_layout","FLinearColor has extra fields or a reflection cycle");
        ComponentMetadata component;
        component.pointer=current;component.next=pointer(current,0x18);component.field_class=pointer(current,8);
        component.owner=raw_name(current,0x10);component.name=raw_name(current,0x20);
        component.type_name=raw_name(component.field_class,0);component.flags=raw_name(current,0x38);
        component.field_flags=integer(current,0x28);
        component.component={std::invoke(decode,component.name),std::invoke(decode,component.type_name),
            integer(current,0x34),integer(current,0x44),integer(current,0x30)};
        result.layout.components.push_back(component.component);result.components.push_back(std::move(component));
        current=result.components.back().next;
    }
    validate(result.path,result.struct_name,result.layout);
    return result;
}

} // namespace sn2_mcp::linear_color_layout
