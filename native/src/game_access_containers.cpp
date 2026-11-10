#include "g1r_mcp/game_access.hpp"
#include "g1r_mcp/game_memory.hpp"
#include "g1r_mcp/array_page.hpp"
#include "g1r_mcp/scalar_struct_values.hpp"
#include <array>
#include <limits>
#include <set>
#include <vector>

namespace g1r_mcp {
using json=nlohmann::json;
using game_memory::read;
namespace {
// UE 5.4.1 FScriptSet/FScriptMap share sparse storage and a default inline bit allocator.
struct ContainerHeader {
    uintptr_t data{};
    int32_t max_index{},capacity{};
    std::array<uint32_t,4> inline_bitmap{};
    uintptr_t bitmap_heap{};
    int32_t num_bits{},max_bits{},first_free{},free_count{};
    uint32_t hash_inline{},hash_padding{};
    uintptr_t hash_heap{};
    int32_t hash_size{};
    uint32_t tail_padding{};
    bool operator==(const ContainerHeader& other) const {
        return data==other.data && max_index==other.max_index && capacity==other.capacity
            && inline_bitmap==other.inline_bitmap && bitmap_heap==other.bitmap_heap
            && num_bits==other.num_bits && max_bits==other.max_bits && first_free==other.first_free
            && free_count==other.free_count && hash_inline==other.hash_inline
            && hash_heap==other.hash_heap && hash_size==other.hash_size;
    }
};
static_assert(sizeof(ContainerHeader)==80);
struct SetLayout {
    int32_t hash_next_offset{},hash_index_offset{},size{},alignment{},stride{};
    bool operator==(const SetLayout&) const=default;
};
static_assert(sizeof(SetLayout)==20);
std::vector<uint32_t> container_bitmap(const ContainerHeader& header) {
    const auto word_count=(static_cast<size_t>(header.num_bits)+31)/32;
    std::vector<uint32_t> words(word_count);
    if(header.bitmap_heap) {
        if(word_count && !game_memory::copy(words.data(),reinterpret_cast<void*>(header.bitmap_heap),word_count*sizeof(uint32_t)))
            throw HandlerError("invalid_memory","The container allocation bitmap is unavailable");
    } else {
        if(word_count>header.inline_bitmap.size())throw HandlerError("invalid_container_header","The allocation bitmap exceeds its inline storage");
        std::copy_n(header.inline_bitmap.begin(),word_count,words.begin());
    }
    return words;
}
struct ContainerPage {
    ContainerHeader header;
    std::vector<uint32_t> bitmap;
    std::vector<int32_t> occupied;
    std::vector<std::byte> copied;
    int32_t total_count{},count{};
};
ContainerPage copy_container_page(uintptr_t address,const SetLayout& layout,int32_t offset,size_t limit) {
    ContainerPage page;page.header=read<ContainerHeader>(address);const auto& header=page.header;
    array_page({header.data,header.max_index,header.capacity},layout.stride,1,0,
        header.max_index>0?static_cast<size_t>(header.max_index):size_t{1});
    if(header.num_bits!=header.max_index || header.max_bits<header.num_bits || header.free_count<0 || header.free_count>header.max_index)
        throw HandlerError("invalid_container_header","The sparse allocation metadata is inconsistent");
    page.bitmap=container_bitmap(header);
    for(int32_t sparse_index=0;sparse_index<header.max_index;++sparse_index)
        if(page.bitmap[static_cast<size_t>(sparse_index)/32]&(uint32_t{1}<<(sparse_index&31)))page.occupied.push_back(sparse_index);
    page.total_count=header.max_index-header.free_count;
    if(page.occupied.size()!=static_cast<size_t>(page.total_count))
        throw HandlerError("invalid_container_header","The sparse allocation bitmap does not match the occupied count");
    if(offset>page.total_count)throw HandlerError("invalid_container_page","The page offset exceeds the occupied count");
    page.count=static_cast<int32_t>(std::min<size_t>(static_cast<size_t>(page.total_count-offset),limit));
    page.copied.resize(static_cast<size_t>(page.count)*static_cast<size_t>(layout.stride));
    for(int32_t index=0;index<page.count;++index) {
        const auto sparse_index=page.occupied[static_cast<size_t>(offset+index)];
        const auto element_address=header.data+static_cast<uintptr_t>(sparse_index)*static_cast<uintptr_t>(layout.stride);
        if(!game_memory::copy(page.copied.data()+static_cast<size_t>(index)*static_cast<size_t>(layout.stride),
            reinterpret_cast<void*>(element_address),static_cast<size_t>(layout.stride)))
            throw HandlerError("invalid_memory","A container element is unavailable");
    }
    return page;
}
}
json GameAccess::capture_object_set_return(uintptr_t field,int size,int dim) const {
    const auto flags=read<uint64_t>(field+0x38);
    if(game_memory::field_type(field,[this](uint64_t value){return name(value);})!="SetProperty" || size!=80 || dim!=1
       || read<int32_t>(field+0x34)!=size || read<int32_t>(field+0x30)!=dim || !(flags&0x80) || !(flags&0x400)
       || (flags&(uint64_t{0x2}|uint64_t{0x08000000})))
        throw HandlerError("unsupported_signature","Object sets require an 80-byte return by value");
    const auto inner=read<uintptr_t>(field+0x70),klass=read<uintptr_t>(inner+8);
    const auto inner_name=read<uint64_t>(inner+0x20),type_name=read<uint64_t>(klass);
    if(name(type_name)!="ObjectProperty" || read<int32_t>(inner+0x34)!=8 || read<int32_t>(inner+0x30)!=1 || read<int32_t>(inner+0x44)!=0)
        throw HandlerError("unsupported_signature","The returned set requires exact ObjectProperty elements");
    const auto layout=read<SetLayout>(field+0x78);
    if(layout.size<8 || layout.stride<layout.size || layout.alignment<1 || (layout.alignment&(layout.alignment-1))
       || layout.stride%layout.alignment || layout.hash_next_offset<8 || layout.hash_next_offset>layout.size-4
       || layout.hash_index_offset<layout.hash_next_offset+4 || layout.hash_index_offset>layout.size-4)
        throw HandlerError("unsupported_container_layout","The reflected sparse element layout is unsupported");
    const auto declared=read<uintptr_t>(inner+0x70);validate_class(reinterpret_cast<void*>(declared));
    const auto identity=serial_identity(validate_object(reinterpret_cast<void*>(declared)));
    const json public_metadata={{"container_type","SetProperty"},{"element",{{"type","ObjectProperty"},{"element_size",8},{"array_dim",1},{"offset",0},
        {"declared_type",{{"name",object_name(reinterpret_cast<void*>(declared))},{"path",object_path(reinterpret_cast<void*>(declared))}}}}}};
    return {{"inner",{{"pointer",inner},{"next",read<uintptr_t>(inner+0x18)},{"field_class",klass},{"owner",read<uint64_t>(inner+0x10)},
        {"name",inner_name},{"type_name",type_name},{"flags",read<uint64_t>(inner+0x38)},{"offset",0},{"element_size",8},{"array_dim",1}}},
        {"declared",{{"pointer",declared},{"class",identity.object_class},{"index",identity.index},{"name",identity.name},
            {"serial",identity.serial},{"class_index",identity.class_index},{"class_name",identity.class_name},{"class_serial",identity.class_serial}}},
        {"layout",{{"hash_next_offset",layout.hash_next_offset},{"hash_index_offset",layout.hash_index_offset},{"size",layout.size},
            {"alignment",layout.alignment},{"stride",layout.stride}}},
        {"public",public_metadata}};
}
json GameAccess::read_object_set_return(uintptr_t field,uintptr_t address,const json& metadata) {
    if(capture_object_set_return(field,80,1)!=metadata)throw HandlerError("signature_changed","The returned set metadata changed after the call");
    const auto& reflected=metadata.at("layout");
    const SetLayout layout{reflected.at("hash_next_offset").get<int32_t>(),reflected.at("hash_index_offset").get<int32_t>(),
        reflected.at("size").get<int32_t>(),reflected.at("alignment").get<int32_t>(),reflected.at("stride").get<int32_t>()};
    const auto page=copy_container_page(address,layout,0,std::numeric_limits<size_t>::max());
    json items=json::array();bool partial=false;const auto inner=metadata.at("inner").at("pointer").get<uintptr_t>();
    for(int32_t index=0;index<page.count;++index) {
        json item={{"index",index},{"sparse_index",page.occupied[static_cast<size_t>(index)]},{"supported",false}};
        try {
            item["value"]=property_value(reinterpret_cast<uintptr_t>(page.copied.data()+static_cast<size_t>(index)*static_cast<size_t>(layout.stride)),
                "ObjectProperty",8,inner);item["supported"]=true;
        } catch(const HandlerError& error) {item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
        items.push_back(std::move(item));
    }
    const auto after=read<ContainerHeader>(address);
    if(capture_object_set_return(field,80,1)!=metadata || !(after==page.header) || container_bitmap(after)!=page.bitmap)
        throw HandlerError("container_changed","The returned set metadata, header or bitmap changed during the read");
    return {{"items",std::move(items)},{"total_count",page.total_count},{"max_index",page.header.max_index},{"partial",partial}};
}
json GameAccess::read_set(const json& params) {return read_container(params,false);}
json GameAccess::read_map(const json& params) {return read_container(params,true);}
json GameAccess::read_container(const json& params,bool map) {
    if(!params.is_object() || !params.contains("handle") || !params.at("handle").is_string()
       || !params.contains("property_name") || !params.at("property_name").is_string())
        throw HandlerError("invalid_container_params","String handle and property_name fields are required");
    const auto handle=params.at("handle").get<std::string>(),property_name=params.at("property_name").get<std::string>();
    if(handle.empty() || handle.find('\0')!=std::string::npos || property_name.empty() || property_name.find('\0')!=std::string::npos)
        throw HandlerError("invalid_container_params","handle and property_name must be nonempty and contain no NUL");
    auto integer=[&](const char* key,uint64_t default_value,uint64_t minimum,uint64_t maximum) {
        if(!params.contains(key))return default_value;
        const auto& value=params.at(key);
        if(!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>()<0)
           || value.get<uint64_t>()<minimum || value.get<uint64_t>()>maximum)
            throw HandlerError("invalid_container_params","offset and limit require integers within their representable ranges");
        return value.get<uint64_t>();
    };
    const auto offset=static_cast<int32_t>(integer("offset",0,0,std::numeric_limits<int32_t>::max()));
    const auto limit=static_cast<size_t>(integer("limit",64,1,std::numeric_limits<size_t>::max()));
    const std::string container_type=map?"MapProperty":"SetProperty";
    auto object=resolve(handle);const auto initial=serial_identity(validate_object(object));
    struct Metadata {
        scalar_struct_values::ComponentMetadata field,key,value;
        uintptr_t owner{};
        int32_t owner_size{},value_offset{};
        uint8_t map_flags{};
        ObjectSerialIdentity owner_identity;
        SetLayout layout;
        json auxiliary=json::object();
        bool operator==(const Metadata&) const=default;
    };
    auto capture_field=[&](uintptr_t field) {
        scalar_struct_values::ComponentMetadata result;
        result.pointer=field;result.next=read<uintptr_t>(field+0x18);result.field_class=read<uintptr_t>(field+8);
        result.owner=read<uint64_t>(field+0x10);result.name=read<uint64_t>(field+0x20);
        result.type_name=read<uint64_t>(result.field_class);result.flags=read<uint64_t>(field+0x38);
        result.component={name(result.name),read<int32_t>(field+0x44),
            {name(result.type_name),read<int32_t>(field+0x34),read<int32_t>(field+0x30)}};
        return result;
    };
    auto capture=[&]() {
        Metadata result;uintptr_t selected{};
        auto klass=reinterpret_cast<uintptr_t>(validate_object(object).object_class);
        std::set<uintptr_t> classes,fields;
        for(;klass && !selected;klass=read<uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)throw HandlerError("container_scan_incomplete","Class chain contains a cycle");
            validate_class(reinterpret_cast<void*>(klass));
            for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)throw HandlerError("container_scan_incomplete","Property chain contains a cycle");
                if(name(read<uint64_t>(field+0x20))==property_name){selected=field;result.owner=klass;break;}
            }
        }
        if(!selected)throw HandlerError("property_not_found","Container property not found");
        result.field=capture_field(selected);result.owner_identity=serial_identity(validate_object(reinterpret_cast<void*>(result.owner)));
        result.owner_size=read<int32_t>(result.owner+0x58);
        const auto& field=result.field.component;
        if(field.layout.type!=container_type)throw HandlerError("unsupported_container_type","The property does not match the requested container type");
        if(field.layout.array_dim!=1 || field.layout.element_size!=sizeof(ContainerHeader) || field.offset<0
           || result.owner_size<sizeof(ContainerHeader) || field.offset>result.owner_size-static_cast<int32_t>(sizeof(ContainerHeader)))
            throw HandlerError("unsupported_container_layout","The container requires an 80-byte header inside the object");
        result.key=capture_field(read<uintptr_t>(selected+0x70));
        result.layout=read<SetLayout>(selected+(map?0x84:0x78));
        const auto& key=result.key.component;
        if(key.layout.type!="NameProperty" || key.layout.element_size!=8 || key.layout.array_dim!=1 || key.offset!=0)
            throw HandlerError("unsupported_container_element","The container requires exact NameProperty keys or elements");
        if(map) {
            result.map_flags=read<uint8_t>(selected+0x98);
            if(result.map_flags&0x01)
                throw HandlerError("unsupported_container_layout","Memory image map allocation is unsupported");
            result.value=capture_field(read<uintptr_t>(selected+0x78));result.value_offset=read<int32_t>(selected+0x80);
            const auto& value=result.value.component;
            if(value.layout.type!="ObjectProperty" || value.layout.element_size!=8 || value.layout.array_dim!=1
               || value.offset!=result.value_offset || result.value_offset<8)
                throw HandlerError("unsupported_container_element","The map requires exact ObjectProperty values");
            const auto declared=read<uintptr_t>(result.value.pointer+0x70);validate_class(reinterpret_cast<void*>(declared));
            const auto identity=serial_identity(validate_object(reinterpret_cast<void*>(declared)));
            result.auxiliary["declared"]={{"pointer",declared},{"class",identity.object_class},{"index",identity.index},{"name",identity.name},
                {"serial",identity.serial},{"class_index",identity.class_index},{"class_name",identity.class_name},{"class_serial",identity.class_serial}};
            result.auxiliary["declared_type"]={{"name",object_name(reinterpret_cast<void*>(declared))},{"path",object_path(reinterpret_cast<void*>(declared))}};
        }
        const auto& layout=result.layout;
        if(layout.size<8 || layout.stride<layout.size || layout.alignment<1 || (layout.alignment&(layout.alignment-1))
           || layout.stride%layout.alignment || layout.hash_next_offset<8 || layout.hash_next_offset>layout.size-4
           || layout.hash_index_offset<layout.hash_next_offset+4 || layout.hash_index_offset>layout.size-4
           || (map && result.value_offset>layout.hash_next_offset-8))
            throw HandlerError("unsupported_container_layout","The reflected sparse element layout is unsupported");
        return result;
    };
    const auto metadata=capture();const auto object_address=reinterpret_cast<uintptr_t>(object);
    if(object_address>std::numeric_limits<uintptr_t>::max()-static_cast<uintptr_t>(metadata.field.component.offset)-sizeof(ContainerHeader))
        throw HandlerError("invalid_container_header","The container header address overflows");
    const auto header_address=object_address+static_cast<uintptr_t>(metadata.field.component.offset);
    const auto page=copy_container_page(header_address,metadata.layout,offset,limit);
    const auto& header=page.header;const auto& bitmap=page.bitmap;const auto& occupied=page.occupied;
    const auto& copied=page.copied;const auto total_count=page.total_count,count=page.count;
    auto revalidate=[&]() {
        const auto after=capture();const auto current_header=read<ContainerHeader>(header_address);
        if(!(metadata==after) || !(header==current_header) || bitmap!=container_bitmap(current_header)
           || !same_serial_identity(initial,serial_identity(validate_object(object))) || resolve(handle)!=object)
            throw HandlerError("container_changed","The object, container metadata, header or bitmap changed during the read");
    };
    revalidate();json items=json::array();bool partial=false;
    for(int32_t index=0;index<count;++index) {
        json item={{"index",offset+index},{"sparse_index",occupied[static_cast<size_t>(offset+index)]},{"supported",false}};
        try {
            const auto address=reinterpret_cast<uintptr_t>(copied.data()+static_cast<size_t>(index)*static_cast<size_t>(metadata.layout.stride));
            auto key=property_value(address,metadata.key.component.layout.type,metadata.key.component.layout.element_size,metadata.key.pointer);
            if(map) {
                item["key"]=std::move(key);
                item["value"]=property_value(address+static_cast<uintptr_t>(metadata.value_offset),metadata.value.component.layout.type,
                    metadata.value.component.layout.element_size,metadata.value.pointer);
            } else item["value"]=std::move(key);
            item["supported"]=true;
        } catch(const HandlerError& error) {item["error"]={{"code",error.code()},{"message",error.what()}};partial=true;}
        items.push_back(std::move(item));
    }
    revalidate();const auto next_offset=offset+count;const bool has_more=next_offset<total_count;
    auto public_field=[](const scalar_struct_values::ComponentMetadata& metadata) {
        const auto& component=metadata.component;
        return json{{"type",component.layout.type},{"element_size",component.layout.element_size},
            {"array_dim",component.layout.array_dim},{"offset",component.offset}};
    };
    json result={{"source","live_native"},{"session_id",handle_session_},{"process_id",GetCurrentProcessId()},
        {"thread_id",GetCurrentThreadId()},{"tick_count",ticks_.load()},{"handle",handle},{"property_name",property_name},
        {"container_type",container_type},{"total_count",total_count},{"max_index",header.max_index},{"offset",offset},
        {"next_offset",has_more?json(next_offset):json(nullptr)},{"has_more",has_more},{"truncated",has_more},
        {"consistency","copied_slots_then_metadata_revalidated"},{"partial",partial},{"items",std::move(items)}};
    if(map) {
        result["key"]=public_field(metadata.key);result["value"]=public_field(metadata.value);
        result["value"]["declared_type"]=metadata.auxiliary.at("declared_type");result["value_offset"]=metadata.value_offset;
    } else result["element"]=public_field(metadata.key);
    return result;
}
} // namespace g1r_mcp
