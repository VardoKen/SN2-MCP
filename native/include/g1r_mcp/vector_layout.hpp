#pragma once

#include <g1r_mcp/object_serial.hpp>
#include <g1r_mcp/struct_values.hpp>

#include <cstdint>
#include <functional>
#include <limits>
#include <set>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace g1r_mcp::vector_layout {

using Layout=struct_values::Layout;

inline void validate_layout(const Layout& layout) {
    try {struct_values::validate_layout(layout);}
    catch(const HandlerError&) {
        throw HandlerError("unsupported_vector_layout", "FVector requires three own float or double fields and size 12 or 24");
    }
    std::set<std::string> names;
    for(const auto& component:layout.components)names.insert(component.name);
    if(names!=std::set<std::string>{"X","Y","Z"})
        throw HandlerError("unsupported_vector_layout", "FVector requires exact X, Y and Z components");
}

inline void validate(std::string_view path,std::string_view struct_name,const Layout& layout) {
    if(path!="/Script/CoreUObject.Vector" || struct_name!="Vector")
        throw HandlerError("unsupported_vector_type", "Only confirmed CoreUObject.Vector is supported");
    vector_layout::validate_layout(layout);
}

struct ComponentMetadata {
    std::uintptr_t pointer{},next{},field_class{};
    std::uint64_t owner{},name{},type_name{};
    struct_values::Component component;
    bool operator==(const ComponentMetadata&) const=default;
};

struct Snapshot {
    std::uintptr_t field{},field_class{},structure{},head{},super{};
    std::uint64_t field_owner{},field_name{},field_type_name{};
    int element_size{},array_dim{};
    ObjectSerialIdentity identity;
    std::string path,struct_name;
    Layout layout;
    std::vector<ComponentMetadata> components;
    bool operator==(const Snapshot&) const=default;
};

inline std::uintptr_t checked_address(std::uintptr_t base,std::size_t offset,std::size_t bytes) {
    if(!base || offset>std::numeric_limits<std::uintptr_t>::max()-base)
        throw HandlerError("invalid_memory", "The FVector metadata address is missing or overflows");
    const auto address=base+offset;
    if(bytes && bytes-1>std::numeric_limits<std::uintptr_t>::max()-address)
        throw HandlerError("invalid_memory", "The FVector metadata read exceeds the address range");
    return address;
}

// Reader accesses only confirmed reflection offsets. The caller supplies identity and path.
// GameAccess owns registry access; this helper does not create engine structs.
template<class Reader,class NameDecoder,class ObjectCapture,class ClassValidator,class PathResolver>
Snapshot capture(std::uintptr_t field,int element_size,int array_dim,Reader&& read,
                 NameDecoder&& decode,ObjectCapture&& capture_identity,
                 ClassValidator&& validate_class,PathResolver&& object_path) {
    if((element_size!=12 && element_size!=24) || array_dim!=1)
        throw HandlerError("unsupported_vector_layout", "A FVector parameter requires size 12 or 24 and ArrayDim=1");
    auto read_pointer=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uintptr_t>(checked_address(base,offset,sizeof(std::uintptr_t)));
    };
    auto read_name=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uint64_t>(checked_address(base,offset,sizeof(std::uint64_t)));
    };
    auto read_int=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::int32_t>(checked_address(base,offset,sizeof(std::int32_t)));
    };
    Snapshot result;
    result.field=field;
    result.field_class=read_pointer(field,8);
    result.field_owner=read_name(field,0x10); // FFieldVariant: retain raw bits without interpreting the tag.
    result.field_name=read_name(field,0x20);
    result.field_type_name=read_name(result.field_class,0);
    result.element_size=read_int(field,0x34);
    result.array_dim=read_int(field,0x30);
    if(std::invoke(decode,result.field_type_name)!="StructProperty")
        throw HandlerError("unsupported_vector_type", "A FVector parameter must be StructProperty");
    if(result.element_size!=element_size || result.array_dim!=array_dim)
        throw HandlerError("unsupported_vector_layout", "The outer FVector reflection layout changed");

    result.structure=read_pointer(field,0x70);
    result.identity=std::invoke(capture_identity,result.structure);
    if(result.identity.object!=result.structure || !same_serial_identity(result.identity,result.identity))
        throw HandlerError("unsupported_vector_type", "FVector identity does not match the reflected struct");
    std::invoke(validate_class,result.identity.object_class);
    result.path=std::invoke(object_path,result.structure);
    result.struct_name=std::invoke(decode,result.identity.name);
    result.head=read_pointer(result.structure,0x50);
    result.super=read_pointer(result.structure,0x40);
    result.layout.properties_size=read_int(result.structure,0x58);
    result.layout.min_alignment=read_int(result.structure,0x5c);
    result.layout.has_super=result.super!=0;
    if(result.path!="/Script/CoreUObject.Vector" || result.struct_name!="Vector")
        throw HandlerError("unsupported_vector_type", "The parameter does not refer to CoreUObject.Vector");
    if(result.layout.properties_size!=element_size)
        throw HandlerError("unsupported_vector_layout", "The UStruct.Vector size does not match the parameter");
    std::set<std::uintptr_t> visited;
    for(auto current=result.head;current;) {
        if(visited.size()>=3 || !visited.insert(current).second)
            throw HandlerError("unsupported_vector_layout", "FVector has extra fields or a reflection cycle");
        ComponentMetadata component;
        component.pointer=current;
        component.next=read_pointer(current,0x18);
        component.field_class=read_pointer(current,8);
        component.owner=read_name(current,0x10);
        component.name=read_name(current,0x20);
        component.type_name=read_name(component.field_class,0);
        component.component={std::invoke(decode,component.name),
            std::invoke(decode,component.type_name),read_int(current,0x34),
            read_int(current,0x44),read_int(current,0x30)};
        result.layout.components.push_back(component.component);
        result.components.push_back(std::move(component));
        current=result.components.back().next;
    }
    validate(result.path,result.struct_name,result.layout);
    return result;
}

} // namespace g1r_mcp::vector_layout
