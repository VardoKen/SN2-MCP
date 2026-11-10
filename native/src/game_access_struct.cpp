#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/game_memory.hpp"
#include "sn2_mcp/array_page.hpp"
#include "sn2_mcp/scalar_struct_values.hpp"
#include "sn2_mcp/scalar_arguments.hpp"
#include "sn2_mcp/enum_arguments.hpp"
#include "sn2_mcp/vector_arguments.hpp"
#include "sn2_mcp/transform_arguments.hpp"
#include "sn2_mcp/rotator_arguments.hpp"
#include "sn2_mcp/linear_color_arguments.hpp"
#include <functional>
#include <cstring>
#include <optional>
#include <set>
#include <span>
#include <vector>
namespace sn2_mcp {
using json=nlohmann::json;
using game_memory::read;
namespace {
scalar_outputs::Layout pod_scalar_layout(const json& captured) {
    scalar_outputs::Layout layout{captured.at("type").get<std::string>(),captured.at("element_size").get<int>(),captured.at("array_dim").get<int>()};
    if(layout.type=="BoolProperty") {
        const auto& boolean=captured.at("boolean");
        layout.field_size=boolean.at("field_size").get<uint8_t>();layout.byte_offset=boolean.at("byte_offset").get<uint8_t>();
        layout.byte_mask=boolean.at("byte_mask").get<uint8_t>();layout.field_mask=boolean.at("field_mask").get<uint8_t>();
    }
    scalar_outputs::validate_layout(layout);return layout;
}
bool pod_numeric_type(const std::string& type) {
    return type=="BoolProperty" || type=="IntProperty" || type=="Int64Property" || type=="UInt32Property"
        || type=="UInt64Property" || type=="FloatProperty" || type=="DoubleProperty";
}
}
json GameAccess::capture_pod_struct_parameter(uintptr_t field,int size,int dim) const {
    auto captured=capture_read_value(field,size,dim);
    std::function<void(json&)> complete;
    complete=[&](json& current) {
        if(current.contains("error"))throw HandlerError(current.at("error").at("code").get<std::string>(),current.at("error").at("message").get<std::string>());
        const auto type=current.at("type").get<std::string>();
        const auto bytes=current.at("element_size").get<int>();
        if(type=="StructProperty") {
            const auto& structure=current.at("structure");
            current["struct_path"]=structure.at("path");current["struct_name"]=name(structure.at("identity").at("name").get<uint64_t>());
            current["properties_size"]=structure.at("properties_size");current["min_alignment"]=structure.at("min_alignment");
            auto& children=current.at("children");
            if(children.empty())throw HandlerError("unsupported_scalar_struct_layout","A POD DTO requires own supported fields");
            std::set<std::string> names;
            for(size_t index=0;index<children.size();++index) {
                auto& child=children[index];complete(child);
                const auto offset=child.at("offset").get<int>(),child_size=child.at("element_size").get<int>();
                if(child.at("name").get<std::string>().empty() || !names.insert(child.at("name").get<std::string>()).second
                   || offset<0 || child_size>bytes || offset>bytes-child_size)
                    throw HandlerError("unsupported_scalar_struct_layout","DTO fields duplicate names or exceed the struct size");
                for(size_t previous=0;previous<index;++previous) {
                    const auto& other=children[previous];const auto other_offset=other.at("offset").get<int>(),other_size=other.at("element_size").get<int>();
                    if(offset>=other_offset+other_size || other_offset>=offset+child_size)continue;
                    if(child.at("type")=="BoolProperty" && other.at("type")=="BoolProperty" && offset==other_offset
                       && !(child.at("boolean").at("field_mask").get<uint8_t>()&other.at("boolean").at("field_mask").get<uint8_t>()))continue;
                    throw HandlerError("unsupported_scalar_struct_layout","POD DTO fields overlap");
                }
            }
        } else if(pod_numeric_type(type)) {
            pod_scalar_layout(current);
        } else if(type=="NameProperty") {
            if(bytes!=8)throw HandlerError("unsupported_scalar_struct_layout","A DTO FName requires eight bytes");
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            if(bytes!=1 || current.at("underlying_type")!="ByteProperty")
                throw HandlerError("unsupported_enum_underlying_layout","A DTO enum requires one-byte ByteProperty values");
            const auto layout=enum_layout(type,bytes,current.at("pointer").get<uintptr_t>());
            if(type=="EnumProperty" || layout.object) {
                const auto table=enum_table(layout.object);json entries=json::array();
                for(const auto& entry:table.entries)entries.push_back({{"name",entry.name},{"label",name(entry.name)},{"value",entry.value}});
                const auto pointer=reinterpret_cast<uintptr_t>(layout.object);
                current["enum_table"]={{"path",table.path},{"underlying_type",layout.underlying_type},{"entries",std::move(entries)},
                    {"data",read<uintptr_t>(pointer+0x40)},{"count",read<int32_t>(pointer+0x48)},{"capacity",read<int32_t>(pointer+0x4c)}};
            }
        } else throw HandlerError("unsupported_scalar_struct_layout","A POD DTO supports only own numeric, FName, byte enum, or nested DTO fields");
    };
    if(captured.at("type")!="StructProperty")throw HandlerError("unsupported_scalar_struct_layout","A POD DTO requires StructProperty");
    complete(captured);return captured;
}
json GameAccess::pod_struct_metadata(const json& captured) {
    json components=json::array();
    for(const auto& child:captured.at("children")) {
        json item={{"name",child.at("name")},{"type",child.at("type")},{"element_size",child.at("element_size")},
            {"array_dim",child.at("array_dim")},{"offset",child.at("offset")}};
        if(child.contains("boolean"))item["bool_layout"]=child.at("boolean");
        if(child.at("type")=="StructProperty")item["struct"]=pod_struct_metadata(child);
        if(child.contains("enum_table")) {
            const auto& table=child.at("enum_table");json entries=json::array();
            for(const auto& entry:table.at("entries"))entries.push_back({{"name",entry.at("label")},{"value",entry.at("value")}});
            item["enum"]={{"path",table.at("path")},{"underlying_type",table.at("underlying_type")},{"entries",std::move(entries)}};
        }
        components.push_back(std::move(item));
    }
    return {{"struct_path",captured.at("struct_path")},{"struct_name",captured.at("struct_name")},
        {"properties_size",captured.at("properties_size")},{"min_alignment",captured.at("min_alignment")},{"components",std::move(components)}};
}
std::vector<std::byte> GameAccess::encode_pod_struct(const json& captured,const json& value) const {
    const auto& children=captured.at("children");
    if(!value.is_object() || value.size()!=children.size())throw HandlerError("invalid_argument","A DTO requires exactly the reflected field names");
    std::vector<std::byte> result(captured.at("element_size").get<size_t>(),std::byte{});
    for(const auto& child:children) {
        const auto& key=child.at("name").get_ref<const std::string&>();
        if(!value.contains(key))throw HandlerError("invalid_argument","A DTO requires exactly the reflected field names");
        const auto& input=value.at(key);const auto type=child.at("type").get<std::string>();
        auto* destination=result.data()+child.at("offset").get<size_t>();
        const auto write=[destination](auto number){std::memcpy(destination,&number,sizeof(number));};
        if(type=="StructProperty") {
            const auto encoded=encode_pod_struct(child,input);std::memcpy(destination,encoded.data(),encoded.size());
        } else if(type=="NameProperty")write(encode_name_text(input));
        else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto encoded=enum_arguments::encode_integer("ByteProperty",1,input);std::memcpy(destination,encoded.bytes.data(),encoded.size);
        } else if(type=="BoolProperty") {
            const auto layout=pod_scalar_layout(child);
            if(!input.is_boolean())throw HandlerError("invalid_argument","A DTO boolean field is required");
            auto& byte=destination[layout.byte_offset];const auto previous=std::to_integer<uint8_t>(byte);
            byte=static_cast<std::byte>((previous&static_cast<uint8_t>(~layout.byte_mask))|(input.get<bool>()?layout.byte_mask:uint8_t{}));
        } else if(type=="IntProperty")write(int32_argument(input));
        else if(type=="Int64Property")write(int64_argument(input));
        else if(type=="UInt32Property")write(uint32_argument(input));
        else if(type=="UInt64Property")write(uint64_argument(input));
        else if(type=="FloatProperty")write(float_argument(input));
        else if(type=="DoubleProperty")write(double_argument(input));
        else throw HandlerError("unsupported_scalar_struct_layout","The DTO field is unsupported");
    }
    return result;
}
json GameAccess::decode_pod_struct(const json& captured,std::span<const std::byte> copied) {
    if(copied.size()!=captured.at("element_size").get<size_t>())throw HandlerError("invalid_struct_copy","The DTO copy size does not match reflection");
    json result=json::object();
    for(const auto& child:captured.at("children")) {
        const auto type=child.at("type").get<std::string>();
        const auto bytes=copied.subspan(child.at("offset").get<size_t>(),child.at("element_size").get<size_t>());
        json value;
        if(type=="StructProperty")value=decode_pod_struct(child,bytes);
        else if(type=="NameProperty") {
            uint64_t packed{};std::memcpy(&packed,bytes.data(),sizeof(packed));value=name(packed);
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            uint8_t integer{};std::memcpy(&integer,bytes.data(),sizeof(integer));value=integer;
        } else value=scalar_outputs::decode(pod_scalar_layout(child),bytes);
        result[child.at("name").get_ref<const std::string&>()]=std::move(value);
    }
    return result;
}
json GameAccess::capture_read_value(uintptr_t field,int size,int dim) const {
    std::set<uintptr_t> active_structs,active_arrays;
    auto identity=[&](uintptr_t pointer) {
        const auto value=serial_identity(validate_object(reinterpret_cast<void*>(pointer)));
        return json{{"pointer",pointer},{"class",value.object_class},{"index",value.index},{"name",value.name},
            {"serial",value.serial},{"class_index",value.class_index},{"class_name",value.class_name},{"class_serial",value.class_serial}};
    };
    std::function<json(uintptr_t,int,int)> capture;
    capture=[&](uintptr_t current,int element_size,int array_dim) -> json {
        const auto field_class=read<uintptr_t>(current+8),raw_name=read<uint64_t>(current+0x20),type_name=read<uint64_t>(field_class);
        const auto type=name(type_name);
        json result={{"pointer",current},{"next",read<uintptr_t>(current+0x18)},{"field_class",field_class},
            {"owner",read<uint64_t>(current+0x10)},{"raw_name",raw_name},{"name",name(raw_name)},{"type_name",type_name},{"type",type},
            {"flags",read<uint64_t>(current+0x38)},{"field_flags",read<uint32_t>(current+0x28)},
            {"offset",read<int32_t>(current+0x44)},{"element_size",read<int32_t>(current+0x34)},{"array_dim",read<int32_t>(current+0x30)}};
        try {
        if(element_size<1 || array_dim!=1 || result.at("element_size")!=element_size || result.at("array_dim")!=array_dim)
            throw HandlerError("unsupported_struct_field_layout","The reflected value requires a matching size and ArrayDim=1");
        if(type=="BoolProperty") {
            result["boolean"]={{"field_size",read<uint8_t>(current+0x70)},{"byte_offset",read<uint8_t>(current+0x71)},
                {"byte_mask",read<uint8_t>(current+0x72)},{"field_mask",read<uint8_t>(current+0x73)}};
        } else if(type=="ObjectProperty" || type=="ClassProperty" || type=="WeakObjectProperty") {
            const auto declared=read<uintptr_t>(current+0x70);validate_class(reinterpret_cast<void*>(declared));
            result["declared"]=identity(declared);
            if(type=="ClassProperty") {
                const auto meta=read<uintptr_t>(current+0x78);validate_class(reinterpret_cast<void*>(meta));result["meta"]=identity(meta);
            }
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto enumeration=enum_layout(type,element_size,current);result["underlying_type"]=enumeration.underlying_type;
            if(type=="EnumProperty") {
                const auto underlying=read<uintptr_t>(current+0x70),klass=read<uintptr_t>(underlying+8);
                result["underlying"]={underlying,klass,read<uint64_t>(klass),read<uint64_t>(underlying+0x10),
                    read<uintptr_t>(underlying+0x18),read<uint64_t>(underlying+0x20),read<int32_t>(underlying+0x30),
                    read<int32_t>(underlying+0x34),read<uint64_t>(underlying+0x38),read<int32_t>(underlying+0x44)};
            }
            if(enumeration.object)result["enum"]=identity(reinterpret_cast<uintptr_t>(enumeration.object));
        } else if(type=="ArrayProperty") {
            if(element_size!=sizeof(ArrayHeader))throw HandlerError("unsupported_struct_field_layout","An array value requires a 16-byte header");
            result["array_flags"]=read<uint8_t>(current+0x70);
            const auto inner=read<uintptr_t>(current+0x78);
            if(!active_arrays.insert(current).second)throw HandlerError("struct_scan_incomplete","The nested array type path is cyclic");
            try {result["inner"]=capture(inner,read<int32_t>(inner+0x34),read<int32_t>(inner+0x30));}
            catch(...) {active_arrays.erase(current);throw;}
            active_arrays.erase(current);
        } else if(type=="StructProperty") {
            const auto structure=read<uintptr_t>(current+0x70),head=read<uintptr_t>(structure+0x50),super=read<uintptr_t>(structure+0x40);
            const auto structure_identity=identity(structure);validate_class(reinterpret_cast<void*>(structure_identity.at("class").get<uintptr_t>()));
            const auto properties_size=read<int32_t>(structure+0x58);const auto alignment=read<int16_t>(structure+0x5c);
            result["structure"]={{"identity",structure_identity},{"path",object_path(reinterpret_cast<void*>(structure))},
                {"head",head},{"super",super},{"properties_size",properties_size},{"min_alignment",alignment}};
            if(properties_size!=element_size || super || alignment<1 || (alignment&(alignment-1)) || element_size%alignment)
                throw HandlerError("unsupported_struct_field_layout","A nested struct requires matching reflected size, alignment, and own fields");
            if(!active_structs.insert(structure).second)
                throw HandlerError("struct_scan_incomplete","The nested struct type path is cyclic");
            try {
                json children=json::array();std::set<uintptr_t> fields;
                for(auto child=head;child;child=read<uintptr_t>(child+0x18)) {
                    if(!fields.insert(child).second)throw HandlerError("struct_scan_incomplete","The nested struct field chain is cyclic");
                    children.push_back(capture(child,read<int32_t>(child+0x34),read<int32_t>(child+0x30)));
                }
                result["children"]=std::move(children);
            } catch(...) {active_structs.erase(structure);throw;}
            active_structs.erase(structure);
        }
        } catch(const HandlerError& error) {result["error"]={{"code",error.code()},{"message",error.what()}};}
        return result;
    };
    return capture(field,size,dim);
}

