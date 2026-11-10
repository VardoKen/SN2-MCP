#include "g1r_mcp/game_access.hpp"
#include "g1r_mcp/game_memory.hpp"
#include "g1r_mcp/array_page.hpp"
#include "g1r_mcp/scalar_arguments.hpp"
#include "g1r_mcp/scalar_struct_values.hpp"
#include "g1r_mcp/object_array_parameter_layout.hpp"
#include "g1r_mcp/string_return.hpp"
#include <array>
#include <cstring>
#include <limits>
#include <optional>
#include <set>
#include <span>

namespace g1r_mcp {
using json=nlohmann::json;
using game_memory::read;

json GameAccess::capture_object_array_return(uintptr_t field,int size,int dim,bool output) const {
    const auto flags=read<uint64_t>(field+0x38);
    if(game_memory::field_type(field,[this](uint64_t value){return name(value);})!="ArrayProperty" || size!=16 || dim!=1
       || read<int32_t>(field+0x34)!=size || read<int32_t>(field+0x30)!=dim || !(flags&0x80)
       || (output ? (!(flags&0x100) || (flags&0x400)) : !(flags&0x400))
       || (flags&(uint64_t{0x2}|uint64_t{0x08000000})))
        throw HandlerError("unsupported_signature",output ? "Object arrays require a 16-byte pure output parameter" : "Object arrays require a 16-byte return by value");
    const auto inner=read<uintptr_t>(field+0x78),klass=read<uintptr_t>(inner+8);
    const auto inner_name=read<uint64_t>(inner+0x20),type_name=read<uint64_t>(klass);
    if(name(type_name)!="ObjectProperty" || read<int32_t>(inner+0x34)!=8 || read<int32_t>(inner+0x30)!=1 || read<int32_t>(inner+0x44)!=0)
        throw HandlerError("unsupported_signature",output ? "The output array requires exact ObjectProperty elements" : "The returned array requires exact ObjectProperty elements");
    const auto declared=read<uintptr_t>(inner+0x70);validate_class(reinterpret_cast<void*>(declared));
    const auto identity=serial_identity(validate_object(reinterpret_cast<void*>(declared)));
    const auto field_class=read<uintptr_t>(field+8);
    const json public_metadata={{"container_type","ArrayProperty"},{"element",{{"type","ObjectProperty"},{"element_size",8},{"array_dim",1},{"offset",0},
        {"declared_type",{{"name",object_name(reinterpret_cast<void*>(declared))},{"path",object_path(reinterpret_cast<void*>(declared))}}}}}};
    return {{"outer",{{"pointer",field},{"next",read<uintptr_t>(field+0x18)},{"field_class",field_class},{"owner",read<uint64_t>(field+0x10)},
        {"name",read<uint64_t>(field+0x20)},{"type_name",read<uint64_t>(field_class)},{"flags",flags},{"field_flags",read<uint32_t>(field+0x28)},
        {"offset",read<int32_t>(field+0x44)},{"element_size",size},{"array_dim",dim},{"array_flags",read<uint8_t>(field+0x70)}}},
        {"inner",{{"pointer",inner},{"next",read<uintptr_t>(inner+0x18)},{"field_class",klass},{"owner",read<uint64_t>(inner+0x10)},
            {"name",inner_name},{"type_name",type_name},{"flags",read<uint64_t>(inner+0x38)},{"field_flags",read<uint32_t>(inner+0x28)},
            {"offset",0},{"element_size",8},{"array_dim",1}}},
        {"declared",{{"pointer",declared},{"class",identity.object_class},{"index",identity.index},{"name",identity.name},
            {"serial",identity.serial},{"class_index",identity.class_index},{"class_name",identity.class_name},{"class_serial",identity.class_serial}}},
        {"public",public_metadata}};
}

json GameAccess::capture_scalar_array_return(uintptr_t field,int size,int dim,bool output) const {
    const auto capture=[&]() {
        const auto snapshot=[&](uintptr_t pointer) {
            const auto klass=read<uintptr_t>(pointer+8);
            const auto type_name=read<uint64_t>(klass);
            return json{{"pointer",pointer},{"next",read<uintptr_t>(pointer+0x18)},
                {"owner",read<uint64_t>(pointer+0x10)},{"field_class",klass},{"type_name",type_name},{"type",name(type_name)},
                {"name",read<uint64_t>(pointer+0x20)},{"field_flags",read<uint32_t>(pointer+0x28)},
                {"element_size",read<int32_t>(pointer+0x34)},{"array_dim",read<int32_t>(pointer+0x30)},
                {"offset",read<int32_t>(pointer+0x44)},{"flags",read<uint64_t>(pointer+0x38)}};
        };
        auto outer=snapshot(field);
        const auto flags=outer.at("flags").get<uint64_t>();
        const auto offset=outer.at("offset").get<int32_t>();
        if(outer.at("type")!="ArrayProperty" || size!=16 || dim!=1 || outer.at("element_size")!=size
           || outer.at("array_dim")!=dim || offset<0 || offset%8 || !(flags&0x80)
           || (output ? (!(flags&0x100) || (flags&0x400)) : !(flags&0x400))
           || (flags&(uint64_t{0x2}|uint64_t{0x08000000})))
            throw HandlerError("unsupported_signature",output ? "Scalar arrays require a 16-byte pure output parameter" : "Scalar arrays require a 16-byte return by value");
        outer["array_flags"]=read<uint8_t>(field+0x70);
        const auto inner_pointer=read<uintptr_t>(field+0x78);
        outer["inner"]=inner_pointer;
        auto inner=snapshot(inner_pointer);
        const auto type=inner.at("type").get<std::string>();
        const auto stride=inner.at("element_size").get<int>();
        const bool enumeration=type=="EnumProperty" || type=="ByteProperty";
        const bool numeric=type=="BoolProperty" || type=="IntProperty" || type=="Int64Property" || type=="UInt32Property"
            || type=="UInt64Property" || type=="FloatProperty" || type=="DoubleProperty";
        scalar_outputs::Layout scalar_layout{type,stride,1};
        if(numeric) {
            if(type=="BoolProperty") {
                scalar_layout.field_size=read<uint8_t>(inner_pointer+0x70);scalar_layout.byte_offset=read<uint8_t>(inner_pointer+0x71);
                scalar_layout.byte_mask=read<uint8_t>(inner_pointer+0x72);scalar_layout.field_mask=read<uint8_t>(inner_pointer+0x73);
                inner["bool_layout"]={{"field_size",scalar_layout.field_size},{"byte_offset",scalar_layout.byte_offset},
                    {"byte_mask",scalar_layout.byte_mask},{"field_mask",scalar_layout.field_mask}};
            }
            scalar_outputs::validate_layout(scalar_layout);
        }
        if(!(numeric || (type=="NameProperty" && stride==8) || (type=="StrProperty" && stride==16) || (enumeration && stride==1)
             || (type=="StructProperty" && stride>0))
           || inner.at("array_dim")!=1 || inner.at("offset")!=0)
            throw HandlerError("unsupported_signature","The returned array requires int32, FName, byte enum, or own scalar DTO elements");
        json element={{"type",type},{"element_size",stride},{"array_dim",1},{"offset",0}};
        if(type=="BoolProperty")element["bool_layout"]=inner.at("bool_layout");
        json captured={{"outer",std::move(outer)},{"inner",std::move(inner)},{"scalar_type",type}};
        if(type=="StructProperty") {
            const auto structure=capture_pod_struct_parameter(inner_pointer,stride,1);
            captured["struct"]=structure;
            element["struct"]=pod_struct_metadata(structure);
        }
        if(enumeration) {
            const auto layout=enum_layout(type,stride,inner_pointer);
            if(layout.underlying_type!="ByteProperty")
                throw HandlerError("unsupported_enum_underlying_layout","The returned enum array requires ByteProperty underlying values");
            captured["inner"]["enum"]=reinterpret_cast<uintptr_t>(layout.object);
            captured["underlying_type"]=layout.underlying_type;
            if(type=="EnumProperty") {
                const auto pointer=read<uintptr_t>(inner_pointer+0x70);
                captured["inner"]["underlying"]=pointer;
                captured["underlying"]=snapshot(pointer);
            }
            if(type=="EnumProperty" || layout.object) {
                const auto table=enum_table(layout.object);
                json entries=json::array(),public_entries=json::array();
                for(const auto& entry:table.entries) {
                    const auto label=name(entry.name);
                    entries.push_back({{"name",entry.name},{"label",label},{"value",entry.value}});
                    public_entries.push_back({{"name",label},{"value",entry.value}});
                }
                const auto address=reinterpret_cast<uintptr_t>(layout.object);
                captured["enum"]={{"identity",object_array_parameter_layout::identity_json(serial_identity(table.identity))},
                    {"path",table.path},{"entries",std::move(entries)},{"data",read<uintptr_t>(address+0x40)},
                    {"count",read<int32_t>(address+0x48)},{"capacity",read<int32_t>(address+0x4c)}};
                element["enum"]={{"path",table.path},{"underlying_type",layout.underlying_type},{"entries",std::move(public_entries)}};
            }
        }
        captured["public"]={{"container_type","ArrayProperty"},{"element",std::move(element)}};
        return captured;
    };
    const auto before=capture();
    if(before!=capture())throw HandlerError("scalar_array_metadata_changed","Reflected scalar return metadata changed during capture");
    return before;
}
json GameAccess::read_scalar_array_return(uintptr_t field,uintptr_t address,const json& metadata) {
    const bool output=!(metadata.at("outer").at("flags").get<uint64_t>()&0x400);
    if(capture_scalar_array_return(field,16,1,output)!=metadata)
        throw HandlerError("signature_changed","The returned scalar array metadata changed after the call");
    const auto header=read<ArrayHeader>(address);
    const auto stride=metadata.at("inner").at("element_size").get<int>();
    const auto type=metadata.at("scalar_type").get<std::string>();
    const bool numeric=type=="BoolProperty" || type=="IntProperty" || type=="Int64Property" || type=="UInt32Property"
        || type=="UInt64Property" || type=="FloatProperty" || type=="DoubleProperty";
    scalar_outputs::Layout scalar_layout{type,stride,1};
    if(type=="BoolProperty") {
        const auto& boolean=metadata.at("inner").at("bool_layout");
        scalar_layout.field_size=boolean.at("field_size").get<uint8_t>();scalar_layout.byte_offset=boolean.at("byte_offset").get<uint8_t>();
        scalar_layout.byte_mask=boolean.at("byte_mask").get<uint8_t>();scalar_layout.field_mask=boolean.at("field_mask").get<uint8_t>();
    }
    json structure=nullptr;
    if(type=="StructProperty") {
        structure=capture_pod_struct_parameter(metadata.at("inner").at("pointer").get<uintptr_t>(),stride,1);
        if(structure!=metadata.at("struct"))
            throw HandlerError("signature_changed","The returned DTO array structure changed after the call");
    }
    const auto page=array_page(header,stride,1,0,std::numeric_limits<size_t>::max());
    std::vector<std::byte> copied(page.bytes);
    if(page.bytes && !game_memory::copy(copied.data(),reinterpret_cast<void*>(page.begin),page.bytes))
        throw HandlerError("invalid_memory","The returned scalar array elements are unavailable");
    json items=json::array();
    for(int32_t index=0;index<page.count;++index) {
        const auto bytes=copied.data()+static_cast<size_t>(index)*stride;
        json item={{"index",index},{"supported",true}};
        if(numeric) {
            item["value"]=scalar_outputs::decode(scalar_layout,std::span<const std::byte>(bytes,static_cast<size_t>(stride)));
        } else if(type=="NameProperty") {
            uint64_t value{};std::memcpy(&value,bytes,sizeof(value));item["value"]=name(value);
        } else if(type=="StrProperty") {
            StringHeader value{};std::memcpy(&value,bytes,sizeof(value));item["value"]=read_copied_string(value);
        } else if(type=="StructProperty") {
            item["value"]=decode_pod_struct(structure,std::span<const std::byte>(bytes,static_cast<size_t>(stride)));
        } else {
            uint8_t value{};std::memcpy(&value,bytes,sizeof(value));item["value"]=value;
            if(metadata.contains("enum"))item["enum"]=enum_metadata(reinterpret_cast<void*>(metadata.at("inner").at("enum").get<uintptr_t>()),item["value"],"ByteProperty");
        }
        items.push_back(std::move(item));
    }
    if(capture_scalar_array_return(field,16,1,output)!=metadata || !same_array_header(header,read<ArrayHeader>(address)))
        throw HandlerError("array_changed","The returned scalar array metadata or header changed during read");
    return {{"items",std::move(items)},{"total_count",header.count},{"partial",false}};
}
json GameAccess::read_object_array_return(uintptr_t field,uintptr_t address,const json& metadata) {
    const bool output=!(metadata.at("outer").at("flags").get<uint64_t>()&0x400);
    if(capture_object_array_return(field,16,1,output)!=metadata)throw HandlerError("signature_changed",output ? "The output array metadata changed after the call" : "The returned array metadata changed after the call");
    const auto header=read<ArrayHeader>(address);
    const auto page=array_page(header,8,1,0,std::numeric_limits<size_t>::max());
    std::vector<std::byte> copied(page.bytes);
    if(page.bytes && !game_memory::copy(copied.data(),reinterpret_cast<void*>(page.begin),page.bytes))
        throw HandlerError("invalid_memory",output ? "The output array elements are unavailable" : "The returned array elements are unavailable");
    json items=json::array();bool partial=false;const auto inner=metadata.at("inner").at("pointer").get<uintptr_t>();
    for(int32_t index=0;index<page.count;++index) {
        json item={{"index",index},{"supported",false}};
        try {
            item["value"]=property_value(reinterpret_cast<uintptr_t>(copied.data()+static_cast<size_t>(index)*8),"ObjectProperty",8,inner);
            item["supported"]=true;
        } catch(const HandlerError& error) {item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
        items.push_back(std::move(item));
    }
    if(capture_object_array_return(field,16,1,output)!=metadata || !same_array_header(header,read<ArrayHeader>(address)))
        throw HandlerError("array_changed",output ? "The output array metadata or header changed during the read" : "The returned array metadata or header changed during the read");
    return {{"items",std::move(items)},{"total_count",header.count},{"partial",partial}};
}

json GameAccess::read_array(const json& params) {
    if(!params.is_object() || !params.contains("handle") || !params.at("handle").is_string()
       || !params.contains("property_name") || !params.at("property_name").is_string())
        throw HandlerError("invalid_array_params","String handle and property_name fields are required");
    const auto handle=params.at("handle").get<std::string>();
    const auto property_name=params.at("property_name").get<std::string>();
    if(handle.empty() || handle.find('\0')!=std::string::npos
       || property_name.empty() || property_name.find('\0')!=std::string::npos)
        throw HandlerError("invalid_array_params","handle and property_name must be nonempty and contain no NUL");
    auto integer=[&](const char* key,uint64_t default_value,uint64_t minimum,uint64_t maximum) {
        if(!params.contains(key))return default_value;
        const auto& value=params.at(key);
        if(!value.is_number_integer()
           || (!value.is_number_unsigned() && value.get<int64_t>()<0)
           || value.get<uint64_t>()<minimum
           || value.get<uint64_t>()>maximum)
            throw HandlerError("invalid_array_params","offset and limit require integers within their representable ranges");
        return value.get<uint64_t>();
    };
    const auto offset=static_cast<int32_t>(integer("offset",0,0,std::numeric_limits<int32_t>::max()));
    const auto limit=static_cast<std::size_t>(integer("limit",64,1,std::numeric_limits<std::size_t>::max()));
    auto object=resolve(handle);
    const auto initial=validate_object(object);
    struct StructChild {
        scalar_struct_values::ComponentMetadata field;
        json auxiliary=json::object();
        json error=nullptr;
        bool operator==(const StructChild&) const=default;
    };
    struct Metadata {
        uintptr_t field{},owner{},field_class{},inner{},inner_class{};
        uint64_t field_name{},inner_name{},flags{},inner_flags{};
        int offset{},size{},dim{},owner_size{},stride{},inner_dim{},inner_offset{};
        uint8_t array_flags{};
        std::string element_type;
        std::vector<uint64_t> auxiliary;
        std::optional<scalar_struct_values::Snapshot> element_struct;
        std::vector<StructChild> struct_children;
        Handle owner_identity;
    };
    auto capture=[&]() {
        Metadata result;
        auto klass=reinterpret_cast<uintptr_t>(validate_object(object).object_class);
        std::set<uintptr_t> classes,fields;
        for(;klass && !result.field;klass=read<uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)
                throw HandlerError("array_scan_incomplete","Class chain contains a cycle");
            validate_class(reinterpret_cast<void*>(klass));
            for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)
                    throw HandlerError("array_scan_incomplete","Property chain contains a cycle");
                if(name(read<uint64_t>(field+0x20))==property_name){result.field=field;result.owner=klass;break;}
            }
        }
        if(!result.field)throw HandlerError("property_not_found","Array property not found");
        result.owner_identity=validate_object(reinterpret_cast<void*>(result.owner));
        result.field_name=read<uint64_t>(result.field+0x20);result.field_class=read<uintptr_t>(result.field+8);
        if(game_memory::field_type(result.field,[this](uint64_t value){return name(value);})!="ArrayProperty")
            throw HandlerError("unsupported_array_type","The selected property is not an exact ArrayProperty");
        result.offset=read<int32_t>(result.field+0x44);result.size=read<int32_t>(result.field+0x34);
        result.dim=read<int32_t>(result.field+0x30);result.owner_size=read<int32_t>(result.owner+0x58);
        result.flags=read<uint64_t>(result.field+0x38);result.array_flags=read<uint8_t>(result.field+0x70);
        if(result.dim!=1 || result.size!=16 || result.offset<0 || result.owner_size<16
           || result.offset>result.owner_size-16)
            throw HandlerError("unsupported_array_layout","The array requires a confirmed 16-byte header inside the object");
        result.inner=read<uintptr_t>(result.field+0x78);
        if(!result.inner)throw HandlerError("unsupported_array_layout","The array Inner property is missing");
        result.inner_name=read<uint64_t>(result.inner+0x20);result.inner_class=read<uintptr_t>(result.inner+8);
        result.stride=read<int32_t>(result.inner+0x34);result.inner_dim=read<int32_t>(result.inner+0x30);
        result.inner_offset=read<int32_t>(result.inner+0x44);result.inner_flags=read<uint64_t>(result.inner+0x38);
        result.element_type=game_memory::field_type(result.inner,[this](uint64_t value){return name(value);});
        const auto& type=result.element_type;
        const bool supported=(type=="IntProperty" && result.stride==4)
            || (type=="Int64Property" && result.stride==8)
            || (type=="UInt32Property" && result.stride==4)
            || (type=="UInt64Property" && result.stride==8)
            || (type=="FloatProperty" && result.stride==4)
            || (type=="DoubleProperty" && result.stride==8)
            || (type=="ByteProperty" && result.stride==1)
            || (type=="NameProperty" && result.stride==8)
            || (type=="ObjectProperty" && result.stride==8)
            || (type=="ClassProperty" && result.stride==8)
            || (type=="WeakObjectProperty" && result.stride==8)
            || type=="BoolProperty" || type=="EnumProperty" || type=="StructProperty";
        if(!supported || result.inner_dim!=1)
            throw HandlerError("unsupported_array_element","The array element type or ArrayDim is unsupported");
        if(type=="StructProperty") {
            auto& structure=result.element_struct.emplace();
            structure.field=result.inner;structure.next=read<uintptr_t>(result.inner+0x18);structure.field_class=result.inner_class;
            structure.owner=read<uint64_t>(result.inner+0x10);structure.name=result.inner_name;structure.type_name=read<uint64_t>(result.inner_class);
            structure.flags=result.inner_flags;structure.offset=result.inner_offset;structure.element_size=result.stride;structure.array_dim=result.inner_dim;
            structure.structure=read<uintptr_t>(result.inner+0x70);
            structure.identity=serial_identity(validate_object(reinterpret_cast<void*>(structure.structure)));
            validate_class(reinterpret_cast<void*>(structure.identity.object_class));
            structure.path=object_path(reinterpret_cast<void*>(structure.structure));structure.struct_name=name(structure.identity.name);
            structure.head=read<uintptr_t>(structure.structure+0x50);structure.super=read<uintptr_t>(structure.structure+0x40);
            structure.layout={read<int32_t>(structure.structure+0x58),read<int32_t>(structure.structure+0x5c),structure.super!=0,{}};
            if(result.stride<1 || structure.layout.properties_size!=result.stride || structure.super || structure.layout.min_alignment<1
               || (structure.layout.min_alignment&(structure.layout.min_alignment-1)) || result.stride%structure.layout.min_alignment)
                throw HandlerError("unsupported_array_layout","Struct elements require matching reflected size, alignment, and own fields");
            auto identity=[&](uintptr_t pointer) {
                const auto value=serial_identity(validate_object(reinterpret_cast<void*>(pointer)));
                return json{{"pointer",pointer},{"class",value.object_class},{"index",value.index},{"name",value.name},
                    {"serial",value.serial},{"class_index",value.class_index},{"class_name",value.class_name},{"class_serial",value.class_serial}};
            };
            std::set<uintptr_t> children;
            for(auto field=structure.head;field;field=read<uintptr_t>(field+0x18)) {
                if(!children.insert(field).second)throw HandlerError("array_scan_incomplete","Struct field chain contains a cycle");
                StructChild child;auto& metadata=child.field;
                metadata.pointer=field;metadata.next=read<uintptr_t>(field+0x18);metadata.field_class=read<uintptr_t>(field+8);
                metadata.owner=read<uint64_t>(field+0x10);metadata.name=read<uint64_t>(field+0x20);
                metadata.type_name=read<uint64_t>(metadata.field_class);metadata.flags=read<uint64_t>(field+0x38);
                metadata.component={name(metadata.name),read<int32_t>(field+0x44),
                    {name(metadata.type_name),read<int32_t>(field+0x34),read<int32_t>(field+0x30)}};
                auto& layout=metadata.component.layout;
                try {
                    if(layout.type=="BoolProperty") {
                        layout.field_size=read<uint8_t>(field+0x70);layout.byte_offset=read<uint8_t>(field+0x71);
                        layout.byte_mask=read<uint8_t>(field+0x72);layout.field_mask=read<uint8_t>(field+0x73);
                    } else if(layout.type=="ObjectProperty" || layout.type=="ClassProperty" || layout.type=="WeakObjectProperty") {
                        const auto declared=read<uintptr_t>(field+0x70);validate_class(reinterpret_cast<void*>(declared));
                        child.auxiliary["declared"]=identity(declared);
                        if(layout.type=="ClassProperty") {
                            const auto meta=read<uintptr_t>(field+0x78);validate_class(reinterpret_cast<void*>(meta));
                            child.auxiliary["meta"]=identity(meta);
                        }
                    } else if(layout.type=="EnumProperty" || layout.type=="ByteProperty") {
                        const auto enumeration=enum_layout(layout.type,layout.element_size,field);
                        child.auxiliary["underlying_type"]=enumeration.underlying_type;
                        if(layout.type=="EnumProperty") {
                            const auto underlying=read<uintptr_t>(field+0x70),klass=read<uintptr_t>(underlying+8);
                            child.auxiliary["underlying"]={underlying,klass,read<uint64_t>(klass),read<uint64_t>(underlying+0x10),
                                read<uintptr_t>(underlying+0x18),read<uint64_t>(underlying+0x20),read<int32_t>(underlying+0x30),
                                read<int32_t>(underlying+0x34),read<uint64_t>(underlying+0x38),read<int32_t>(underlying+0x44)};
                        }
                        if(enumeration.object)child.auxiliary["enum"]=identity(reinterpret_cast<uintptr_t>(enumeration.object));
                    } else if(layout.type=="StructProperty" || layout.type=="ArrayProperty") {
                        child.auxiliary["read_value"]=capture_read_value(field,layout.element_size,layout.array_dim);
                    }
                } catch(const HandlerError& error) {child.error={{"code",error.code()},{"message",error.what()}};}
                structure.layout.components.push_back(metadata.component);structure.components.push_back(metadata);
                result.struct_children.push_back(std::move(child));
            }
        } else if(type=="BoolProperty") {
            const auto byte_offset=read<uint8_t>(result.inner+0x71),mask=read<uint8_t>(result.inner+0x73);
            validate_bool_layout(result.stride,byte_offset,mask);
            result.auxiliary={byte_offset,mask};
        } else if(type=="ObjectProperty" || type=="ClassProperty" || type=="WeakObjectProperty") {
            result.auxiliary={read<uintptr_t>(result.inner+0x70)};
            validate_class(reinterpret_cast<void*>(result.auxiliary[0]));
            if(type=="ClassProperty") {
                result.auxiliary.push_back(read<uintptr_t>(result.inner+0x78));
                validate_class(reinterpret_cast<void*>(result.auxiliary[1]));
            }
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto layout=enum_layout(type,result.stride,result.inner);
            result.auxiliary={reinterpret_cast<uintptr_t>(layout.object)};
            if(type=="EnumProperty") {
                const auto underlying=read<uintptr_t>(result.inner+0x70);
                result.auxiliary.push_back(underlying);
                result.auxiliary.push_back(read<uintptr_t>(underlying+8));
                result.auxiliary.push_back(read<uint64_t>(read<uintptr_t>(underlying+8)));
                result.auxiliary.push_back(read<int32_t>(underlying+0x34));
                result.auxiliary.push_back(read<int32_t>(underlying+0x30));
            }
        }
        return result;
    };
    const auto metadata=capture();
    const auto object_address=reinterpret_cast<uintptr_t>(object);
    if(object_address>std::numeric_limits<uintptr_t>::max()-static_cast<uintptr_t>(metadata.offset)-16)
        throw HandlerError("invalid_array_header","The array header address overflows");
    const auto header_address=object_address+metadata.offset;
    const auto header=read<ArrayHeader>(header_address);
    const auto page=array_page(header,metadata.stride,metadata.inner_dim,offset,limit);
    std::vector<std::byte> copied(page.bytes);
    if(page.bytes && !game_memory::copy(copied.data(),reinterpret_cast<void*>(page.begin),page.bytes))
        throw HandlerError("invalid_memory","The array page is unavailable");
    auto revalidate=[&]() {
        const auto after=capture();
        if(!same_serial_identity(serial_identity(initial),serial_identity(validate_object(object)))
           || !same_serial_identity(serial_identity(metadata.owner_identity),serial_identity(after.owner_identity))
           || !same_array_header(header,read<ArrayHeader>(header_address))
           || metadata.field!=after.field || metadata.owner!=after.owner || metadata.field_class!=after.field_class
           || metadata.inner!=after.inner || metadata.inner_class!=after.inner_class
           || metadata.field_name!=after.field_name || metadata.inner_name!=after.inner_name
           || metadata.flags!=after.flags || metadata.inner_flags!=after.inner_flags
           || metadata.offset!=after.offset || metadata.size!=after.size || metadata.dim!=after.dim
           || metadata.owner_size!=after.owner_size || metadata.stride!=after.stride
           || metadata.inner_dim!=after.inner_dim || metadata.inner_offset!=after.inner_offset
           || metadata.array_flags!=after.array_flags || metadata.element_type!=after.element_type
           || metadata.auxiliary!=after.auxiliary || metadata.element_struct!=after.element_struct || metadata.struct_children!=after.struct_children)
            throw HandlerError("array_changed","The object, array header or layout changed during the read");
        resolve(handle);
    };
    revalidate();
    json items=json::array();bool partial=false;
    std::vector<std::pair<uintptr_t,ArrayHeader>> copied_headers;
    for(int index=0;index<page.count;++index) {
        json item={{"index",page.offset+index},{"supported",false}};
        try {
            const auto address=reinterpret_cast<uintptr_t>(copied.data()+static_cast<size_t>(index)*metadata.stride);
            json value;
            if(metadata.element_struct) {
                const auto element=std::span<const std::byte>(copied).subspan(
                    static_cast<size_t>(index)*metadata.stride,static_cast<size_t>(metadata.stride));
                value=json::object();json components=json::array();bool element_partial=false;
                for(const auto& child:metadata.struct_children) {
                    const auto& component=child.field.component;const auto& layout=component.layout;
                    json field={{"name",component.name},{"type",layout.type},{"element_size",layout.element_size},
                        {"offset",component.offset},{"array_dim",layout.array_dim},{"supported",false}};
                    try {
                        if(!child.error.is_null())throw HandlerError(child.error.at("code").get<std::string>(),child.error.at("message").get<std::string>());
                        if(layout.array_dim!=1 || layout.element_size<1 || component.offset<0
                           || layout.element_size>metadata.stride || component.offset>metadata.stride-layout.element_size)
                            throw HandlerError("unsupported_struct_field_layout","The struct field layout is unsupported");
                        const auto field_address=reinterpret_cast<uintptr_t>(element.data())+static_cast<uintptr_t>(component.offset);json field_value;
                        if(child.auxiliary.contains("read_value")) {
                            const auto decoded=decode_read_value(child.auxiliary.at("read_value"),
                                element.subspan(static_cast<size_t>(component.offset),static_cast<size_t>(layout.element_size)),
                                page.begin+static_cast<uintptr_t>(index)*metadata.stride+static_cast<uintptr_t>(component.offset),copied_headers);
                            field_value=decoded.at("value");
                            if(decoded.contains("components"))field["components"]=decoded.at("components");
                            if(decoded.contains("items"))field["items"]=decoded.at("items");
                            if(decoded.contains("total_count"))field["total_count"]=decoded.at("total_count");
                            if(decoded.contains("partial")) {field["partial"]=decoded.at("partial");element_partial|=decoded.at("partial").get<bool>();}
                        } else if(layout.type=="EnumProperty" || layout.type=="ByteProperty") {
                            const auto decoded=enum_property(field_address,layout.type,layout.element_size,child.field.pointer);
                            field_value=decoded.at("value");if(decoded.contains("enum"))field["enum"]=decoded.at("enum");
                        } else field_value=property_value(field_address,layout.type,layout.element_size,child.field.pointer);
                        if(field_value.is_null() && layout.type!="ObjectProperty" && layout.type!="ClassProperty" && layout.type!="WeakObjectProperty")
                            throw HandlerError("unsupported_struct_field_type","The struct field type is unsupported");
                        value[component.name]=field_value;field["value"]=std::move(field_value);field["supported"]=true;
                    } catch(const HandlerError& error) {
                        field["error"]={{"code",error.code()},{"message",error.what()}};element_partial=true;
                    }
                    components.push_back(std::move(field));
                }
                item["components"]=std::move(components);item["partial"]=element_partial;partial|=element_partial;
            } else if(metadata.element_type=="EnumProperty" || metadata.element_type=="ByteProperty") {
                const auto result=enum_property(address,metadata.element_type,metadata.stride,metadata.inner);
                value=result.at("value");if(result.contains("enum"))item["enum"]=result.at("enum");
            } else value=property_value(address,metadata.element_type,metadata.stride,metadata.inner);
            if(value.is_null() && metadata.element_type!="ObjectProperty" && metadata.element_type!="ClassProperty" && metadata.element_type!="WeakObjectProperty")
                throw HandlerError("unsupported_array_value","The element value is unsupported");
            item["value"]=std::move(value);item["supported"]=true;
        } catch(const HandlerError& error) {
            item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;
        }
        items.push_back(std::move(item));
    }
    revalidate();
    for(const auto& [address,copied_header]:copied_headers)
        if(!same_array_header(copied_header,read<ArrayHeader>(address)))
            throw HandlerError("array_changed","A nested array header changed during the read");
    json result={{"source","live_native"},{"session_id",handle_session_},{"process_id",GetCurrentProcessId()},
        {"thread_id",GetCurrentThreadId()},{"tick_count",ticks_.load()},{"handle",handle},
        {"property_name",property_name},{"element_type",metadata.element_type},{"element_size",metadata.stride},
        {"array_dim",metadata.inner_dim},{"total_count",header.count},{"offset",page.offset},
        {"next_offset",page.has_more?json(page.next_offset):json(nullptr)},{"has_more",page.has_more},
        {"truncated",page.has_more},{"consistency","single_tick_sequential"},{"partial",partial},{"items",items}};
    if(metadata.element_struct) {
        const auto& structure=*metadata.element_struct;
        json components=json::array();
        for(const auto& component:structure.layout.components)
            components.push_back({{"name",component.name},{"type",component.layout.type},
                {"element_size",component.layout.element_size},{"offset",component.offset},{"array_dim",component.layout.array_dim}});
        result["element_struct"]={{"struct_path",structure.path},{"struct_name",structure.struct_name},
            {"properties_size",structure.layout.properties_size},{"min_alignment",structure.layout.min_alignment},
            {"components",std::move(components)}};
    }
    return result;
}

} // namespace g1r_mcp
