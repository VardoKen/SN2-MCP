#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/game_memory.hpp"
#include "sn2_mcp/object_array_parameter_layout.hpp"
#include "sn2_mcp/vector_parameter_direction.hpp"
#include "sn2_mcp/scalar_struct_values.hpp"

namespace sn2_mcp {

nlohmann::json GameAccess::capture_object_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const {
    const auto capture=[&] {
        return object_array_parameter_layout::capture(field,size,dim,flags,
            []<class Value>(std::uintptr_t address) {return game_memory::read<Value>(address);},
            [this](std::uint64_t value) {return name(value);},
            [this](std::uintptr_t object) {return serial_identity(validate_object(reinterpret_cast<void*>(object)));},
            [this](std::uintptr_t object_class) {validate_class(reinterpret_cast<void*>(object_class));},
            [this](std::uintptr_t object) {return object_path(reinterpret_cast<void*>(object));});
    };
    const auto before=capture();
    const auto after=capture();
    if(before!=after)
        throw HandlerError("object_array_metadata_changed","Reflected UObject array metadata changed during validation");
    return before;
}

nlohmann::json GameAccess::capture_scalar_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const {
    using json=nlohmann::json;
    using game_memory::read;
    const auto capture=[&]() {
        const auto field_snapshot=[&](std::uintptr_t pointer) {
            const auto field_class=read<std::uintptr_t>(pointer+8);
            const auto type_name=read<std::uint64_t>(field_class);
            return json{{"pointer",pointer},{"next",read<std::uintptr_t>(pointer+0x18)},
                {"owner",read<std::uint64_t>(pointer+0x10)},{"field_class",field_class},{"type_name",type_name},{"type",name(type_name)},
                {"name",read<std::uint64_t>(pointer+0x20)},{"field_flags",read<std::uint32_t>(pointer+0x28)},
                {"element_size",read<std::int32_t>(pointer+0x34)},{"array_dim",read<std::int32_t>(pointer+0x30)},
                {"offset",read<std::int32_t>(pointer+0x44)},{"flags",read<std::uint64_t>(pointer+0x38)}};
        };
        auto outer=field_snapshot(field);
        const auto offset=outer.at("offset").get<std::int32_t>();
        if(outer.at("type")!="ArrayProperty" || size!=16 || dim!=1 || outer.at("element_size")!=size || outer.at("array_dim")!=dim
           || offset<0 || offset%8 || outer.at("flags")!=flags)
            throw HandlerError("unsupported_signature","Scalar arrays require a 16-byte input layout");
        if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
            throw HandlerError("unsupported_signature","Scalar arrays require value or const reference input");
        outer["array_flags"]=read<std::uint8_t>(field+0x70);
        const auto inner_pointer=read<std::uintptr_t>(field+0x78);
        outer["inner"]=inner_pointer;
        auto inner=field_snapshot(inner_pointer);
        const auto scalar_type=inner.at("type").get<std::string>();
        const bool numeric=scalar_type=="BoolProperty" || scalar_type=="IntProperty" || scalar_type=="Int64Property"
            || scalar_type=="UInt32Property" || scalar_type=="UInt64Property" || scalar_type=="FloatProperty" || scalar_type=="DoubleProperty";
        const auto element_size=(numeric || scalar_type=="StructProperty")?inner.at("element_size").get<int>():scalar_type=="NameProperty"?8:scalar_type=="StrProperty"?16:0;
        scalar_outputs::Layout scalar_layout{scalar_type,element_size,1};
        if(numeric) {
            if(scalar_type=="BoolProperty") {
                scalar_layout.field_size=read<uint8_t>(inner_pointer+0x70);scalar_layout.byte_offset=read<uint8_t>(inner_pointer+0x71);
                scalar_layout.byte_mask=read<uint8_t>(inner_pointer+0x72);scalar_layout.field_mask=read<uint8_t>(inner_pointer+0x73);
                inner["bool_layout"]={{"field_size",scalar_layout.field_size},{"byte_offset",scalar_layout.byte_offset},
                    {"byte_mask",scalar_layout.byte_mask},{"field_mask",scalar_layout.field_mask}};
            }
            scalar_outputs::validate_layout(scalar_layout);
        }
        if(element_size<1 || inner.at("element_size")!=element_size || inner.at("array_dim")!=1 || inner.at("offset")!=0)
            throw HandlerError("unsupported_signature","Scalar arrays require int32, FName, or own scalar DTO elements");
        json captured={{"outer",std::move(outer)},{"inner",std::move(inner)},{"scalar_type",scalar_type},
            {"public",{{"container_type","ArrayProperty"},{"element",{{"type",scalar_type},
                {"element_size",element_size},{"array_dim",1},{"offset",0}}}}}};
        if(scalar_type=="BoolProperty")captured["public"]["element"]["bool_layout"]=captured.at("inner").at("bool_layout");
        if(scalar_type=="StructProperty") {
            const auto structure=capture_pod_struct_parameter(inner_pointer,element_size,1);
            captured["struct"]=structure;
            captured["public"]["element"]["struct"]=pod_struct_metadata(structure);
        }
        return captured;
    };
    const auto before=capture();
    if(before!=capture())throw HandlerError("scalar_array_metadata_changed","Reflected scalar array metadata changed during validation");
    return before;
}
nlohmann::json GameAccess::capture_enum_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const {
    using json=nlohmann::json;
    using game_memory::read;
    const auto capture=[&]() {
        const auto field_snapshot=[&](std::uintptr_t pointer) {
            const auto field_class=read<std::uintptr_t>(pointer+8);
            const auto type_name=read<std::uint64_t>(field_class);
            return json{{"pointer",pointer},{"next",read<std::uintptr_t>(pointer+0x18)},
                {"owner",read<std::uint64_t>(pointer+0x10)},{"field_class",field_class},{"type_name",type_name},{"type",name(type_name)},
                {"name",read<std::uint64_t>(pointer+0x20)},{"field_flags",read<std::uint32_t>(pointer+0x28)},
                {"element_size",read<std::int32_t>(pointer+0x34)},{"array_dim",read<std::int32_t>(pointer+0x30)},
                {"offset",read<std::int32_t>(pointer+0x44)},{"flags",read<std::uint64_t>(pointer+0x38)}};
        };
        auto outer=field_snapshot(field);
        const auto offset=outer.at("offset").get<std::int32_t>();
        if(outer.at("type")!="ArrayProperty" || size!=16 || dim!=1 || outer.at("element_size")!=size || outer.at("array_dim")!=dim
           || offset<0 || offset%8 || outer.at("flags")!=flags || !(flags&0x80) || (flags&(std::uint64_t{0x100}|std::uint64_t{0x08000000}|std::uint64_t{0x400})))
            throw HandlerError("unsupported_signature","Enum arrays require a 16-byte input by value");
        outer["array_flags"]=read<std::uint8_t>(field+0x70);
        const auto inner_pointer=read<std::uintptr_t>(field+0x78);
        outer["inner"]=inner_pointer;
        auto inner=field_snapshot(inner_pointer);
        if(inner.at("type")!="EnumProperty" || inner.at("element_size")!=1 || inner.at("array_dim")!=1 || inner.at("offset")!=0)
            throw HandlerError("unsupported_signature","The enum array requires one-byte EnumProperty elements");
        const auto layout=enum_layout("EnumProperty",1,inner_pointer);
        if(layout.underlying_type!="ByteProperty")
            throw HandlerError("unsupported_enum_underlying_layout","The enum array requires ByteProperty underlying values");
        const auto underlying_pointer=read<std::uintptr_t>(inner_pointer+0x70);
        inner["underlying"]=underlying_pointer;inner["enum"]=reinterpret_cast<std::uintptr_t>(layout.object);
        auto underlying=field_snapshot(underlying_pointer);
        const auto table=enum_table(layout.object);
        json entries=json::array(),public_entries=json::array();
        for(const auto& entry:table.entries) {
            const auto label=name(entry.name);
            entries.push_back({{"name",entry.name},{"label",label},{"value",entry.value}});
            public_entries.push_back({{"name",label},{"value",entry.value}});
        }
        const auto enum_address=reinterpret_cast<std::uintptr_t>(layout.object);
        const json enumeration={{"identity",object_array_parameter_layout::identity_json(serial_identity(table.identity))},
            {"path",table.path},{"entries",std::move(entries)},{"data",read<std::uintptr_t>(enum_address+0x40)},
            {"count",read<std::int32_t>(enum_address+0x48)},{"capacity",read<std::int32_t>(enum_address+0x4c)}};
        return json{{"outer",std::move(outer)},{"inner",std::move(inner)},{"underlying",std::move(underlying)},
            {"underlying_type",layout.underlying_type},{"enum",enumeration},
            {"public",{{"container_type","ArrayProperty"},{"element",{{"type","EnumProperty"},{"element_size",1},{"array_dim",1},{"offset",0},
                {"enum",{{"path",table.path},{"underlying_type",layout.underlying_type},{"entries",std::move(public_entries)}}}}}}}};
    };
    const auto before=capture();
    if(before!=capture())throw HandlerError("enum_array_metadata_changed","Reflected enum array metadata changed during validation");
    return before;
}

} // namespace sn2_mcp
