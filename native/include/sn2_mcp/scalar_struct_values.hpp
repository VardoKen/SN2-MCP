#pragma once

#include <sn2_mcp/object_serial.hpp>
#include <sn2_mcp/scalar_outputs.hpp>
#include <sn2_mcp/vector_layout.hpp>
#include <cstdint>
#include <functional>
#include <set>
#include <span>
#include <string>
#include <utility>
#include <vector>

namespace sn2_mcp::scalar_struct_values {
struct Component {
    std::string name;
    int offset{};
    scalar_outputs::Layout layout;
    bool operator==(const Component&) const=default;
};
struct Layout {
    int properties_size{},min_alignment{};
    bool has_super{};
    std::vector<Component> components;
    bool operator==(const Layout&) const=default;
};
inline void validate_layout(const Layout& layout) {
    if(layout.has_super || layout.properties_size<1 || layout.min_alignment<1
       || (layout.min_alignment&(layout.min_alignment-1)) || layout.properties_size%layout.min_alignment
       || layout.components.empty())
        throw HandlerError("unsupported_scalar_struct_layout","DTO requires own scalar fields and reflected size/alignment");
    std::set<std::string> names;
    for(std::size_t i=0;i<layout.components.size();++i) {
        const auto& component=layout.components[i];
        scalar_outputs::validate_layout(component.layout);
        if(component.name.empty() || !names.insert(component.name).second || component.offset<0
           || component.layout.element_size>layout.properties_size
           || component.offset>layout.properties_size-component.layout.element_size)
            throw HandlerError("unsupported_scalar_struct_layout","DTO fields have duplicate names or exceed the struct size");
        for(std::size_t j=0;j<i;++j) {
            const auto& other=layout.components[j];
            if(component.offset>=other.offset+other.layout.element_size
               || other.offset>=component.offset+component.layout.element_size)continue;
            if(component.layout.type=="BoolProperty" && other.layout.type=="BoolProperty"
               && component.offset==other.offset
               && !(component.layout.field_mask&other.layout.field_mask))continue;
            throw HandlerError("unsupported_scalar_struct_layout","Scalar DTO fields overlap the same storage");
        }
    }
}
inline nlohmann::json decode(const Layout& layout,std::span<const std::byte> bytes) {
    validate_layout(layout);
    if(bytes.size()!=static_cast<std::size_t>(layout.properties_size))
        throw HandlerError("invalid_struct_copy","The DTO copy size does not match its reflected size");
    auto result=nlohmann::json::object();
    for(const auto& component:layout.components)
        result[component.name]=scalar_outputs::decode(component.layout,
            bytes.subspan(static_cast<std::size_t>(component.offset),static_cast<std::size_t>(component.layout.element_size)));
    return result;
}
struct ComponentMetadata {
    std::uintptr_t pointer{},next{},field_class{};
    std::uint64_t owner{},name{},type_name{},flags{};
    Component component;
    bool operator==(const ComponentMetadata&) const=default;
};
struct Snapshot {
    std::uintptr_t field{},next{},field_class{},structure{},head{},super{};
    std::uint64_t owner{},name{},type_name{},flags{};
    int offset{},element_size{},array_dim{};
    ObjectSerialIdentity identity;
    std::string path,struct_name;
    Layout layout;
    std::vector<ComponentMetadata> components;
    bool operator==(const Snapshot&) const=default;
};
// Capture reads reflection; decode reads only the owned result copy.
template<class Reader,class NameDecoder,class ObjectCapture,class ClassValidator,class PathResolver>
Snapshot capture(std::uintptr_t field,int size,int dim,Reader&& read,NameDecoder&& name,
                 ObjectCapture&& identity,ClassValidator&& validate_class,PathResolver&& path) {
    auto pointer=[&](std::uintptr_t at,std::size_t offset){return read.template operator()<std::uintptr_t>(at+offset);};
    auto integer=[&](std::uintptr_t at,std::size_t offset){return read.template operator()<std::int32_t>(at+offset);};
    auto raw_name=[&](std::uintptr_t at,std::size_t offset){return read.template operator()<std::uint64_t>(at+offset);};
    Snapshot result;
    result.field=field;result.next=pointer(field,0x18);result.field_class=pointer(field,8);
    result.owner=raw_name(field,0x10);result.name=raw_name(field,0x20);result.type_name=raw_name(result.field_class,0);
    result.flags=raw_name(field,0x38);result.offset=integer(field,0x44);
    result.element_size=integer(field,0x34);result.array_dim=integer(field,0x30);
    if(std::invoke(name,result.type_name)!="StructProperty" || result.element_size!=size || result.array_dim!=dim
       || dim!=1 || size<1)
        throw HandlerError("unsupported_scalar_struct_layout","DTO requires one StructProperty of a matching size");
    result.structure=pointer(field,0x70);result.identity=std::invoke(identity,result.structure);
    std::invoke(validate_class,result.identity.object_class);
    result.path=std::invoke(path,result.structure);result.struct_name=std::invoke(name,result.identity.name);
    result.head=pointer(result.structure,0x50);result.super=pointer(result.structure,0x40);
    result.layout={integer(result.structure,0x58),static_cast<std::int32_t>(read.template operator()<std::int16_t>(vector_layout::checked_address(result.structure,0x5c,sizeof(std::int16_t)))),result.super!=0,{}};
    if(result.layout.properties_size!=size)
        throw HandlerError("unsupported_scalar_struct_layout","The DTO UScriptStruct size does not match the parameter");
    std::set<std::uintptr_t> visited;
    for(auto current=result.head;current;current=pointer(current,0x18)) {
        if(!visited.insert(current).second)
            throw HandlerError("unsupported_scalar_struct_layout","The DTO field chain contains a cycle");
        ComponentMetadata child;
        child.pointer=current;child.next=pointer(current,0x18);child.field_class=pointer(current,8);
        child.owner=raw_name(current,0x10);child.name=raw_name(current,0x20);
        child.type_name=raw_name(child.field_class,0);child.flags=raw_name(current,0x38);
        child.component={std::invoke(name,child.name),integer(current,0x44),
            {std::invoke(name,child.type_name),integer(current,0x34),integer(current,0x30)}};
        if(child.component.layout.type=="BoolProperty") {
            auto& boolean=child.component.layout;
            boolean.field_size=read.template operator()<std::uint8_t>(current+0x70);
            boolean.byte_offset=read.template operator()<std::uint8_t>(current+0x71);
            boolean.byte_mask=read.template operator()<std::uint8_t>(current+0x72);
            boolean.field_mask=read.template operator()<std::uint8_t>(current+0x73);
        }
        result.layout.components.push_back(child.component);result.components.push_back(std::move(child));
    }
    validate_layout(result.layout);
    return result;
}
} // namespace sn2_mcp::scalar_struct_values