json GameAccess::decode_read_value(const json& metadata,std::span<const std::byte> copied,uintptr_t live_address,
                                 std::vector<std::pair<uintptr_t,ArrayHeader>>& copied_headers) {
    if(metadata.contains("error"))throw HandlerError(metadata.at("error").at("code").get<std::string>(),metadata.at("error").at("message").get<std::string>());
    const auto field=metadata.at("pointer").get<uintptr_t>();const auto type=metadata.at("type").get<std::string>();
    const auto size=metadata.at("element_size").get<int>(),dim=metadata.at("array_dim").get<int>();
    if(size<1 || dim!=1 || copied.size()!=static_cast<size_t>(size))
        throw HandlerError("unsupported_struct_field_layout","The copied value does not match its reflected size and ArrayDim");
    if(type=="StructProperty") {
        const auto path=metadata.at("structure").at("path").get<std::string>();
        json value;
        if(path=="/Script/CoreUObject.Vector")value=vector_arguments::decode(capture_vector_parameter(field,size,dim).layout,copied);
        else if(path=="/Script/CoreUObject.Transform")value=transform_arguments::decode(capture_transform_parameter(field,size,dim).layout,copied);
        else if(path=="/Script/CoreUObject.LinearColor")value=linear_color_arguments::decode(capture_linear_color_parameter(field,size,dim).layout,copied);
        else if(path=="/Script/CoreUObject.Rotator" || path=="/Script/CoreUObject.Quat") {
            const auto snapshot=scalar_struct_values::capture(field,size,dim,
                []<class Value>(uintptr_t address){return read<Value>(address);},[this](uint64_t raw){return name(raw);},
                [this](uintptr_t pointer){return serial_identity(validate_object(reinterpret_cast<void*>(pointer)));},
                [this](uintptr_t pointer){validate_class(reinterpret_cast<void*>(pointer));},
                [this](uintptr_t pointer){return object_path(reinterpret_cast<void*>(pointer));});
            if(path=="/Script/CoreUObject.Rotator") {
                rotator_arguments::validate(snapshot);value=rotator_arguments::decode(snapshot.layout,copied);
            }
            else {
                if(snapshot.path!="/Script/CoreUObject.Quat" || snapshot.struct_name!="Quat")
                    throw HandlerError("unsupported_transform_type","Rotation requires exact CoreUObject.Quat");
                transform_layout::QuatLayout quaternion{snapshot.layout.properties_size,snapshot.layout.min_alignment,snapshot.layout.has_super,{}};
                for(const auto& component:snapshot.layout.components)
                    quaternion.components.push_back({component.name,component.layout.type,component.layout.element_size,component.offset,component.layout.array_dim});
                transform_layout::validate_quat_layout(quaternion);value=scalar_struct_values::decode(snapshot.layout,copied);
            }
        } else {
            value=json::object();json components=json::array();bool partial=false;
            for(const auto& child:metadata.at("children")) {
                const auto child_size=child.at("element_size").get<int>(),offset=child.at("offset").get<int>();
                const auto child_type=child.at("type").get<std::string>(),child_name=child.at("name").get<std::string>();
                json item={{"name",child_name},{"type",child_type},{"element_size",child_size},{"offset",offset},
                    {"array_dim",child.at("array_dim")},{"supported",false}};
                if(child.contains("structure"))item["struct_path"]=child.at("structure").at("path");
                try {
                    if(offset<0 || child_size<1 || child_size>size || offset>size-child_size)
                        throw HandlerError("unsupported_struct_field_layout","The nested struct field exceeds its copied struct");
                    auto decoded=decode_read_value(child,copied.subspan(static_cast<size_t>(offset),static_cast<size_t>(child_size)),
                        live_address+static_cast<uintptr_t>(offset),copied_headers);
                    value[child_name]=decoded.at("value");item["value"]=decoded.at("value");item["supported"]=true;
                    for(const auto* key:{"components","items","enum","total_count","partial"})if(decoded.contains(key))item[key]=decoded.at(key);
                    partial=partial || decoded.value("partial",false);
                } catch(const HandlerError& error) {item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
                components.push_back(std::move(item));
            }
            return {{"value",std::move(value)},{"components",std::move(components)},{"partial",partial}};
        }
        return {{"value",std::move(value)},{"partial",false}};
    }
    if(type=="ArrayProperty") {
        const auto& inner=metadata.at("inner");const auto stride=inner.at("element_size").get<int>();
        const auto header=read<ArrayHeader>(reinterpret_cast<uintptr_t>(copied.data()));
        copied_headers.emplace_back(live_address,header);
        if(inner.contains("error"))
            throw HandlerError(inner.at("error").at("code").get<std::string>(),inner.at("error").at("message").get<std::string>());
        const auto page=array_page(header,stride,inner.at("array_dim").get<int>(),0,header.count>0?static_cast<size_t>(header.count):size_t{1});
        std::vector<std::byte> payload(page.bytes);
        if(page.bytes && !game_memory::copy(payload.data(),reinterpret_cast<void*>(page.begin),page.bytes))
            throw HandlerError("invalid_memory","The nested array bytes are unavailable");
        json values=json::array(),items=json::array();bool partial=false;
        for(int32_t index=0;index<page.count;++index) {
            json item={{"index",index},{"supported",false}};
            try {
                const auto offset=static_cast<size_t>(index)*static_cast<size_t>(stride);
                auto decoded=decode_read_value(inner,std::span<const std::byte>(payload).subspan(offset,static_cast<size_t>(stride)),
                    page.begin+offset,copied_headers);
                values.push_back(decoded.at("value"));item["value"]=decoded.at("value");item["supported"]=true;
                for(const auto* key:{"components","items","enum","total_count","partial"})if(decoded.contains(key))item[key]=decoded.at(key);
                partial=partial || decoded.value("partial",false);
            } catch(const HandlerError& error) {values.push_back(nullptr);item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
            items.push_back(std::move(item));
        }
        if(!same_array_header(header,read<ArrayHeader>(live_address)))throw HandlerError("array_changed","The nested array header changed while being copied");
        return {{"value",std::move(values)},{"items",std::move(items)},{"total_count",header.count},{"partial",partial}};
    }
    const auto address=reinterpret_cast<uintptr_t>(copied.data());json result;
    if(type=="EnumProperty" || type=="ByteProperty") {
        const auto decoded=enum_property(address,type,size,field);result={{"value",decoded.at("value")},{"partial",false}};
        if(decoded.contains("enum"))result["enum"]=decoded.at("enum");
        return result;
    }
    auto value=property_value(address,type,size,field);
    if(value.is_null() && type!="ObjectProperty" && type!="ClassProperty" && type!="WeakObjectProperty")
        throw HandlerError("unsupported_struct_field_type","The nested value type is unsupported");
    return {{"value",std::move(value)},{"partial",false}};
}

json GameAccess::read_struct(void* object,const std::string& handle,const std::string& property_name) {
    if(property_name.empty() || property_name.find('\0')!=std::string::npos)
        throw HandlerError("invalid_struct_params","property_name must be nonempty and contain no NUL");
    const auto initial=validate_object(object);
    struct Child {
        scalar_struct_values::ComponentMetadata field;
        json auxiliary=json::object();
        std::optional<vector_layout::Snapshot> vector;
        std::optional<transform_layout::Snapshot> transform;
        std::optional<rotator_arguments::Snapshot> rotator;
        std::optional<scalar_struct_values::Snapshot> quat;
        std::optional<linear_color_layout::Snapshot> color;
        std::optional<scalar_struct_values::Snapshot> gameplay_tag_array;
        json error=nullptr;
        bool operator==(const Child&) const=default;
    };
    struct Metadata {
        uintptr_t owner{};
        int owner_size{};
        Handle owner_identity;
        scalar_struct_values::Snapshot structure;
        std::vector<Child> children;
    };
    auto capture=[&]() {
        Metadata result;
        uintptr_t selected{};
        auto klass=reinterpret_cast<uintptr_t>(validate_object(object).object_class);
        std::set<uintptr_t> classes,fields;
        for(;klass && !selected;klass=read<uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)
                throw HandlerError("struct_scan_incomplete","Class chain is cyclic");
            validate_class(reinterpret_cast<void*>(klass));
            for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)
                    throw HandlerError("struct_scan_incomplete","Property chain is cyclic");
                if(name(read<uint64_t>(field+0x20))==property_name){selected=field;result.owner=klass;break;}
            }
        }
        if(!selected)throw HandlerError("property_not_found","Struct property not found");
        result.owner_identity=validate_object(reinterpret_cast<void*>(result.owner));
        if(game_memory::field_type(selected,[this](uint64_t value){return name(value);})!="StructProperty")
            throw HandlerError("unsupported_struct_type","The selected property is not an exact StructProperty");
        const auto size=read<int32_t>(selected+0x34),dim=read<int32_t>(selected+0x30);
        result.owner_size=read<int32_t>(result.owner+0x58);
        if(dim!=1 || size<1)
            throw HandlerError("unsupported_struct_layout","The outer struct property is unsupported");
        auto& structure=result.structure;
        structure.field=selected;structure.next=read<uintptr_t>(selected+0x18);
        structure.field_class=read<uintptr_t>(selected+8);structure.owner=read<uint64_t>(selected+0x10);
        structure.name=read<uint64_t>(selected+0x20);structure.type_name=read<uint64_t>(structure.field_class);
        structure.flags=read<uint64_t>(selected+0x38);structure.offset=read<int32_t>(selected+0x44);
        structure.element_size=size;structure.array_dim=dim;structure.structure=read<uintptr_t>(selected+0x70);
        structure.identity=serial_identity(validate_object(reinterpret_cast<void*>(structure.structure)));
        validate_class(reinterpret_cast<void*>(structure.identity.object_class));
        structure.path=object_path(reinterpret_cast<void*>(structure.structure));structure.struct_name=name(structure.identity.name);
        structure.head=read<uintptr_t>(structure.structure+0x50);structure.super=read<uintptr_t>(structure.structure+0x40);
        structure.layout={read<int32_t>(structure.structure+0x58),read<int16_t>(structure.structure+0x5c),structure.super!=0,{}};
        if(structure.layout.properties_size!=size || structure.super || structure.layout.min_alignment<1
           || (structure.layout.min_alignment&(structure.layout.min_alignment-1)) || size%structure.layout.min_alignment)
            throw HandlerError("unsupported_struct_layout","The struct requires matching reflected size, alignment, and own fields");
        auto identity=[&](uintptr_t pointer) {
            const auto value=serial_identity(validate_object(reinterpret_cast<void*>(pointer)));
            return json{{"pointer",pointer},{"class",value.object_class},{"index",value.index},{"name",value.name},
                {"serial",value.serial},{"class_index",value.class_index},{"class_name",value.class_name},{"class_serial",value.class_serial}};
        };
        std::set<uintptr_t> children;
        for(auto field=structure.head;field;field=read<uintptr_t>(field+0x18)) {
            if(!children.insert(field).second)throw HandlerError("struct_scan_incomplete","Struct field chain is cyclic");
            Child child;auto& metadata=child.field;
            metadata.pointer=field;metadata.next=read<uintptr_t>(field+0x18);metadata.field_class=read<uintptr_t>(field+8);
            metadata.owner=read<uint64_t>(field+0x10);metadata.name=read<uint64_t>(field+0x20);
            metadata.type_name=read<uint64_t>(metadata.field_class);metadata.flags=read<uint64_t>(field+0x38);
            metadata.component={name(metadata.name),read<int32_t>(field+0x44),
                {name(metadata.type_name),read<int32_t>(field+0x34),read<int32_t>(field+0x30)}};
            auto& layout=metadata.component.layout;const auto& type=layout.type;
            try {
                if(type=="BoolProperty") {
                    layout.field_size=read<uint8_t>(field+0x70);layout.byte_offset=read<uint8_t>(field+0x71);
                    layout.byte_mask=read<uint8_t>(field+0x72);layout.field_mask=read<uint8_t>(field+0x73);
                } else if(type=="ObjectProperty" || type=="ClassProperty" || type=="WeakObjectProperty") {
                    child.auxiliary["declared"]=identity(read<uintptr_t>(field+0x70));
                    if(type=="ClassProperty")child.auxiliary["meta"]=identity(read<uintptr_t>(field+0x78));
                } else if(type=="EnumProperty" || type=="ByteProperty") {
                    const auto enumeration=enum_layout(type,layout.element_size,field);
                    child.auxiliary["underlying_type"]=enumeration.underlying_type;
                    if(type=="EnumProperty") {
                        const auto underlying=read<uintptr_t>(field+0x70),klass=read<uintptr_t>(underlying+8);
                        child.auxiliary["underlying"]={underlying,klass,read<uint64_t>(klass),read<uint64_t>(underlying+0x10),
                            read<uintptr_t>(underlying+0x18),read<uint64_t>(underlying+0x20),read<int32_t>(underlying+0x30),
                            read<int32_t>(underlying+0x34),read<uint64_t>(underlying+0x38),read<int32_t>(underlying+0x44)};
                    }
                    if(enumeration.object)child.auxiliary["enum"]=identity(reinterpret_cast<uintptr_t>(enumeration.object));
                } else if(type=="ArrayProperty") {
                    const auto inner=read<uintptr_t>(field+0x78);
                    if(game_memory::field_type(inner,[this](uint64_t value){return name(value);})!="StructProperty"
                       || object_path(reinterpret_cast<void*>(read<uintptr_t>(inner+0x70)))!="/Script/GameplayTags.GameplayTag") {
                        child.auxiliary["nested"]=capture_read_value(field,layout.element_size,layout.array_dim);
                    } else {
                    auto& snapshot=child.gameplay_tag_array.emplace();
                    snapshot.field=read<uintptr_t>(field+0x78);
                    snapshot.next=read<uintptr_t>(snapshot.field+0x18);snapshot.field_class=read<uintptr_t>(snapshot.field+8);
                    snapshot.owner=read<uint64_t>(snapshot.field+0x10);snapshot.name=read<uint64_t>(snapshot.field+0x20);
                    snapshot.type_name=read<uint64_t>(snapshot.field_class);snapshot.flags=read<uint64_t>(snapshot.field+0x38);
                    snapshot.offset=read<int32_t>(snapshot.field+0x44);snapshot.element_size=read<int32_t>(snapshot.field+0x34);
                    snapshot.array_dim=read<int32_t>(snapshot.field+0x30);
                    if(name(snapshot.type_name)!="StructProperty")
                        throw HandlerError("unsupported_struct_field_type","The array requires exact GameplayTags.GameplayTag elements");
                    snapshot.structure=read<uintptr_t>(snapshot.field+0x70);
                    snapshot.identity=serial_identity(validate_object(reinterpret_cast<void*>(snapshot.structure)));
                    validate_class(reinterpret_cast<void*>(snapshot.identity.object_class));
                    snapshot.path=object_path(reinterpret_cast<void*>(snapshot.structure));snapshot.struct_name=name(snapshot.identity.name);
                    snapshot.head=read<uintptr_t>(snapshot.structure+0x50);snapshot.super=read<uintptr_t>(snapshot.structure+0x40);
                    snapshot.layout={read<int32_t>(snapshot.structure+0x58),read<int16_t>(snapshot.structure+0x5c),snapshot.super!=0,{}};
                    if(snapshot.path!="/Script/GameplayTags.GameplayTag")
                        throw HandlerError("unsupported_struct_field_type","The array requires exact GameplayTags.GameplayTag elements");
                    if(snapshot.array_dim!=1 || snapshot.element_size<1 || snapshot.layout.properties_size!=snapshot.element_size)
                        throw HandlerError("unsupported_struct_field_layout","GameplayTag elements require a matching reflected size and ArrayDim");
                    std::set<uintptr_t> tag_fields;bool has_tag_name=false;
                    for(auto tag_field=snapshot.head;tag_field;tag_field=read<uintptr_t>(tag_field+0x18)) {
                        if(!tag_fields.insert(tag_field).second)
                            throw HandlerError("struct_scan_incomplete","GameplayTag field chain is cyclic");
                        scalar_struct_values::ComponentMetadata tag;
                        tag.pointer=tag_field;tag.next=read<uintptr_t>(tag_field+0x18);tag.field_class=read<uintptr_t>(tag_field+8);
                        tag.owner=read<uint64_t>(tag_field+0x10);tag.name=read<uint64_t>(tag_field+0x20);
                        tag.type_name=read<uint64_t>(tag.field_class);tag.flags=read<uint64_t>(tag_field+0x38);
                        tag.component={name(tag.name),read<int32_t>(tag_field+0x44),
                            {name(tag.type_name),read<int32_t>(tag_field+0x34),read<int32_t>(tag_field+0x30)}};
                        snapshot.layout.components.push_back(tag.component);snapshot.components.push_back(tag);
                        if(tag.component.name!="TagName")continue;
                        if(has_tag_name || tag.component.layout.type!="NameProperty" || tag.component.layout.element_size!=8
                           || tag.component.layout.array_dim!=1 || tag.component.offset<0 || snapshot.element_size<8
                           || tag.component.offset>snapshot.element_size-8)
                            throw HandlerError("unsupported_struct_field_layout","GameplayTag requires one own TagName NameProperty");
                        has_tag_name=true;
                    }
                    if(!has_tag_name)
                        throw HandlerError("unsupported_struct_field_layout","GameplayTag requires an own TagName NameProperty");
                    }
                } else if(type=="StructProperty") {
                    const auto pointer=read<uintptr_t>(field+0x70);child.auxiliary["structure"]=identity(pointer);
                    const auto path=object_path(reinterpret_cast<void*>(pointer));child.auxiliary["struct_path"]=path;
                    if(path=="/Script/CoreUObject.Vector")child.vector=capture_vector_parameter(field,layout.element_size,layout.array_dim);
                    else if(path=="/Script/CoreUObject.Transform")child.transform=capture_transform_parameter(field,layout.element_size,layout.array_dim);
                    else if(path=="/Script/CoreUObject.LinearColor")child.color=capture_linear_color_parameter(field,layout.element_size,layout.array_dim);
                    else if(path=="/Script/CoreUObject.Rotator" || path=="/Script/CoreUObject.Quat") {
                        auto snapshot=scalar_struct_values::capture(field,layout.element_size,layout.array_dim,
                            []<class Value>(uintptr_t address){return read<Value>(address);},
                            [this](uint64_t value){return name(value);},
                            [this](uintptr_t value){return serial_identity(validate_object(reinterpret_cast<void*>(value)));},
                            [this](uintptr_t value){validate_class(reinterpret_cast<void*>(value));},
                            [this](uintptr_t value){return object_path(reinterpret_cast<void*>(value));});
                        if(path=="/Script/CoreUObject.Rotator") {
                            rotator_arguments::validate(snapshot);child.rotator=std::move(snapshot);
                        } else {
                            if(snapshot.path!="/Script/CoreUObject.Quat" || snapshot.struct_name!="Quat")
                                throw HandlerError("unsupported_transform_type","Rotation requires exact CoreUObject.Quat");
                            transform_layout::QuatLayout quaternion{snapshot.layout.properties_size,snapshot.layout.min_alignment,snapshot.layout.has_super,{}};
                            for(const auto& component:snapshot.layout.components)
                                quaternion.components.push_back({component.name,component.layout.type,component.layout.element_size,component.offset,component.layout.array_dim});
                            transform_layout::validate_quat_layout(quaternion);child.quat=std::move(snapshot);
                        }
                    } else child.auxiliary["nested"]=capture_read_value(field,layout.element_size,layout.array_dim);
                }
            } catch(const HandlerError& error) {child.error={{"code",error.code()},{"message",error.what()}};}
            structure.layout.components.push_back(metadata.component);structure.components.push_back(metadata);
            result.children.push_back(std::move(child));
        }
        if(result.structure.offset<0 || result.owner_size<size || result.structure.offset>result.owner_size-size)
            throw HandlerError("unsupported_struct_layout","The outer struct property is unsupported");
        return result;
    };
    const auto before=capture();
    std::vector<std::byte> bytes(static_cast<size_t>(before.structure.element_size));
    const auto address=reinterpret_cast<uintptr_t>(object)+static_cast<uintptr_t>(before.structure.offset);
    if(address<reinterpret_cast<uintptr_t>(object)
       || !game_memory::copy(bytes.data(),reinterpret_cast<void*>(address),bytes.size()))
        throw HandlerError("invalid_memory","Struct bytes are unavailable");
    json components=json::array();bool partial=false;
    std::vector<std::pair<uintptr_t,ArrayHeader>> copied_arrays;
    for(const auto& child:before.children) {
        const auto& component=child.field.component;const auto& layout=component.layout;
        json item={{"name",component.name},{"type",layout.type},{"element_size",layout.element_size},
            {"offset",component.offset},{"array_dim",layout.array_dim},{"supported",false}};
        if(child.auxiliary.contains("struct_path"))item["struct_path"]=child.auxiliary.at("struct_path");
        if(child.gameplay_tag_array && !child.gameplay_tag_array->path.empty())
            item["element_struct_path"]=child.gameplay_tag_array->path;
        try {
            if(!child.error.is_null())throw HandlerError(child.error.at("code").get<std::string>(),child.error.at("message").get<std::string>());
            if(layout.array_dim!=1 || layout.element_size<1 || component.offset<0
               || layout.element_size>before.structure.element_size || component.offset>before.structure.element_size-layout.element_size)
                throw HandlerError("unsupported_struct_field_layout","The struct field layout is unsupported");
            const auto copied=std::span<const std::byte>(bytes).subspan(static_cast<size_t>(component.offset),static_cast<size_t>(layout.element_size));
            const auto address=reinterpret_cast<uintptr_t>(copied.data());json value;
            if(child.vector)value=vector_arguments::decode(child.vector->layout,copied);
            else if(child.transform)value=transform_arguments::decode(child.transform->layout,copied);
            else if(child.rotator)value=rotator_arguments::decode(child.rotator->layout,copied);
            else if(child.quat)value=scalar_struct_values::decode(child.quat->layout,copied);
            else if(child.color)value=linear_color_arguments::decode(child.color->layout,copied);
            else if(child.auxiliary.contains("nested")) {
                const auto live_address=reinterpret_cast<uintptr_t>(object)+static_cast<uintptr_t>(before.structure.offset)+static_cast<uintptr_t>(component.offset);
                const auto decoded=decode_read_value(child.auxiliary.at("nested"),copied,live_address,copied_arrays);
                value=decoded.at("value");
                for(const auto* key:{"components","items","enum","total_count","partial"})if(decoded.contains(key))item[key]=decoded.at(key);
                partial=partial || decoded.value("partial",false);
            }
            else if(child.gameplay_tag_array) {
                if(layout.element_size!=sizeof(ArrayHeader))
                    throw HandlerError("unsupported_struct_field_layout","An array field requires a 16-byte header");
                const auto& snapshot=*child.gameplay_tag_array;
                const auto& tag=*std::find_if(snapshot.components.begin(),snapshot.components.end(),
                    [](const auto& field){return field.component.name=="TagName";});
                const auto header=read<ArrayHeader>(address);
                const auto page=array_page(header,snapshot.element_size,snapshot.array_dim,0,
                    header.count>0?static_cast<size_t>(header.count):size_t{1});
                std::vector<std::byte> payload(page.bytes);
                if(page.bytes && !game_memory::copy(payload.data(),reinterpret_cast<void*>(page.begin),page.bytes))
                    throw HandlerError("invalid_memory","GameplayTag array bytes are unavailable");
                value=json::array();item["total_count"]=header.count;
                for(int32_t index=0;index<page.count;++index) {
                    const auto element=std::span<const std::byte>(payload).subspan(
                        static_cast<size_t>(index)*static_cast<size_t>(snapshot.element_size),static_cast<size_t>(snapshot.element_size));
                    value.push_back({{"TagName",property_value(reinterpret_cast<uintptr_t>(element.data())+static_cast<uintptr_t>(tag.component.offset),
                        tag.component.layout.type,tag.component.layout.element_size,tag.pointer)}});
                }
                const auto live_header_address=reinterpret_cast<uintptr_t>(object)+static_cast<uintptr_t>(before.structure.offset)
                    +static_cast<uintptr_t>(component.offset);
                if(!same_array_header(header,read<ArrayHeader>(live_header_address)))
                    throw HandlerError("array_changed","The GameplayTag array header changed while being copied");
                copied_arrays.emplace_back(live_header_address,header);
            }
            else if(layout.type=="EnumProperty" || layout.type=="ByteProperty") {
                const auto result=enum_property(address,layout.type,layout.element_size,child.field.pointer);
                value=result.at("value");if(result.contains("enum"))item["enum"]=result.at("enum");
            } else value=property_value(address,layout.type,layout.element_size,child.field.pointer);
            if(value.is_null() && layout.type!="ObjectProperty" && layout.type!="ClassProperty" && layout.type!="WeakObjectProperty")
                throw HandlerError("unsupported_struct_field_type","The struct field type is unsupported");
            item["value"]=std::move(value);item["supported"]=true;
        } catch(const HandlerError& error) {item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
        components.push_back(std::move(item));
    }
    const auto after=capture();
    if(before.owner!=after.owner || before.owner_size!=after.owner_size || !(before.structure==after.structure) || before.children!=after.children
       || !same_serial_identity(serial_identity(before.owner_identity),serial_identity(after.owner_identity))
       || !same_serial_identity(serial_identity(initial),serial_identity(validate_object(object))))
        throw HandlerError("struct_metadata_changed","Struct metadata or identity changed while being copied");
    if(resolve(handle)!=object)throw HandlerError("stale_handle","The struct handle changed");
    for(const auto& [header_address,header]:copied_arrays)
        if(!same_array_header(header,read<ArrayHeader>(header_address)))
            throw HandlerError("array_changed","A nested array header changed while the struct was copied");
    return {{"source","live_native"},{"session_id",handle_session_},{"handle",handle},{"property_name",property_name},
            {"struct_path",before.structure.path},{"properties_size",before.structure.layout.properties_size},
            {"min_alignment",before.structure.layout.min_alignment},{"components",components},
            {"consistency","copied_bytes_then_metadata_revalidated"},{"partial",partial},{"truncated",false}};
}
} // namespace sn2_mcp
