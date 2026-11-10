#pragma once

#include <sn2_mcp/vector_layout.hpp>

#include <algorithm>
#include <set>
#include <string>
#include <string_view>
#include <vector>

namespace sn2_mcp::transform_layout {

struct QuatLayout {
    int properties_size{},min_alignment{};
    bool has_super{};
    std::vector<struct_values::Component> components;
    bool operator==(const QuatLayout&) const=default;
};

struct MemberLayout {
    std::string name,type;
    int element_size{},offset{},array_dim{};
    bool operator==(const MemberLayout&) const=default;
};

struct Layout {
    int properties_size{},min_alignment{};
    bool has_super{};
    std::vector<MemberLayout> members;
    QuatLayout rotation;
    vector_layout::Layout translation,scale3d;
    bool operator==(const Layout&) const=default;
};

inline bool supported_alignment(int alignment,int maximum) {
    return alignment>=1 && alignment<=maximum && !(alignment&(alignment-1));
}

inline void validate_quat_layout(const QuatLayout& layout) {
    if(layout.properties_size!=32 || layout.has_super || layout.components.size()!=4
       || !supported_alignment(layout.min_alignment,16) || layout.properties_size%layout.min_alignment)
        throw HandlerError("unsupported_transform_layout", "Rotation requires an own 32-byte Quat");
    std::set<std::string> names;
    std::set<int> offsets;
    for(const auto& component:layout.components) {
        if(component.type!="DoubleProperty" || component.element_size!=8 || component.array_dim!=1
           || component.offset<0 || component.offset>24 || component.offset%8
           || !names.insert(component.name).second || !offsets.insert(component.offset).second)
            throw HandlerError("unsupported_transform_layout", "Quat requires four nonoverlapping DoubleProperty fields");
    }
    if(names!=std::set<std::string>{"X","Y","Z","W"} || offsets!=std::set<int>{0,8,16,24})
        throw HandlerError("unsupported_transform_layout", "Quat requires exact X, Y, Z and W components with a complete layout");
}

inline void validate_layout(const Layout& layout) {
    if(layout.properties_size!=96 || layout.has_super || layout.members.size()!=3
       || !supported_alignment(layout.min_alignment,16) || layout.properties_size%layout.min_alignment)
        throw HandlerError("unsupported_transform_layout", "Transform requires three own fields and size 96");
    validate_quat_layout(layout.rotation);
    try {
        vector_layout::validate_layout(layout.translation);
        vector_layout::validate_layout(layout.scale3d);
    } catch(const HandlerError&) {
        throw HandlerError("unsupported_transform_layout", "Translation and Scale3D require an exact Vector layout");
    }
    if(layout.translation.properties_size!=24 || layout.scale3d.properties_size!=24)
        throw HandlerError("unsupported_transform_layout", "Translation and Scale3D require 24-byte Vector fields");
    std::set<std::string> names;
    for(const auto& member:layout.members) {
        int expected_offset{},expected_size{},alignment{};
        if(member.name=="Rotation") {expected_offset=0;expected_size=32;alignment=layout.rotation.min_alignment;}
        else if(member.name=="Translation") {expected_offset=32;expected_size=24;alignment=layout.translation.min_alignment;}
        else if(member.name=="Scale3D") {expected_offset=64;expected_size=24;alignment=layout.scale3d.min_alignment;}
        else throw HandlerError("unsupported_transform_layout", "Unknown Transform field");
        if(!names.insert(member.name).second || member.type!="StructProperty" || member.array_dim!=1
           || member.offset!=expected_offset || member.element_size!=expected_size
           || member.offset%alignment || layout.min_alignment<alignment)
            throw HandlerError("unsupported_transform_layout", "The nested Transform layout is unconfirmed");
    }
}

inline void validate(std::string_view path,std::string_view name,const Layout& layout) {
    if(path!="/Script/CoreUObject.Transform" || name!="Transform")
        throw HandlerError("unsupported_transform_type", "Only exact CoreUObject.Transform is supported");
    transform_layout::validate_layout(layout);
}

struct QuatSnapshot {
    vector_layout::Snapshot metadata;
    QuatLayout layout;
    bool operator==(const QuatSnapshot&) const=default;
};

struct MemberMetadata {
    std::uintptr_t pointer{},next{},field_class{},structure{};
    std::uint64_t owner{},name{},type_name{},flags{};
    MemberLayout layout;
    bool operator==(const MemberMetadata&) const=default;
};

struct Snapshot {
    std::uintptr_t field{},field_class{},structure{},head{},super{};
    std::uint64_t field_owner{},field_name{},field_type_name{},field_flags{};
    int element_size{},array_dim{};
    ObjectSerialIdentity identity;
    std::string path,struct_name;
    Layout layout;
    std::vector<MemberMetadata> members;
    QuatSnapshot rotation;
    vector_layout::Snapshot translation,scale3d;
    bool operator==(const Snapshot&) const=default;
};

template<class Reader,class NameDecoder,class ObjectCapture,class ClassValidator,class PathResolver>
Snapshot capture(std::uintptr_t field,int element_size,int array_dim,Reader&& read,
                 NameDecoder&& decode,ObjectCapture&& capture_identity,
                 ClassValidator&& validate_class,PathResolver&& object_path) {
    if(element_size!=96 || array_dim!=1)
        throw HandlerError("unsupported_transform_layout", "A Transform parameter requires size 96 and ArrayDim=1");
    auto pointer=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uintptr_t>(vector_layout::checked_address(base,offset,sizeof(std::uintptr_t)));
    };
    auto word=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::uint64_t>(vector_layout::checked_address(base,offset,sizeof(std::uint64_t)));
    };
    auto integer=[&](std::uintptr_t base,std::size_t offset) {
        return read.template operator()<std::int32_t>(vector_layout::checked_address(base,offset,sizeof(std::int32_t)));
    };
    Snapshot result;
    result.field=field;result.field_class=pointer(field,8);result.field_owner=word(field,0x10);
    result.field_name=word(field,0x20);result.field_type_name=word(result.field_class,0);result.field_flags=word(field,0x38);
    result.element_size=integer(field,0x34);result.array_dim=integer(field,0x30);
    if(std::invoke(decode,result.field_type_name)!="StructProperty")
        throw HandlerError("unsupported_transform_type", "A Transform parameter must be StructProperty");
    if(result.element_size!=element_size || result.array_dim!=array_dim)
        throw HandlerError("unsupported_transform_layout", "The outer Transform layout changed");
    result.structure=pointer(field,0x70);result.identity=std::invoke(capture_identity,result.structure);
    if(result.identity.object!=result.structure || !same_serial_identity(result.identity,result.identity))
        throw HandlerError("unsupported_transform_type", "Transform identity does not match the reflected struct");
    std::invoke(validate_class,result.identity.object_class);
    result.path=std::invoke(object_path,result.structure);result.struct_name=std::invoke(decode,result.identity.name);
    if(result.path!="/Script/CoreUObject.Transform" || result.struct_name!="Transform")
        throw HandlerError("unsupported_transform_type", "The parameter does not refer to CoreUObject.Transform");
    result.head=pointer(result.structure,0x50);result.super=pointer(result.structure,0x40);
    result.layout.properties_size=integer(result.structure,0x58);result.layout.min_alignment=static_cast<std::int32_t>(read.template operator()<std::int16_t>(vector_layout::checked_address(result.structure,0x5c,sizeof(std::int16_t))));
    result.layout.has_super=result.super!=0;
    if(result.layout.properties_size!=96 || result.layout.has_super || !supported_alignment(result.layout.min_alignment,16))
        throw HandlerError("unsupported_transform_layout", "The Transform size, alignment or inheritance is unsupported");
    std::set<std::uintptr_t> visited;
    std::set<std::string> member_names;
    for(auto current=result.head;current;) {
        if(visited.size()>=3 || !visited.insert(current).second)
            throw HandlerError("unsupported_transform_layout", "Transform has extra fields or a cycle");
        MemberMetadata member;
        member.pointer=current;member.next=pointer(current,0x18);member.field_class=pointer(current,8);
        member.owner=word(current,0x10);member.name=word(current,0x20);member.type_name=word(member.field_class,0);
        member.flags=word(current,0x38);member.structure=pointer(current,0x70);
        member.layout={std::invoke(decode,member.name),std::invoke(decode,member.type_name),integer(current,0x34),integer(current,0x44),integer(current,0x30)};
        if(member.layout.type!="StructProperty" || member.layout.array_dim!=1
           || !member_names.insert(member.layout.name).second || member.structure==result.structure)
            throw HandlerError("unsupported_transform_layout", "The nested Transform type or cycle is unsupported");
        result.layout.members.push_back(member.layout);result.members.push_back(member);current=member.next;
    }
    if(member_names!=std::set<std::string>{"Rotation","Translation","Scale3D"})
        throw HandlerError("unsupported_transform_layout", "Transform requires Rotation, Translation and Scale3D");
    for(const auto& member:result.members) {
        if(member.layout.name=="Rotation") {
            if(member.layout.element_size!=32 || member.layout.offset!=0)
                throw HandlerError("unsupported_transform_layout", "Rotation requires offset 0 and Quat32");
            auto& quat=result.rotation;
            auto& metadata=quat.metadata;
            metadata.field=member.pointer;metadata.field_class=member.field_class;metadata.field_owner=member.owner;
            metadata.field_name=member.name;metadata.field_type_name=member.type_name;
            metadata.element_size=member.layout.element_size;metadata.array_dim=member.layout.array_dim;metadata.structure=member.structure;
            metadata.identity=std::invoke(capture_identity,metadata.structure);
            if(metadata.identity.object!=metadata.structure || !same_serial_identity(metadata.identity,metadata.identity))
                throw HandlerError("unsupported_transform_type", "Quat identity is unconfirmed");
            std::invoke(validate_class,metadata.identity.object_class);
            metadata.path=std::invoke(object_path,metadata.structure);metadata.struct_name=std::invoke(decode,metadata.identity.name);
            if(metadata.path!="/Script/CoreUObject.Quat" || metadata.struct_name!="Quat")
                throw HandlerError("unsupported_transform_type", "Rotation requires exact CoreUObject.Quat");
            metadata.head=pointer(metadata.structure,0x50);metadata.super=pointer(metadata.structure,0x40);
            quat.layout.properties_size=integer(metadata.structure,0x58);quat.layout.min_alignment=static_cast<std::int32_t>(read.template operator()<std::int16_t>(vector_layout::checked_address(metadata.structure,0x5c,sizeof(std::int16_t))));
            quat.layout.has_super=metadata.super!=0;
            std::set<std::uintptr_t> components;
            for(auto child=metadata.head;child;) {
                if(components.size()>=4 || !components.insert(child).second)
                    throw HandlerError("unsupported_transform_layout", "Quat has extra components or a cycle");
                vector_layout::ComponentMetadata component;
                component.pointer=child;component.next=pointer(child,0x18);component.field_class=pointer(child,8);
                component.owner=word(child,0x10);component.name=word(child,0x20);component.type_name=word(component.field_class,0);
                component.component={std::invoke(decode,component.name),std::invoke(decode,component.type_name),integer(child,0x34),integer(child,0x44),integer(child,0x30)};
                quat.layout.components.push_back(component.component);metadata.components.push_back(component);child=component.next;
            }
            metadata.layout={quat.layout.properties_size,quat.layout.min_alignment,quat.layout.has_super,quat.layout.components};
            validate_quat_layout(quat.layout);result.layout.rotation=quat.layout;
        } else {
            const auto expected=member.layout.name=="Translation"?32:64;
            if(member.layout.element_size!=24 || member.layout.offset!=expected)
                throw HandlerError("unsupported_transform_layout", "The Transform vector requires a confirmed offset and size 24");
            // A canonical Vector can be used twice; the fixed-depth helper
            // checks the exact path and XYZ layout, so a shared pointer is not a cycle.
            const auto vector=vector_layout::capture(member.pointer,24,1,read,decode,capture_identity,validate_class,object_path);
            if(vector.structure==result.structure || vector.structure==result.rotation.metadata.structure)
                throw HandlerError("unsupported_transform_layout", "Nested Transform types repeat the parent or Quat");
            if(member.layout.name=="Translation") {result.translation=vector;result.layout.translation=vector.layout;}
            else {result.scale3d=vector;result.layout.scale3d=vector.layout;}
        }
    }
    transform_layout::validate(result.path,result.struct_name,result.layout);
    return result;
}

} // namespace sn2_mcp::transform_layout
