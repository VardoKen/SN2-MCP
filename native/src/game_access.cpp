#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/transport.hpp"
#include "sn2_mcp/scalar_arguments.hpp"
#include "sn2_mcp/object_arguments.hpp"
#include "sn2_mcp/class_arguments.hpp"
#include "sn2_mcp/object_query.hpp"
#include "sn2_mcp/function_query.hpp"
#include "sn2_mcp/function_contract.hpp"
#include "sn2_mcp/function_call_support.hpp"
#include "sn2_mcp/selected_property_query.hpp"
#include "sn2_mcp/name_return.hpp"
#include "sn2_mcp/name_argument.hpp"
#include "sn2_mcp/string_return.hpp"
#include "sn2_mcp/array_page.hpp"
#include "sn2_mcp/enum_values.hpp"
#include "sn2_mcp/enum_arguments.hpp"
#include "sn2_mcp/vector_layout.hpp"
#include "sn2_mcp/vector_arguments.hpp"
#include "sn2_mcp/vector_parameter_direction.hpp"
#include "sn2_mcp/parameter_direction.hpp"
#include "sn2_mcp/scalar_outputs.hpp"
#include "sn2_mcp/scalar_struct_values.hpp"
#include "sn2_mcp/scalar_struct_arguments.hpp"
#include "sn2_mcp/rotator_arguments.hpp"
#include "sn2_mcp/aligned_parameter_buffer.hpp"
#include "sn2_mcp/transform_layout.hpp"
#include "sn2_mcp/transform_arguments.hpp"
#include "sn2_mcp/string_arguments.hpp"
#include "sn2_mcp/string_input_lifetime.hpp"
#include "sn2_mcp/object_array_input_lifetime.hpp"
#include "sn2_mcp/linear_color_layout.hpp"
#include "sn2_mcp/linear_color_arguments.hpp"
#include "sn2_mcp/cleanup_once.hpp"
#include "sn2_mcp/native_exception.hpp"
#include "sn2_mcp/process_identity.hpp"
#include "sn2_mcp/request_identity.hpp"
#include "sn2_mcp/image_patterns.hpp"
#include <MinHook.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <initializer_list>
#include <optional>
#include <sstream>
#include <set>
#include <vector>

#if defined(SN2_MOD_RUNTIME)
extern "C" void SN2ModDiagnostic(const char* text) noexcept;
#endif

namespace sn2_mcp {
using json = nlohmann::json;
namespace {
uint64_t steady_ms() {
    return static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}
constexpr uint64_t kRequestWaitMs=2500;
std::chrono::duration<double,std::milli> request_wait(const json& envelope) {
    double milliseconds=kRequestWaitMs;
    if(envelope.contains("wait_timeout_ms")) {
        const auto& value=envelope.at("wait_timeout_ms");
        if(!value.is_number() || !std::isfinite(milliseconds=value.get<double>()) || milliseconds<=0)
            throw HandlerError("invalid_arguments","wait_timeout_ms must be a finite positive number",
                {{"executed",false},{"may_have_executed",false},{"do_not_retry",false}});
    }
    return std::chrono::duration<double,std::milli>{milliseconds};
}
constexpr uint64_t kReferenceParm=vector_parameter_direction::reference_parm;
constexpr uint64_t kConstParm=vector_parameter_direction::const_parm;
// UE 5.6 Object.h IsValid rejects RF_MirroredGarbage before BeginDestroy.
constexpr uint32_t kInvalidObjectFlags=0x40018000;
bool supported_scalar_output_type(const std::string& type) {
    return type=="BoolProperty" || type=="IntProperty" || type=="Int64Property"
        || type=="UInt32Property" || type=="UInt64Property" || type=="FloatProperty" || type=="DoubleProperty";
}
scalar_outputs::Layout capture_scalar_output_layout(uintptr_t field,const std::string& type,int size,int dim);
json scalar_output_metadata(const scalar_outputs::Layout& layout) {
    json result={{"type",layout.type},{"element_size",layout.element_size},{"array_dim",layout.array_dim}};
    if(layout.type=="BoolProperty")result["bool_layout"]={{"field_size",layout.field_size},{"byte_offset",layout.byte_offset},
        {"byte_mask",layout.byte_mask},{"field_mask",layout.field_mask}};
    return result;
}
json vector_snapshot_json(const vector_layout::Snapshot& value,bool raw=true) {
    json components=json::array();
    for(const auto& child:value.components) {
        json item={{"name",child.component.name},{"type",child.component.type},{"element_size",child.component.element_size},
            {"offset",child.component.offset},{"array_dim",child.component.array_dim}};
        if(raw)item.update({{"pointer",child.pointer},{"next",child.next},{"field_class",child.field_class},
            {"owner",child.owner},{"field_name",child.name},{"type_name",child.type_name}});
        components.push_back(std::move(item));
    }
    json result={{"struct_path",value.path},{"struct_name",value.struct_name},{"properties_size",value.layout.properties_size},
        {"min_alignment",value.layout.min_alignment},{"components",components}};
    if(raw) {
        const auto& identity=value.identity;
        result.update({{"field",value.field},{"field_class",value.field_class},{"field_owner",value.field_owner},
            {"field_name",value.field_name},{"field_type_name",value.field_type_name},{"structure",value.structure},
            {"head",value.head},{"super",value.super},{"element_size",value.element_size},{"array_dim",value.array_dim},
            {"identity",{{"object",identity.object},{"object_class",identity.object_class},{"index",identity.index},
                {"name",identity.name},{"serial",identity.serial},{"class_index",identity.class_index},
                {"class_name",identity.class_name},{"class_serial",identity.class_serial}}}});
    }
    return result;
}
json transform_snapshot_json(const transform_layout::Snapshot& value,bool raw=true) {
    json members=json::array();
    for(const auto& member:value.members) {
        json item={{"name",member.layout.name},{"type",member.layout.type},{"element_size",member.layout.element_size},
            {"offset",member.layout.offset},{"array_dim",member.layout.array_dim}};
        if(member.layout.name=="Rotation")item["struct"]=vector_snapshot_json(value.rotation.metadata,raw);
        else if(member.layout.name=="Translation")item["struct"]=vector_snapshot_json(value.translation,raw);
        else if(member.layout.name=="Scale3D")item["struct"]=vector_snapshot_json(value.scale3d,raw);
        if(raw)item.update({{"pointer",member.pointer},{"next",member.next},{"field_class",member.field_class},
            {"structure",member.structure},{"owner",member.owner},{"field_name",member.name},{"type_name",member.type_name},{"flags",member.flags}});
        members.push_back(std::move(item));
    }
    json result={{"struct_path",value.path},{"struct_name",value.struct_name},{"properties_size",value.layout.properties_size},
        {"min_alignment",value.layout.min_alignment},{"members",members}};
    if(raw) {
        const auto& identity=value.identity;
        result.update({{"field",value.field},{"field_class",value.field_class},{"field_owner",value.field_owner},
            {"field_name",value.field_name},{"field_type_name",value.field_type_name},{"field_flags",value.field_flags},
            {"structure",value.structure},{"head",value.head},{"super",value.super},{"element_size",value.element_size},{"array_dim",value.array_dim},
            {"identity",{{"object",identity.object},{"object_class",identity.object_class},{"index",identity.index},{"name",identity.name},
                {"serial",identity.serial},{"class_index",identity.class_index},{"class_name",identity.class_name},{"class_serial",identity.class_serial}}}});
    }
    return result;
}
json linear_color_snapshot_json(const linear_color_layout::Snapshot& value,bool raw=true) {
    json components=json::array();
    for(const auto& child:value.components) {
        json item={{"name",child.component.name},{"type",child.component.type},{"element_size",child.component.element_size},
            {"offset",child.component.offset},{"array_dim",child.component.array_dim}};
        if(raw)item.update({{"pointer",child.pointer},{"next",child.next},{"field_class",child.field_class},{"owner",child.owner},
            {"field_name",child.name},{"type_name",child.type_name},{"flags",child.flags},{"field_flags",child.field_flags}});
        components.push_back(std::move(item));
    }
    json result={{"struct_path",value.path},{"struct_name",value.struct_name},{"properties_size",value.layout.properties_size},
        {"min_alignment",value.layout.min_alignment},{"components",components}};
    if(raw) {
        const auto& identity=value.identity;
        result.update({{"field",value.field},{"next",value.next},{"field_class",value.field_class},{"field_owner",value.field_owner},
            {"field_name",value.field_name},{"field_type_name",value.field_type_name},{"flags",value.flags},{"field_flags",value.field_flags},
            {"offset",value.offset},{"structure",value.structure},{"head",value.head},{"super",value.super},{"element_size",value.element_size},
            {"array_dim",value.array_dim},{"identity",{{"object",identity.object},{"object_class",identity.object_class},{"index",identity.index},
                {"name",identity.name},{"serial",identity.serial},{"class_index",identity.class_index},{"class_name",identity.class_name},{"class_serial",identity.class_serial}}}});
    }
    return result;
}
void diagnostic(const char* text) {
#if defined(SN2_MOD_RUNTIME)
    // A compiled package is immutable. The mod's status snapshot owns
    // bootstrap diagnostics instead of creating files next to its DLL.
    SN2ModDiagnostic(text);
#else
    HMODULE self{};GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&diagnostic),&self);
    wchar_t path[32768]{};GetModuleFileNameW(self,path,32768);std::wstring file(path);file=file.substr(0,file.find_last_of(L"\\/"))+L"\\native-tick.log";
    std::ofstream output(file,std::ios::app);output<<text<<"\n";
#endif
}
uintptr_t kObjects{};
uintptr_t kNames{};
uintptr_t kTick{};
uintptr_t kProcessEvent{};
uintptr_t kFree{};
uintptr_t kStringConstruct{};
uintptr_t kNameConstruct{};
uintptr_t kObjectArrayRealloc{};
json kInstalledTickHook=nullptr;

bool copy_memory(void* destination, const void* source, size_t count) {
    if (!source) return false;
    __try { std::memcpy(destination, source, count); return true; }
    __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}
template<class T> T read(uintptr_t address) {
    T value{};
    if (!copy_memory(&value, reinterpret_cast<void*>(address), sizeof(T)))
        throw HandlerError("invalid_memory", "Object memory is unavailable");
    return value;
}
bool call_event(void* function, void* object, void* event, void* params,native_exception::Diagnostic* fault) {
    __try { reinterpret_cast<void(__fastcall*)(void*,void*,void*)>(function)(object,event,params); return true; }
    __except(native_exception::capture(GetExceptionInformation(),fault)) { return false; }
}
bool free_string(uintptr_t free_address, void* memory) {
    if (!memory) return true;
    __try { reinterpret_cast<void(__fastcall*)(void*)>(free_address)(memory); return true; }
    __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}
bool construct_string(uintptr_t constructor,sn2_mcp::StringHeader* header,const char16_t* text,int units) {
    __try {reinterpret_cast<void(__fastcall*)(sn2_mcp::StringHeader*,const char16_t*,int)>(constructor)(header,text,units);return true;}
    __except(EXCEPTION_EXECUTE_HANDLER) {return false;}
}
bool construct_name(uintptr_t constructor,uint64_t* value,const char16_t* text) {
    // Current 0x1479E60 branch inserts a missing base only for FNAME_Add (1).
    __try {reinterpret_cast<uint64_t*(__fastcall*)(uint64_t*,const char16_t*,uint32_t)>(constructor)(value,text,1);return true;}
    __except(EXCEPTION_EXECUTE_HANDLER) {return false;}
}
bool allocate_object_array(uintptr_t allocator,size_t bytes,void** result) {
    __try {*result=reinterpret_cast<void*(__fastcall*)(void*,size_t,uint32_t)>(allocator)(nullptr,bytes,8);return true;}
    __except(EXCEPTION_EXECUTE_HANDLER) {return false;}
}
std::string utf8(const wchar_t* value, int length) {
    if (length <= 0) return {};
    int count = WideCharToMultiByte(CP_UTF8, WC_ERR_INVALID_CHARS, value,length,nullptr,0,nullptr,nullptr);
    if (!count) throw HandlerError("invalid_string", "Invalid UTF-16 string");
    std::string result(count,'\0');
    WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,value,length,result.data(),count,nullptr,nullptr);
    return result;
}
json hook_address(std::uintptr_t address) {
    json result={{"address",address?json(address):json(nullptr)},{"module",nullptr},
        {"committed",false},{"executable",false}};
    if(!address)return result;
    MEMORY_BASIC_INFORMATION region{};
    if(!VirtualQuery(reinterpret_cast<const void*>(address),&region,sizeof(region))) {
        result["error"]="memory_region_unavailable";return result;
    }
    result["allocation_base"]=reinterpret_cast<std::uintptr_t>(region.AllocationBase);
    result["region_base"]=reinterpret_cast<std::uintptr_t>(region.BaseAddress);
    result["region_bytes"]=region.RegionSize;
    result["protection"]=region.Protect;
    result["committed"]=region.State==MEM_COMMIT;
    result["executable"]=region.State==MEM_COMMIT && !(region.Protect&(PAGE_GUARD|PAGE_NOACCESS))
        && (region.Protect&(PAGE_EXECUTE|PAGE_EXECUTE_READ|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY));
    HMODULE owner{};
    if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(address),&owner)) {
        wchar_t path[32768]{};const auto length=GetModuleFileNameW(owner,path,32768);
        if(length && length<32768)result["module"]=utf8(path,static_cast<int>(length));
    }
    return result;
}
json sample_hook_jump(std::uintptr_t address) {
    auto result=hook_address(address);
    result.update({{"encoding",nullptr},{"destination",nullptr},{"bytes",nullptr},
        {"sample_stable",false}});
    std::array<std::uint8_t,16> before{},after{};
    if(!address || !copy_memory(before.data(),reinterpret_cast<const void*>(address),before.size())) {
        result["state"]="unreadable";return result;
    }
    result["bytes"]=before;
    std::uintptr_t destination{},slot{};std::uint64_t pointer{},pointer_after{};
    bool indirect{},first_pointer_read{};
    if(before[0]==0xe9) {
        std::int32_t displacement{};std::memcpy(&displacement,before.data()+1,sizeof(displacement));
        const auto target=static_cast<std::int64_t>(address)+5+displacement;
        if(target>0)destination=static_cast<std::uintptr_t>(target);
        result["encoding"]="E9_REL32";
    } else if(before[0]==0xff && before[1]==0x25) {
        indirect=true;
        std::int32_t displacement{};std::memcpy(&displacement,before.data()+2,sizeof(displacement));
        const auto target=static_cast<std::int64_t>(address)+6+displacement;
        if(target>0) {
            slot=static_cast<std::uintptr_t>(target);
            first_pointer_read=copy_memory(&pointer,reinterpret_cast<const void*>(slot),sizeof(pointer));
            if(first_pointer_read)
                destination=static_cast<std::uintptr_t>(pointer);
        }
        result["encoding"]="FF25_RIP";result["indirect_slot"]=slot?json(slot):json(nullptr);
    }
    const bool code_stable=copy_memory(after.data(),reinterpret_cast<const void*>(address),after.size()) && before==after;
    const bool pointer_stable=!indirect || (slot && first_pointer_read
        && copy_memory(&pointer_after,reinterpret_cast<const void*>(slot),sizeof(pointer_after))
        && pointer==pointer_after);
    result["sample_stable"]=code_stable && pointer_stable;
    result["state"]=indirect && (!slot || !first_pointer_read)?"indirect_slot_unavailable":
        code_stable && pointer_stable?"sampled":"changed_during_sample";
    result["destination"]=destination?hook_address(destination):json(nullptr);
    return result;
}
json current_tick_hook(std::uintptr_t entry,std::uintptr_t expected,std::uintptr_t trampoline) {
    json result={{"method","advisory_entry_and_single_relay_memory_sample"},
        {"expected_detour",hook_address(expected)},{"original_trampoline",hook_address(trampoline)},
        {"entry",sample_hook_jump(entry)},{"relay",nullptr},{"resolved_destination",nullptr},
        {"relation","not_observed"},{"full_chain_validated",false},
        {"route_sample_consistency","sequential_non_atomic"},
        {"limits",{"Two matching reads are not an atomic snapshot or proof that Tick reaches this address",
            "Only an entry jump and one relay are decoded; a different destination may preserve a longer chain"}}};
    if(!result.at("entry").value("sample_stable",false))return result;
    const auto first=result.at("entry").at("destination");
    if(first.is_null()) {result["relation"]="entry_is_not_a_supported_jump";return result;}
    const auto first_address=first.at("address").get<std::uintptr_t>();
    auto resolved=first_address;
    if(first_address!=expected && first.value("executable",false)) {
        result["relay"]=sample_hook_jump(first_address);
        if(!result.at("relay").value("sample_stable",false)) {
            result["relation"]="relay_changed_or_unreadable";return result;
        }
        if(!result.at("relay").at("destination").is_null())
            resolved=result.at("relay").at("destination").at("address").get<std::uintptr_t>();
    }
    result["resolved_destination"]=hook_address(resolved);
    result["relation"]=resolved==expected?"expected_detour_observed":"different_destination_or_longer_chain";
    return result;
}
std::string copied_string(const sn2_mcp::StringHeader& header) {
    sn2_mcp::validate_string_header(header);
    std::vector<wchar_t> text(static_cast<size_t>(header.count));
    if(header.count && !copy_memory(text.data(),reinterpret_cast<void*>(header.data),
                                   static_cast<size_t>(header.count)*sizeof(wchar_t)))
        throw HandlerError("invalid_memory","String memory is unavailable");
    if(header.count && text.back()==0)text.pop_back();
    return utf8(text.data(),static_cast<int>(text.size()));
}
std::size_t limit_of(const json& params,std::size_t default_value=50) {
    if(!params.contains("limit"))return default_value;
    const auto& value=params.at("limit");
    if(!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>()<1)
       || value.get<uint64_t>()<1 || value.get<uint64_t>()>std::numeric_limits<std::size_t>::max())
        throw HandlerError("invalid_limit","limit must be a positive integer representable by size_t");
    return static_cast<std::size_t>(value.get<uint64_t>());
}
std::size_t offset_of(const json& params) {
    if(!params.contains("offset"))return 0;
    const auto& value=params.at("offset");
    if(!value.is_number_integer() || (!value.is_number_unsigned() && value.get<std::int64_t>()<0)
       || value.get<std::uint64_t>()>std::numeric_limits<std::size_t>::max())
        throw HandlerError("invalid_offset","offset must be a nonnegative integer representable by size_t");
    return static_cast<std::size_t>(value.get<std::uint64_t>());
}
bool contains(const std::string& value,const std::string& query) {
    std::string a=value,b=query;
    std::transform(a.begin(),a.end(),a.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
    std::transform(b.begin(),b.end(),b.begin(),[](unsigned char c){return static_cast<char>(std::tolower(c));});
    return a.find(b)!=std::string::npos;
}
std::string field_type(uintptr_t field, const std::function<std::string(uint64_t)>& get_name) {
    auto type=read<uintptr_t>(field+8); return get_name(read<uint64_t>(type));
}
json scalar(uintptr_t address,const std::string& type,int size,uintptr_t field) {
    if(type=="BoolProperty") {auto offset=read<uint8_t>(field+0x71),mask=read<uint8_t>(field+0x73);validate_bool_layout(size,offset,mask);return (read<uint8_t>(address+offset)&mask)!=0;}
    if(type=="IntProperty" && size==4) return read<int32_t>(address);
    if(type=="Int64Property" && size==8) return read<int64_t>(address);
    if(type=="UInt32Property" && size==4) return read<uint32_t>(address);
    if(type=="UInt64Property" && size==8) return read<uint64_t>(address);
    if(type=="ByteProperty" && size==1) return read<uint8_t>(address);
    if(type=="FloatProperty" && size==4) {auto v=read<float>(address); return std::isfinite(v)?json(v):json(nullptr);}
    if(type=="DoubleProperty" && size==8) {auto v=read<double>(address);return std::isfinite(v)?json(v):json(nullptr);}
    if(type=="StrProperty" && size==16) {
        return copied_string(read<sn2_mcp::StringHeader>(address));
    }
    return nullptr;
}
scalar_outputs::Layout capture_scalar_output_layout(uintptr_t field,const std::string& type,int size,int dim) {
    scalar_outputs::Layout layout{type,size,dim};
    if(type=="BoolProperty") {
        layout.field_size=read<uint8_t>(field+0x70);layout.byte_offset=read<uint8_t>(field+0x71);
        layout.byte_mask=read<uint8_t>(field+0x72);layout.field_mask=read<uint8_t>(field+0x73);
    }
    scalar_outputs::validate_layout(layout);return layout;
}
}

GameAccess* GameAccess::active_{};
uintptr_t GameAccess::ensure_image_base() {
    if(base_)return base_;
    const auto candidate_base=reinterpret_cast<uintptr_t>(GetModuleHandleW(nullptr));
    const auto executable=sn2::loader::comparable_path(sn2::loader::process_image(GetCurrentProcess()));
    if(!executable.ends_with(L"\\subnautica2\\binaries\\win64\\subnautica2-win64-shipping.exe"))
        throw HandlerError("target_process_mismatch","The native target is not the configured Subnautica 2 Shipping process");
    const image_patterns::Image image(candidate_base);
    const auto discover=[&](RuntimeBinding& binding,std::string_view label,std::string_view pattern,
                             std::size_t instruction_offset=0,std::size_t displacement_offset=0,
                             std::size_t instruction_size=0,std::size_t target_offset=0) {
        try {
            const auto match=image.find_unique(label,pattern);
            const auto rva=instruction_size?image.rip_target(match+instruction_offset,displacement_offset,instruction_size)+target_offset:match;
            if(!image.contains(rva,sizeof(uintptr_t)))
                throw HandlerError("binding_outside_image","The resolved binding exceeds the mapped image");
            binding={rva,{},false};
        } catch(const HandlerError& error) {binding={0,error.code()+": "+error.what(),false};}
    };
    const auto discover_function=[&](RuntimeBinding& binding,std::string_view label,
                                     std::string_view body_pattern,std::size_t body_offset) {
        try {
            const auto match=image.find_unique(label,body_pattern);
            if(match<body_offset)throw HandlerError("binding_outside_image","A function body precedes its entry");
            const auto entry=match-body_offset;
            DWORD64 unwind_image{};
            const auto unwind=RtlLookupFunctionEntry(candidate_base+match,&unwind_image,nullptr);
            if(!unwind || unwind_image!=candidate_base || unwind->BeginAddress!=entry
               || unwind->EndAddress<=match || !image.contains(entry,16))
                throw HandlerError("binding_unwind_mismatch","The matched body does not resolve to the expected PE unwind function entry");
            json existing=nullptr;
            const auto address=candidate_base+entry;
            const auto first=read<std::uint16_t>(address);
            std::uintptr_t destination{};
            if(first==0x25ff) {
                const auto displacement=read<std::int32_t>(address+2);
                const auto slot=static_cast<std::int64_t>(address)+6+displacement;
                if(slot<=0)throw HandlerError("binding_detour_invalid","The existing indirect jump slot is invalid");
                destination=read<std::uintptr_t>(static_cast<std::uintptr_t>(slot));
                existing={{"encoding","FF25_RIP"},{"slot",slot},{"destination",destination}};
            }else if((first&0xff)==0xe9) {
                const auto target=static_cast<std::int64_t>(address)+5+read<std::int32_t>(address+1);
                if(target<=0)throw HandlerError("binding_detour_invalid","The existing relative jump is invalid");
                destination=static_cast<std::uintptr_t>(target);
                existing={{"encoding","E9_REL32"},{"destination",destination}};
            }
            if(destination) {
                MEMORY_BASIC_INFORMATION region{};
                if(!VirtualQuery(reinterpret_cast<void*>(destination),&region,sizeof(region))
                   || region.State!=MEM_COMMIT || (region.Protect&(PAGE_GUARD|PAGE_NOACCESS))
                   || !(region.Protect&(PAGE_EXECUTE|PAGE_EXECUTE_READ|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY)))
                    throw HandlerError("binding_detour_invalid","The existing hook destination is not executable committed memory");
                HMODULE owner{};
                if(GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                    reinterpret_cast<LPCWSTR>(destination),&owner)) {
                    wchar_t path[32768]{};const auto length=GetModuleFileNameW(owner,path,32768);
                    existing["module"]=length?json(utf8(path,static_cast<int>(length))):json(nullptr);
                }
            }
            binding={entry,{},false};
            binding.discovery={{"method","unique_function_body_and_pe_unwind"},{"body_rva",match},
                {"body_offset",body_offset},{"function_end_rva",unwind->EndAddress},{"existing_entry_hook",existing}};
        }catch(const HandlerError& error){binding={0,error.code()+": "+error.what(),false};}
    };
    // Current CL131439 executable semantics and unique masked patterns were
    // inspected on 2026-11-10. Matches are candidates, not runtime acceptance.
    discover_function(bindings_.tick,"tick",
        "49 8D 6B 98 48 81 EC 40 01 00 00 41 0F 29 7B A8 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 45 00 41 0F 29 73 B8 0F 28 F9 0F 57 F6 44 88 44 24 30",0xb);
    discover_function(bindings_.process_event,"process_event",
        "8B 41 08 4D 8B F0 C1 E8 1E 48 8B FA F6 D0 4C 8B F9 A8 01 0F 84 ?? ?? ?? ?? 33 F6 F7 82 B0 00 00 00 00 04 00 00",0x30);
    discover(bindings_.names,"name_pool_reference",
        "80 3D ?? ?? ?? ?? 00 74 09 4C 8D 05 ?? ?? ?? ?? EB 16 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 4C 8B C0 C6 05 ?? ?? ?? ?? 01 8B CB 0F B7 C3 C1 E9 10", 9, 3, 7, 0);
    discover(bindings_.object_array,"registry_reference",
        "2B FB 85 FF 7E 14 41 3B EF 75 0F 49 8B D6 48 8D 0D ?? ?? ?? ?? E8 ?? ?? ?? ?? 48 8B 5C 24 50 48 8B 6C 24 58", 14, 3, 7, 16);
    discover(bindings_.free,"free",
        "48 85 C9 74 2E 53 48 83 EC 20 48 8B D9 48 8B 0D ?? ?? ?? ?? 48 85 C9 75 0C E8 ?? ?? ?? ?? 48 8B 0D ?? ?? ?? ?? 48 8B 01 48 8B D3 FF 50 48");
    discover(bindings_.realloc,"realloc",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 48 8B F1 41 8B D8 48 8B 0D ?? ?? ?? ?? 48 8B FA 48 85 C9 75 0C E8 ?? ?? ?? ?? 48 8B 0D ?? ?? ?? ?? 48 8B 01 44 8B CB 4C 8B C7 48 8B D6 48 8B 5C 24 30 48 8B 74 24 38 48 83 C4 20 5F 48 FF 60 38");
    discover(bindings_.string_construct,"wide_string_construct",
        "48 89 6C 24 10 48 89 74 24 18 48 89 7C 24 20 41 54 41 56 41 57 48 83 EC 30 45 33 E4 49 63 E8 4C 89 21");
    discover_function(bindings_.name_construct,"name_construct",
        "49 83 C1 02 0F B7 C0 44 0B D0 41 0F B7 01 66 85 C0 75 ED 41 F7 C2 80 FF FF FF 4C 89 5C 24 20",0x30);
    discover_function(bindings_.create_package,"create_package",
        "48 89 5C 24 10 48 89 74 24 18 48 89 7C 24 20 55 41 56 41 57 48 8D AC 24 E0 FB FF FF 48 81 EC 20 05 00 00",0);
    kTick=bindings_.tick.rva;
    kProcessEvent=bindings_.process_event.rva;
    kNames=bindings_.names.rva;
    kObjects=bindings_.object_array.rva;
    kFree=bindings_.free.rva;
    kObjectArrayRealloc=bindings_.realloc.rva;
    kStringConstruct=bindings_.string_construct.rva;
    kNameConstruct=bindings_.name_construct.rva;
    base_=candidate_base;
    return base_;
}
bool GameAccess::start() {
    std::lock_guard lock(bootstrap_mutex_);
    if(enabled_){if(!reflection_validated_.load())reflection_error_.clear();return true;}
    try {ensure_image_base();}
    catch(const std::exception& error) {startup_error_=error.what();return false;}
    if(!bindings_.executor_discovered()) {
        startup_error_="tick_binding_unavailable";return false;
    }
    const auto initialized=MH_Initialize();
    if(initialized!=MH_OK && initialized!=MH_ERROR_ALREADY_INITIALIZED) {
        startup_error_="hook_initialize_failed";return false;
    }
    active_=this;
    process_event_=bindings_.process_event.available()?reinterpret_cast<ProcessEventFn>(base_+kProcessEvent):nullptr;
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kTick),reinterpret_cast<void*>(&hooked_tick),
                     reinterpret_cast<void**>(&original_tick_))!=MH_OK) {
        active_=nullptr;startup_error_="tick_hook_create_failed";return false;
    }
    enabled_=true;
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kTick))!=MH_OK) {
        enabled_=false;MH_RemoveHook(reinterpret_cast<void*>(base_+kTick));
        original_tick_=nullptr;active_=nullptr;startup_error_="tick_hook_enable_failed";return false;
    }
    try {kInstalledTickHook=current_tick_hook(base_+kTick,reinterpret_cast<std::uintptr_t>(&hooked_tick),
        reinterpret_cast<std::uintptr_t>(original_tick_));}
    catch(const std::exception& error) {kInstalledTickHook={{"state","snapshot_unavailable"},{"error",error.what()}};}
    startup_error_.clear();diagnostic("tick_hook_enabled");return true;
}
void GameAccess::validate_runtime_layout() {
    reflection_validation_={{"state","checking"},{"method","game_tick_relationship_checks"},
        {"thread_id",GetCurrentThreadId()},{"tick",ticks_.load()},{"stage","binding_presence"}};
    if(!bindings_.reflection_discovered())
        throw HandlerError("reflection_bindings_unavailable","Name pool or object registry was not discovered");
    if(GetCurrentThreadId()!=game_thread_.load())
        throw HandlerError("reflection_wrong_thread","The checkpoint requires the observed Tick thread");
    reflection_validation_["stage"]="name_pool";
    if(name(0)!="None")
        throw HandlerError("name_layout_mismatch","The target FName index zero does not decode as None");
    const auto current_block=read<std::uint32_t>(base_+kNames+8);
    const auto current_cursor=read<std::uint32_t>(base_+kNames+0xc);
    if(current_block>=8192 || current_cursor>0x20000
       || !read<std::uintptr_t>(base_+kNames+0x10+current_block*sizeof(std::uintptr_t)))
        throw HandlerError("name_layout_mismatch","The name pool block and cursor violate the target layout");
    reflection_validation_["name_pool"]={{"none",name(0)},{"current_block",current_block},{"current_cursor",current_cursor}};
    reflection_validation_["stage"]="object_registry";
    const auto maximum=read<std::int32_t>(base_+kObjects+0x10),count=read<std::int32_t>(base_+kObjects+0x14);
    const auto max_chunks=read<std::int32_t>(base_+kObjects+0x18),chunks=read<std::int32_t>(base_+kObjects+0x1c);
    if(count<=0 || maximum<count || chunks<=0 || max_chunks<chunks
       || std::int64_t(count)>std::int64_t(chunks)*65536 || !read<std::uintptr_t>(base_+kObjects))
        throw HandlerError("registry_layout_mismatch","The object registry counts and chunk table are inconsistent");
    reflection_validation_["registry"]={{"count",count},{"maximum",maximum},{"chunks",chunks},
        {"maximum_chunks",max_chunks},{"item_stride",24},{"serial_offset",0x10}};
    const std::set<std::string> required={"/Script/CoreUObject.Object","/Script/CoreUObject.Class",
        "/Script/CoreUObject.Struct","/Script/CoreUObject.Function","/Script/Engine.Engine",
        "/Script/Engine.World","/Script/Engine.GameViewportClient"};
    std::map<std::string,std::uintptr_t> metadata;
    std::size_t live{},destroying{};
    for(int index=0;index<count;++index) {
        auto object=slot(index);if(!object)continue;
        const auto pointer=reinterpret_cast<std::uintptr_t>(object);
        if(read<std::int32_t>(pointer+0xc)!=index)
            throw HandlerError("registry_layout_mismatch","A live UObject index disagrees with its registry slot");
        if(read<std::uint32_t>(pointer+8)&kInvalidObjectFlags){++destroying;continue;}
        ++live;
        // Only known metadata is followed through its outer chain here. All
        // returned objects are validated again by the existing handle guards.
        const auto simple=object_name(object);
        const bool candidate=std::any_of(required.begin(),required.end(),[&](const auto& path){
            return path.ends_with("."+simple);});
        if(!candidate)continue;
        const auto path=object_path(object);
        if(required.contains(path)) {
            validate_object(object);
            if(!metadata.emplace(path,pointer).second)
                throw HandlerError("reflection_identity_mismatch","Required class path is not unique: "+path);
        }
    }
    reflection_validation_["registry"]["live_slots_inspected"]=live;
    reflection_validation_["registry"]["destroying_slots_skipped"]=destroying;
    for(const auto& path:required)if(!metadata.contains(path))
        throw HandlerError("reflection_metadata_missing","Required reflected class was not found: "+path);
    const auto metaclass=metadata.at("/Script/CoreUObject.Class");
    if(read<std::uintptr_t>(metaclass+0x10)!=metaclass)
        throw HandlerError("reflection_identity_mismatch","CoreUObject.Class is not its own metaclass");
    reflection_validation_["stage"]="class_property_function_relationships";
    json classes=json::array();std::size_t fields_checked{},functions_checked{},parameters_checked{};
    const auto check_fields=[&](std::uintptr_t owner,std::int32_t extent,bool function) {
        std::set<std::uintptr_t> visited;
        std::size_t parameters{},returns{};std::uint16_t return_offset=0xffff;
        for(auto field=read<std::uintptr_t>(owner+0x50);field;field=read<std::uintptr_t>(field+0x18)) {
            if(!visited.insert(field).second)
                throw HandlerError("reflection_field_cycle","The checkpoint found a reflected FField cycle");
            const auto type=field_type(field,[this](auto packed){return name(packed);});
            if(!type.ends_with("Property") || name(read<std::uint64_t>(field+0x20)).empty())
                throw HandlerError("reflection_field_mismatch","FField class and name do not describe an FProperty");
            const auto dim=read<std::int32_t>(field+0x30),size=read<std::int32_t>(field+0x34);
            const auto offset=read<std::int32_t>(field+0x44);const auto flags=read<std::uint64_t>(field+0x38);
            if(dim<1 || size<1 || offset<0)
                throw HandlerError("reflection_field_mismatch","An FProperty has invalid dimension, size or offset");
            if(!function || (flags&0x80)) {
                if(std::int64_t(offset)+std::int64_t(size)*dim>extent)
                    throw HandlerError("reflection_field_mismatch","An FProperty exceeds its reflected owner extent");
            }
            ++fields_checked;
            if(function && (flags&0x80)) {
                ++parameters;++parameters_checked;
                if(flags&0x400){++returns;return_offset=static_cast<std::uint16_t>(offset);}
            }
        }
        if(function && (parameters!=read<std::uint8_t>(owner+0xb4) || returns>1
           || read<std::uint16_t>(owner+0xb8)!=return_offset))
            throw HandlerError("reflection_function_mismatch","UFunction count/return header disagrees with CPF_Parm fields");
    };
    for(const auto& [path,klass]:metadata) {
        if(read<std::uintptr_t>(klass+0x10)!=metaclass)
            throw HandlerError("reflection_identity_mismatch","Required metadata is not a CoreUObject.Class: "+path);
        std::set<std::uintptr_t> supers;
        for(auto current=klass;current;current=read<std::uintptr_t>(current+0x40)) {
            if(!supers.insert(current).second)
                throw HandlerError("reflection_super_cycle","The checkpoint found a UStruct super cycle");
            validate_object(reinterpret_cast<void*>(current));
            if(read<std::uintptr_t>(current+0x10)!=metaclass)
                throw HandlerError("reflection_super_mismatch","A superclass is not a UClass");
        }
        const auto extent=read<std::int32_t>(klass+0x58);const auto alignment=read<std::int16_t>(klass+0x5c);
        if(extent<0 || alignment<=0 || (alignment&(alignment-1)))
            throw HandlerError("reflection_struct_mismatch","UStruct extent or alignment is inconsistent");
        check_fields(klass,extent,false);
        std::set<std::uintptr_t> children;
        for(auto child=read<std::uintptr_t>(klass+0x48);child;child=read<std::uintptr_t>(child+0x28)) {
            if(!children.insert(child).second)
                throw HandlerError("reflection_child_cycle","The checkpoint found a UField child cycle");
            const auto identity=validate_object(reinterpret_cast<void*>(child));
            if(identity.object_class!=reinterpret_cast<void*>(metadata.at("/Script/CoreUObject.Function")))continue;
            if(read<std::uintptr_t>(child+0x20)!=klass)
                throw HandlerError("reflection_function_mismatch","A UFunction child does not name its owning class");
            check_fields(child,read<std::uint16_t>(child+0xb6),true);++functions_checked;
        }
        classes.push_back({{"path",path},{"index",read<std::int32_t>(klass+0xc)},
            {"size",extent},{"alignment",alignment},{"super_count",supers.size()-1}});
    }
    if(!fields_checked || !functions_checked || !parameters_checked)
        throw HandlerError("reflection_coverage_missing","The checkpoint did not inspect properties and nonempty UFunctions");
    reflection_validation_.update({{"state","verified"},{"stage","complete"},{"classes",classes},
        {"fields_checked",fields_checked},{"functions_checked",functions_checked},{"parameters_checked",parameters_checked},
        {"limits",{"This checkpoint validates sampled engine metadata relationships, not every property layout",
                    "ProcessEvent, owned values and sparse storage require their own target exercises"}}});
    bindings_.names.runtime_validated=true;bindings_.object_array.runtime_validated=true;
    reflection_validated_=true;
}
void __fastcall GameAccess::hooked_tick(void* engine,float delta_seconds,bool idle_mode) {
    auto* self=active_;
    if(!self || !self->original_tick_)return;
    self->original_tick_(engine,delta_seconds,idle_mode);
    if(self->enabled_)self->pump();
}
json GameAccess::status() const {
#if defined(SN2_MCP_DEVELOPMENT_PROBES)
    constexpr bool development_probes=true;
    constexpr auto development_build_marker="SN2_MCP_DEVELOPMENT_PROBES=1";
#else
    constexpr bool development_probes=false;
    constexpr auto development_build_marker="SN2_MCP_DEVELOPMENT_PROBES=0";
#endif
    const auto observed=last_tick_ms_.load(),now=steady_ms();
    const auto age=observed && now>=observed?now-observed:0;
    const bool recent=observed && age<=kRequestWaitMs;
    json pending=nullptr,active=nullptr,world=nullptr;bool queue_metadata_available{};
    {
        std::unique_lock lock(queue_mutex_,std::try_to_lock);
        queue_metadata_available=lock.owns_lock();
        if(queue_metadata_available) {
            world=world_metadata_;pending=queue_.size();
            if(active_job_)active={{"request_id",active_job_->request.value("id",json(nullptr))},
                {"operation",active_job_->request.value("op",std::string{})},{"state","running"}};
        }
    }
    json bindings=nullptr,validation=nullptr,tick_hook=nullptr;std::string startup,reflection_error;
    {
        std::lock_guard lock(bootstrap_mutex_);
        bindings=bindings_.describe();validation=reflection_validation_;
        startup=startup_error_;reflection_error=reflection_error_;
        if(base_ && kTick && enabled_) {
            try {
                tick_hook=current_tick_hook(base_+kTick,reinterpret_cast<std::uintptr_t>(&hooked_tick),
                    reinterpret_cast<std::uintptr_t>(original_tick_));
                tick_hook["installed_snapshot"]=kInstalledTickHook;
                tick_hook["entry_changed_since_enable"]=kInstalledTickHook.contains("entry")
                    && tick_hook.at("entry").value("sample_stable",false)
                    && kInstalledTickHook.at("entry").value("sample_stable",false)
                    ?json(tick_hook.at("entry").at("bytes")!=kInstalledTickHook.at("entry").at("bytes"))
                    :json(nullptr);
                const auto stable_route=[](const json& value) {
                    return value.contains("entry") && value.at("entry").value("sample_stable",false)
                        && (value.at("relay").is_null() || value.at("relay").value("sample_stable",false));
                };
                tick_hook["resolved_destination_changed_since_enable"]=stable_route(tick_hook)
                    && stable_route(kInstalledTickHook)
                    ?json((tick_hook.at("resolved_destination").is_null()?json(nullptr):tick_hook.at("resolved_destination").at("address"))
                        !=(kInstalledTickHook.at("resolved_destination").is_null()?json(nullptr):kInstalledTickHook.at("resolved_destination").at("address")))
                    :json(nullptr);
            } catch(const std::exception& error) {tick_hook={{"state","snapshot_unavailable"},{"error",error.what()}};}
        }
    }
    json target={{"pid",GetCurrentProcessId()},{"executable",nullptr},{"creation_filetime",nullptr}};
    try {
        const auto image=sn2::loader::process_image(GetCurrentProcess());
        target["executable"]=utf8(image.data(),static_cast<int>(image.size()));
        target["creation_filetime"]=std::to_string(sn2::loader::creation_filetime(GetCurrentProcess()));
    } catch(const std::exception& error) {target["error"]=error.what();}
    const bool reflection=reflection_validated_.load();
    return {{"version","0.9.0"},{"native_route","standalone_in_process"},{"hook_installed",enabled_.load()},
            {"tick_hook_current",tick_hook},
            {"process_id",GetCurrentProcessId()},{"process_creation_filetime",target.at("creation_filetime")},{"target_process",target},
            {"game_thread_id",game_thread_.load()},{"tick_count",ticks_.load()},
            {"executor_ready",enabled_.load()&&recent},{"ready",enabled_.load()&&recent&&reflection},
            {"pump",{{"state",!observed?"not_observed":recent?"recent_tick":"tick_stale"},
                {"last_tick_monotonic_ms",observed?json(observed):json(nullptr)},
                {"tick_age_ms",observed?json(age):json(nullptr)},{"freshness_limit_ms",kRequestWaitMs},
                {"world_ready_confirmed",false}}},
            {"requests",{{"queue_depth",pending},{"queue_limit",nullptr},{"active",active},
                {"metadata_available",queue_metadata_available},{"completed",completed_jobs_.load()},
                {"cancelled_before_start",cancelled_jobs_.load()},{"default_wait_timeout_ms",kRequestWaitMs},
                {"cancelled_jobs_can_execute",false}}},
            {"bindings_source","runtime_image_patterns"},{"bindings",bindings},{"startup_error",startup},
            {"abi_state",reflection?"reflection_checkpoint_verified":"unverified"},
            {"development_probes",development_probes},
            {"development_build_marker",development_build_marker},
            {"accepted_types",evidence_.describe()},
            {"reflection",{{"state",reflection?"verified":"unverified"},{"validation",validation},
                {"error",reflection_error.empty()?json(nullptr):json(reflection_error)}}},
            {"capabilities",{{"reflection_reads",reflection?"verified":"unverified"},
                {"typed_invocation","unverified"},{"owned_values","unverified"},
                {"sparse_containers","unverified"},{"observations",reflection?"available":"unverified"}}},
            {"operations",{"status","enable_game","objects","properties","functions","invoke","load_asset","load_class",
                "runtime_create_package","runtime_compile_blueprint","runtime_save_package","runtime_release_blueprint",
                "blueprint_mod_enable","blueprint_mod_status","blueprint_mod_disable","read_array",
                "read_set","read_map","read_struct","snapshot","watch_start","watch_read","watch_stop",
                "watch_forget","watch_list","watch_resume"}},
            {"snapshot_consistency","single_tick_sequential"},{"world_generation_supported",reflection},
            {"world",world},{"reflection_generation",nullptr},{"script_reload_generation_supported",false},
            {"string_inputs",{{"quarantined",string_inputs_quarantined_.load()},
                {"retained_allocations",string_inputs_retained_count_.load()},
                {"retained_bytes",string_inputs_retained_bytes_.load()}}},
            {"object_array_inputs",{{"quarantined",object_array_inputs_quarantined_.load()},
                {"retained_allocations",object_array_inputs_retained_count_.load()},
                {"retained_bytes",object_array_inputs_retained_bytes_.load()}}},
            {"serial_guard",{{"policy","strict_observed_equality_including_zero"},
                {"coverage","conditional_on_observed_nonzero_object_and_class_serial"},{"zero_aba_protection",false}}},
            {"limitations",{"Image-pattern matches are static candidates; game-thread metadata validates reflection",
                "Typed invocation and owned/sparse layouts still require target acceptance",
                "Zero serial values do not protect against ABA",
                "One active native session; handles and observations expire on disconnect",
                "DLL residency ends with the process; hot unload is not implemented"}}};
}
void GameAccess::require_runtime_operation(const std::string& operation) const {
    if((operation=="invoke" || operation=="load_asset" || operation=="load_class"
        || operation=="blueprint_mod_enable" || operation=="blueprint_mod_status"
        || operation=="blueprint_mod_disable") && !bindings_.process_event.available())
        throw HandlerError("process_event_unavailable","The current executable ProcessEvent binding is unavailable",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false}});
    if(!reflection_validated_.load())
        throw HandlerError("reflection_unverified","Subnautica game-thread reflection checkpoint has not passed",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false}});
}
json GameAccess::request(const json& envelope) {
    const auto operation=envelope.at("op").get<std::string>();
    const auto& params=envelope.at("params");
    validate_request_identity(params,GetCurrentProcessId(),
        sn2::loader::creation_filetime(GetCurrentProcess()),envelope.at("session_id"));
    if(operation=="status")return status();
    if(operation=="enable_game"){start();return status();}
    require_runtime_operation(operation);
    if(!enabled_) throw HandlerError("native_not_ready","Game hooks are not installed: "+startup_error_);
    const auto wait=request_wait(envelope);
    auto job=std::make_shared<Job>();job->request=envelope;auto future=job->result.get_future();
    {std::lock_guard lock(queue_mutex_);
     job->world_generation=world_generation_.load();queue_.push_back(job);}
    if(future.wait_for(wait)!=std::future_status::ready) {
        if(job->state.cancel()) {
            ++cancelled_jobs_;
            {std::unique_lock lock(queue_mutex_,std::try_to_lock);if(lock.owns_lock()){auto entry=std::find(queue_.begin(),queue_.end(),job);if(entry!=queue_.end())queue_.erase(entry);}}
            throw HandlerError("game_not_pumping","The request was cancelled before execution because the game Tick did not service the queue",
                {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},
                 {"request_started",false},{"stage","queued_cancelled"},{"native_status",status()}});
        }
        if(future.wait_for(std::chrono::milliseconds(0))==std::future_status::ready)return future.get();
        if(job->state.get()==JobState::cancelled)throw HandlerError("session_closed","The request was cancelled before execution after its session closed",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},{"request_started",false},{"stage","queued_cancelled"}});
        throw HandlerError("game_timeout","The wait timed out; the accepted call may have executed",
            {{"executed",false},{"may_have_executed",true},{"do_not_retry",true},
             {"request_started",true},{"stage","running_outcome_unknown"},{"native_status",status()}});
    }
    return future.get();
}
void GameAccess::disconnect(const std::string& session) {
    // Network thread: only mark cleanup and cancel jobs that have not entered Tick.
    std::lock_guard lock(queue_mutex_);
    session_cleanup_pending_=true;
    for (const auto& job : queue_) {
        if (job->request.value("session_id",std::string{})!=session) continue;
        if (job->state.cancel()) {
            ++cancelled_jobs_;
            job->result.set_exception(std::make_exception_ptr(
                HandlerError("session_closed","The session disconnected before the request executed")));
        }
    }
}
namespace {
json public_shape(const json& metadata,bool map_member_metadata=false) {
    json result=json::object();
    for(const auto* key:{"type","element_size","array_dim","direction","passing","struct_path","struct_name",
                        "properties_size","min_alignment","scalar_type","container_type","element_type",
                        "inner_type","inner_element_size","inner_array_dim","inner_offset"})
        if(metadata.contains(key))result[key]=metadata.at(key);
    for(const auto* key:{"struct","array","set","element","element_struct","bool_layout","declared_type"})
        if(metadata.contains(key) && metadata.at(key).is_object())result[key]=public_shape(metadata.at(key));
    if(map_member_metadata)for(const auto* key:{"key","value"})
        if(metadata.contains(key) && metadata.at(key).is_object())result[key]=public_shape(metadata.at(key));
    for(const auto* key:{"field_size","byte_offset","byte_mask","field_mask","underlying_type"})
        if(metadata.contains(key))result[key]=metadata.at(key);
    for(const auto* key:{"declared_type","meta_class","property_class"})
        if(metadata.contains(key) && metadata.at(key).is_object()) {
            const auto& declared=metadata.at(key);
            result[key]={{"name",declared.value("name",std::string{})},{"path",declared.value("path",std::string{})}};
        }
    if(metadata.contains("enum") && metadata.at("enum").is_object()) {
        const auto& enumeration=metadata.at("enum");
        result["enum"]={{"path",enumeration.value("path",json(nullptr))},
            {"underlying_type",enumeration.value("underlying_type",json(nullptr))}};
    }
    for(const auto* key:{"components","members"})
        if(metadata.contains(key) && metadata.at(key).is_array()) {
            result[key]=json::array();
            for(const auto& component:metadata.at(key)) {
                auto child=public_shape(component);
                if(component.contains("name"))child["name"]=component.at("name");
                if(component.contains("offset"))child["offset"]=component.at("offset");
                result[key].push_back(std::move(child));
            }
        }
    return result;
}
bool owned_shape(const json& shape) {
    const auto type=shape.value("type",std::string{});
    if(type=="StrProperty" || type=="ArrayProperty" || type=="SetProperty")return true;
    if(shape.is_structured())for(const auto& child:shape)if(child.is_object() && owned_shape(child))return true;
    return false;
}
}
void GameAccess::observe_result(const std::string& operation,const json& result) {
    if(result.value("source",std::string{})=="development_fixture")return;
    // Partial values and empty-container metadata do not establish decoded
    // element coverage. Individual supported property reads can be recorded.
    if(operation=="invoke")return; // recorded with its validated signature below
    if(operation=="properties") {
        for(const auto& item:result.value("items",json::array())) {
            if(!item.value("supported",false) || item.value("partial",false) || !item.contains("value"))continue;
            auto shape=public_shape(item);shape["direction"]="property_read";
            evidence_.observe(operation,shape,ticks_.load(),false,true);
        }
    }else if(operation=="read_array" || operation=="read_set" || operation=="read_map") {
        auto shape=public_shape(result,operation=="read_map");shape["direction"]="container_read";
        const bool populated=result.contains("items") && !result.at("items").empty();
        evidence_.observe(operation,shape,ticks_.load(),false,true,result.value("partial",false),populated);
    }else if(operation=="read_struct") {
        auto shape=public_shape(result);shape["direction"]="struct_read";
        evidence_.observe(operation,shape,ticks_.load(),false,true,result.value("partial",false));
    }
}
void GameAccess::pump() {
    if(!game_thread_.load()) game_thread_=GetCurrentThreadId();
    if(game_thread_.load()!=GetCurrentThreadId()) return;
    ++ticks_;
    last_tick_ms_=steady_ms();
    {
        std::lock_guard lock(bootstrap_mutex_);
        bindings_.tick.runtime_validated=true;
        if(!reflection_validated_.load() && reflection_error_.empty()) {
            try {validate_runtime_layout();}
            catch(const std::exception& error) {reflection_error_=error.what();}
        }
    }
    if(!reflection_validated_.load())return;
    try {refresh_world();}
    catch(const HandlerError& error) {
        change_world({});
        std::lock_guard lock(queue_mutex_);
        world_metadata_["error"]={{"code",error.code()},{"message",error.what()}};
    }
    pump_mods();
    std::shared_ptr<Job> job;
    {
        std::lock_guard lock(queue_mutex_);
        if(session_cleanup_pending_) {
            observations_.disconnect(handle_session_);
            handles_.clear();signature_contracts_.clear();handle_session_.clear();session_cleanup_pending_=false;
        }
        if(!queue_.empty()){job=queue_.front();queue_.pop_front();}
    }
    if(job) {
        if(job->state.begin()) {
            {std::lock_guard lock(queue_mutex_);active_job_=job;}
            try {
                auto result=execute(job->request);
                try{observe_result(job->request.at("op").get<std::string>(),result);}catch(...) {}
                job->result.set_value(std::move(result));
            }catch(...) {job->result.set_exception(std::current_exception());}
            job->state.finish();++completed_jobs_;
            {std::lock_guard lock(queue_mutex_);active_job_.reset();}
        }
    }
    try {refresh_world();}
    catch(const HandlerError& error) {
        change_world({});
        std::lock_guard lock(queue_mutex_);
        world_metadata_["error"]={{"code",error.code()},{"message",error.what()}};
    }
    const auto now=static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    // Disconnect notification waits for an active read; once marked,
    // no new sample starts. The same Tick performs the actual cleanup.
    {
        std::lock_guard lock(queue_mutex_);
        if(session_cleanup_pending_) {
            observations_.disconnect(handle_session_);
            handles_.clear();signature_contracts_.clear();handle_session_.clear();session_cleanup_pending_=false;
        } else {
            observations_.pump(now,ticks_.load(),[this](const json& targets){return snapshot(targets);});
        }
    }
}

std::string GameAccess::name(uint64_t fname) const {
    uint32_t index=static_cast<uint32_t>(fname),number=static_cast<uint32_t>(fname>>32);
    uint32_t block=index>>16;
    auto current=read<uint32_t>(base_+kNames+8);
    if(block>current || block>=8192) throw HandlerError("invalid_name","Invalid name");
    auto data=read<uintptr_t>(base_+kNames+0x10+block*8ull)+(index&65535)*2ull;
    auto header=read<uint16_t>(data);int length=header>>6;
    if(length<1 || length>1023 || (index&65535)*2ull+2+length*((header&1)?2:1)>0x20000) throw HandlerError("invalid_name","Invalid name entry");
    std::string result;
    if(header&1) {std::vector<wchar_t> text(length);if(!copy_memory(text.data(),reinterpret_cast<void*>(data+2),length*2ull))throw HandlerError("invalid_memory","Name memory is unavailable");result=utf8(text.data(),length);}
    else {std::string bytes(length,'\0');if(!copy_memory(bytes.data(),reinterpret_cast<void*>(data+2),length))throw HandlerError("invalid_memory","Name memory is unavailable");
        std::wstring text; for(unsigned char byte:bytes)text.push_back(static_cast<wchar_t>(byte));result=utf8(text.data(),static_cast<int>(text.size()));}
    if(number) result+="_"+std::to_string(number-1);return result;
}
std::string GameAccess::object_name(void* object) const {return name(read<uint64_t>(reinterpret_cast<uintptr_t>(object)+0x18));}
std::string GameAccess::object_path(void* object) const {
    std::string result;auto ptr=reinterpret_cast<uintptr_t>(object);
    std::set<uintptr_t> visited;
    for(;ptr;ptr=read<uintptr_t>(ptr+0x20)) {
        if(!visited.insert(ptr).second)throw HandlerError("invalid_object_path","Object outer chain contains a cycle");
        auto part=object_name(reinterpret_cast<void*>(ptr));result=result.empty()?part:part+"."+result;
    }
    return result;
}
GameAccess::SlotIdentity GameAccess::slot_identity(int index) const {
    auto count=read<int32_t>(base_+kObjects+0x14);auto chunks=read<uintptr_t>(base_+kObjects);
    if(count<0 || index<0 || index>=count)return {};
    auto chunk_count=read<int32_t>(base_+kObjects+0x1c);
    if((index>>16)>=chunk_count || chunk_count<1 || !chunks)return {};
    const auto chunk_offset=static_cast<uintptr_t>(index>>16)*sizeof(uintptr_t);
    if(chunks>std::numeric_limits<uintptr_t>::max()-chunk_offset-sizeof(uintptr_t))return {};
    auto chunk=read<uintptr_t>(chunks+chunk_offset);
    if(!chunk)return {};
    const auto item_offset=static_cast<uintptr_t>(index&65535)*24;
    if(chunk>std::numeric_limits<uintptr_t>::max()-item_offset-24)return {};
    const auto item=chunk+item_offset;
    const auto object=read<uintptr_t>(item);
    if(!object)return {};
    const auto serial=read<int32_t>(item+0x10);
    if(!valid_observed_serial(serial))throw HandlerError("unsupported_serial_value","The observed slot serial value is unsupported");
    if(read<uintptr_t>(item)!=object || read<int32_t>(item+0x10)!=serial)
        throw HandlerError("stale_handle","Slot identity changed during the read; obtain a new handle");
    return {reinterpret_cast<void*>(object),serial};
}
void* GameAccess::slot(int index) const {
    return slot_identity(index).object;
}
ObjectSerialIdentity GameAccess::serial_identity(const Handle& value) {
    return {reinterpret_cast<uintptr_t>(value.object),reinterpret_cast<uintptr_t>(value.object_class),
            value.index,value.name,value.serial,value.class_index,value.class_name,value.class_serial};
}
bool GameAccess::class_is(void* object,const std::string& class_path) const {
    std::set<uintptr_t> seen;
    for(auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!seen.insert(klass).second)throw HandlerError("invalid_object_class","The class hierarchy contains a cycle");
        if(object_path(reinterpret_cast<void*>(klass))==class_path)return true;
    }
    return false;
}
uintptr_t GameAccess::object_field(void* object,const std::string& property_name) const {
    std::set<uintptr_t> classes,fields;
    for(auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!classes.insert(klass).second)throw HandlerError("property_scan_cycle","The class hierarchy contains a cycle");
        for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
            if(!fields.insert(field).second)throw HandlerError("property_scan_cycle","The reflected property chain contains a cycle");
            if(name(read<uint64_t>(field+0x20))!=property_name)continue;
            const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34),class_size=read<int32_t>(klass+0x58);
            if(field_type(field,[this](auto value){return name(value);})!="ObjectProperty"
               || size!=sizeof(uintptr_t) || read<int32_t>(field+0x30)!=1 || offset<0 || offset>class_size-size)
                throw HandlerError("unsupported_world_layout","The world source requires a reflected object pointer property");
            return field;
        }
    }
    throw HandlerError("world_source_unavailable","The reflected world source property was not found: "+property_name);
}
void* GameAccess::object_world(void* object) {
    if(read<uint32_t>(reinterpret_cast<uintptr_t>(object)+8)&0x10)return nullptr;
    std::set<uintptr_t> seen;
    for(auto outer=reinterpret_cast<uintptr_t>(object);outer;outer=read<uintptr_t>(outer+0x20)) {
        if(!seen.insert(outer).second)throw HandlerError("invalid_object_outer","The outer chain contains a cycle");
        validate_object(reinterpret_cast<void*>(outer));
        if(class_is(reinterpret_cast<void*>(outer),"/Script/Engine.World"))return reinterpret_cast<void*>(outer);
    }
    return nullptr;
}
void GameAccess::change_world(const Handle& world) {
    const bool changed=world.object!=current_world_.object
        || (world.object && !same_serial_identity(serial_identity(world),serial_identity(current_world_)));
    std::lock_guard lock(queue_mutex_);
    if(!changed){world_metadata_.erase("error");return;}
    if(changed) {
        const auto previous=world_generation_.load(),generation=previous+1;
        std::vector<std::string> affected;
        for(const auto& [id,handle]:handles_)
            if(handle.bound_world && handle.world_generation==previous)affected.push_back(id);
        current_world_=world;world_generation_=generation;
        observations_.invalidate_world(affected,previous,generation,steady_ms(),ticks_.load());
        for(auto entry=queue_.begin();entry!=queue_.end();) {
            const auto& job=*entry;
            const auto& params=job->request.at("params");
            const auto affected_reference=[&](const auto& self,const json& value)->bool {
                if(value.is_object() && value.contains("handle") && value.at("handle").is_string()
                   && std::find(affected.begin(),affected.end(),value.at("handle").get<std::string>())!=affected.end())return true;
                if(value.is_structured())for(const auto& child:value)if(self(self,child))return true;
                return false;
            };
            const bool bound=affected_reference(affected_reference,params);
            if(bound && job->world_generation && job->world_generation!=generation && job->state.cancel()) {
                ++cancelled_jobs_;
                job->result.set_exception(std::make_exception_ptr(HandlerError("world_changed","The selected world changed before the request executed",
                    {{"executed",false},{"may_have_executed",false},{"request_started",false},{"stage","queued_cancelled"},
                     {"previous_world_generation",job->world_generation},{"world_generation",generation}})));
                entry=queue_.erase(entry);
            } else ++entry;
        }
    }
    world_metadata_={{"source","GameEngine.GameViewport.World"},{"state",world.object?"observed":"unavailable"},
        {"generation",world_generation_.load()},{"current",world.object?json{{"index",world.index},{"serial",world.serial},{"path",object_path(world.object)}}:json(nullptr)}};
}
void GameAccess::refresh_world() {
    if(game_engine_.object) {
        try {
            if(!same_serial_identity(serial_identity(game_engine_),serial_identity(validate_object(game_engine_.object))))game_engine_={};
        } catch(const HandlerError&) {game_engine_={};}
    }
    if(!game_engine_.object) {
        engine_viewport_field_=0;viewport_world_field_=0;game_viewport_={};
        const auto count=read<int32_t>(base_+kObjects+0x14);
        for(int index=0;index<count;++index) {
            auto object=slot(index);if(!object)continue;
            try {
                if((read<uint32_t>(reinterpret_cast<uintptr_t>(object)+8)&0x10)
                   || object_name(reinterpret_cast<void*>(read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10)))!="GameEngine"
                   || !class_is(object,"/Script/Engine.GameEngine"))continue;
                game_engine_=validate_object(object);break;
            } catch(const HandlerError&) {}
        }
        if(!game_engine_.object){change_world({});return;}
    }
    if(!engine_viewport_field_)engine_viewport_field_=object_field(game_engine_.object,"GameViewport");
    const auto viewport_pointer=read<uintptr_t>(reinterpret_cast<uintptr_t>(game_engine_.object)+read<int32_t>(engine_viewport_field_+0x44));
    if(!viewport_pointer){game_viewport_={};viewport_world_field_=0;change_world({});return;}
    const auto viewport=validate_object(reinterpret_cast<void*>(viewport_pointer));
    if(!game_viewport_.object || !same_serial_identity(serial_identity(viewport),serial_identity(game_viewport_))) {
        if(!class_is(viewport.object,"/Script/Engine.GameViewportClient"))throw HandlerError("unsupported_world_layout","GameViewport is not a GameViewportClient");
        viewport_world_field_=object_field(viewport.object,"World");game_viewport_=viewport;
    }
    const auto world_pointer=read<uintptr_t>(viewport_pointer+read<int32_t>(viewport_world_field_+0x44));
    if(!world_pointer){change_world({});return;}
    auto world=validate_object(reinterpret_cast<void*>(world_pointer));
    if(!class_is(world.object,"/Script/Engine.World"))throw HandlerError("unsupported_world_layout","GameViewport.World is not a World");
    change_world(world);
}
std::string GameAccess::handle_for(void* object,int index) {
    auto identity=validate_object(object);
    if(identity.index!=index)throw HandlerError("stale_handle","The object index changed");
    identity.bound_world=object_world(object);
    identity.world_generation=identity.bound_world?world_generation_.load():0;
    for(const auto& [id,existing]:handles_) {
        if(existing.world_generation==identity.world_generation && existing.bound_world==identity.bound_world
           && same_serial_identity(serial_identity(existing),serial_identity(identity)))return id;
    }
    auto id="obj-"+std::to_string(++next_handle_);
    handles_[id]=identity;return id;
}
GameAccess::Handle GameAccess::validate_object(void* object) const {
    if(!object)throw HandlerError("invalid_object","The object pointer is missing");
    const auto ptr=reinterpret_cast<uintptr_t>(object);
    const auto index=read<int32_t>(ptr+0xc);
    const auto item=slot_identity(index);
    if(index<0 || item.object!=object)throw HandlerError("stale_handle","The object does not match its registry slot");
    const auto object_flags=read<uint32_t>(ptr+8);
    if(object_flags&kInvalidObjectFlags)throw HandlerError("object_destroying","The object is garbage or being destroyed",
        {{"object_flags",object_flags},{"invalid_object_flags",kInvalidObjectFlags}});
    const auto object_class=read<uintptr_t>(ptr+0x10);
    if(!object_class)throw HandlerError("invalid_object_class","The object has no class");
    const auto class_index=read<int32_t>(object_class+0xc);
    const auto class_item=slot_identity(class_index);
    if(class_index<0 || class_item.object!=reinterpret_cast<void*>(object_class)
       || (read<uint32_t>(object_class+8)&kInvalidObjectFlags))
        throw HandlerError("invalid_object_class","The object class does not match a live registry slot");
    const auto object_fname=read<uint64_t>(ptr+0x18);
    const auto class_fname=read<uint64_t>(object_class+0x18);
    name(object_fname);name(class_fname);
    const auto item_after=slot_identity(index),class_after=slot_identity(class_index);
    if(item_after.object!=object || item_after.serial!=item.serial
       || class_after.object!=reinterpret_cast<void*>(object_class) || class_after.serial!=class_item.serial
       || read<int32_t>(ptr+0xc)!=index || read<uintptr_t>(ptr+0x10)!=object_class
       || read<uint64_t>(ptr+0x18)!=object_fname || read<int32_t>(object_class+0xc)!=class_index
       || read<uint64_t>(object_class+0x18)!=class_fname)
        throw HandlerError("stale_handle","Object or class identity changed during the read; obtain a new handle");
    const auto final_flags=read<uint32_t>(ptr+8)|read<uint32_t>(object_class+8);
    if(final_flags&kInvalidObjectFlags)
        throw HandlerError("object_destroying","The object or its class is garbage or being destroyed",
            {{"object_flags",final_flags},{"invalid_object_flags",kInvalidObjectFlags}});
    return {object,reinterpret_cast<void*>(object_class),index,object_fname,item.serial,class_index,class_fname,class_item.serial};
}
void GameAccess::validate_class(void* object_class) const {
    // Validate the actual target metaclass and hierarchy before treating it as UClass.
    const auto identity=validate_object(object_class);
    auto metaclass=reinterpret_cast<uintptr_t>(identity.object_class);
    std::set<uintptr_t> visited;
    for(;metaclass;) {
        if(!visited.insert(metaclass).second)
            throw HandlerError("invalid_object_class","The metaclass chain contains a cycle");
        validate_object(reinterpret_cast<void*>(metaclass));
        if(object_path(reinterpret_cast<void*>(metaclass))=="/Script/CoreUObject.Class")return;
        metaclass=read<uintptr_t>(metaclass+0x40);
    }
    throw HandlerError("invalid_object_class","The PropertyClass object is not a confirmed UClass");
}
void GameAccess::validate_development_service_call(void* object,void* function_owner,
        const std::string& function_name,uintptr_t function,const unsigned char* parameters) const {
    if(object_path(function_owner)!="/SN2MCPDevelopmentServices/BP_DevelopmentServices.BP_DevelopmentServices_C")return;
    if(function_name!="CreateHeldObject" && function_name!="ReadWidgetText"
       && function_name!="WriteWidgetText" && function_name!="SetObjectTextProperty")return;
    const auto parameter_bytes=read<uint16_t>(function+0xb6);
    auto argument=[&](const std::string& key,const std::string& expected_type) {
        std::set<uintptr_t> fields;
        for(auto field=read<uintptr_t>(function+0x50);field;field=read<uintptr_t>(field+0x18)) {
            if(!fields.insert(field).second)
                throw HandlerError("service_signature_changed","The service parameter chain contains a cycle");
            if(!(read<uint64_t>(field+0x38)&0x80) || name(read<uint64_t>(field+0x20))!=key)continue;
            const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34);
            const auto flags=read<uint64_t>(field+0x38);
            const bool const_name_reference=expected_type=="NameProperty" && (flags&kConstParm)
                && (flags&(0x100|kReferenceParm)) && !(flags&0x400);
            if(field_type(field,[this](auto value){return name(value);})!=expected_type || size!=8
               || read<int32_t>(field+0x30)!=1 || offset<0 || offset>parameter_bytes-8
               || ((flags&(0x100|0x400|kReferenceParm)) && !const_name_reference))
                throw HandlerError("service_signature_changed","The service argument no longer has its expected value layout");
            return read<uintptr_t>(reinterpret_cast<uintptr_t>(parameters)+static_cast<uintptr_t>(offset));
        }
        throw HandlerError("service_signature_changed","The expected service argument was not found: "+key);
    };
    if(function_name=="ReadWidgetText" || function_name=="WriteWidgetText") {
        auto widget=reinterpret_cast<void*>(argument("Widget","ObjectProperty"));
        if(!widget)throw HandlerError("service_object_required","A live TextBlock is required for the text operation");
        validate_object(widget);
        if(!class_is(widget,"/Script/UMG.TextBlock"))
            throw HandlerError("incompatible_object_argument","The text operation requires a TextBlock");
        return;
    }
    if(function_name=="SetObjectTextProperty") {
        auto target=reinterpret_cast<void*>(argument("Target","ObjectProperty"));
        if(!target)throw HandlerError("service_object_required","A live object is required for the text property operation");
        validate_object(target);
        const auto requested=argument("PropertyName","NameProperty");
        std::set<uintptr_t> classes,fields;
        for(auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(target)+0x10);klass;klass=read<uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)
                throw HandlerError("property_scan_cycle","The text property owner chain contains a cycle");
            validate_class(reinterpret_cast<void*>(klass));
            for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)
                    throw HandlerError("property_scan_cycle","The text property field chain contains a cycle");
                if(read<uint64_t>(field+0x20)!=requested)continue;
                const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34),extent=read<int32_t>(klass+0x58);
                if(field_type(field,[this](auto value){return name(value);})!="TextProperty")
                    throw HandlerError("service_text_property_required","The selected property is not an FText property");
                if(offset<0 || size<=0 || size>extent || offset>extent-size || read<int32_t>(field+0x30)!=1)
                    throw HandlerError("unsupported_property_layout","The selected FText property has an invalid extent or array dimension");
                validate_object(target);return;
            }
        }
        throw HandlerError("property_not_found","The selected FText property was not found");
    }
    auto represented=reinterpret_cast<void*>(argument("ObjectClass","ClassProperty"));
    if(!represented)throw HandlerError("service_class_required","A concrete UObject class is required for prototype creation");
    validate_class(represented);
    // Exact Shipping code corroborates UClass.ClassFlags +0xd4 and ClassWithin
    // +0xe0: SpawnObject 0x3fecf80 -> StaticConstructObject_Internal 0x16e7af0;
    // CreateDefaultSubobject 0x16d86b0 tests CLASS_Abstract (ObjectMacros.h).
    // These are operation-specific ABI checks, never a build-version gate.
    auto opcode=[&](uintptr_t rva,std::initializer_list<unsigned char> expected) {
        std::vector<unsigned char> actual(expected.size());
        if(!game_memory::copy(actual.data(),reinterpret_cast<const void*>(base_+rva),actual.size())
           || !std::equal(actual.begin(),actual.end(),expected.begin()))
            throw HandlerError("construction_layout_unconfirmed","The current game code does not confirm the UObject construction layout",
                {{"checkpoint_rva",rva},{"executed",false},{"may_have_executed",false}});
    };
    opcode(0x16e7b41,{0x41,0xf7,0x87,0xd4,0,0,0,0x80,0,0,0x10});
    opcode(0x16d87d3,{0xf6,0x87,0xd4,0,0,0,0x01});
    opcode(0x3fed03b,{0x48,0x39,0xb8,0xe0,0,0,0});
    opcode(0x3fed083,{0x48,0x8b,0x9b,0xe0,0,0,0});
    const auto class_pointer=reinterpret_cast<uintptr_t>(represented);
    const auto class_identity=serial_identity(validate_object(represented));
    const auto flags=read<uint32_t>(class_pointer+0xd4);
    const auto within=read<uintptr_t>(class_pointer+0xe0);
    if(flags&0x1)throw HandlerError("abstract_class","An abstract class cannot be constructed as a UObject prototype");
    if(flags&0x02000000)throw HandlerError("deprecated_class","A deprecated class cannot be constructed as a UObject prototype");
    if(flags&0x80000000)throw HandlerError("class_replaced","The selected class has been superseded by a newer class");
    std::set<uintptr_t> classes;
    for(auto klass=class_pointer;klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!classes.insert(klass).second)throw HandlerError("invalid_object_class","The represented class hierarchy contains a cycle");
        validate_class(reinterpret_cast<void*>(klass));
        const auto path=object_path(reinterpret_cast<void*>(klass));
        if(path=="/Script/Engine.Actor" || path=="/Script/Engine.ActorComponent")
            throw HandlerError("prototype_lifecycle_required","Actors and ActorComponents require their own spawn or component lifecycle");
    }
    if(within) {
        validate_class(reinterpret_cast<void*>(within));
        if(!is_compatible_object(object,reinterpret_cast<void*>(within)))
            throw HandlerError("incompatible_object_outer","The service Actor does not satisfy the class's required outer type");
    }
    if(flags!=read<uint32_t>(class_pointer+0xd4) || within!=read<uintptr_t>(class_pointer+0xe0)
       || !same_serial_identity(class_identity,serial_identity(validate_object(represented))))
        throw HandlerError("class_changed","The selected construction class changed during validation");
}
json GameAccess::function_execution_metadata(uintptr_t function) const {
    json result={{"state","checking"},{"function_address",function},
        {"method","exact_code_checkpoints_and_live_function_memory"},
        {"limits",{"This is a synchronous memory sample, not proof that bytecode executed"}}};
    try {
        const auto identity=serial_identity(validate_object(reinterpret_cast<void*>(function)));
        // Class.h UStruct::Script and Stack.h FFrame::Code; exact Shipping
        // ProcessEvent tests Script.Num +0x68, FFrame reads Script.Data +0x60.
        const auto opcode=[&](uintptr_t rva,std::initializer_list<unsigned char> expected) {
            std::vector<unsigned char> bytes(expected.size());
            return game_memory::copy(bytes.data(),reinterpret_cast<const void*>(base_+rva),bytes.size())
                && std::equal(bytes.begin(),bytes.end(),expected.begin());
        };
        if(!opcode(0x16b91a9,{0x39,0x72,0x68})
           || !opcode(0x16afaa5,{0x48,0x8b,0x47,0x60})
           || !opcode(0x16b91b2,{0x48,0x8b,0x8f,0xc8,0,0,0})
           || !opcode(0x157e9a3,{0xff,0x95,0xd8,0,0,0})) {
            result["state"]="layout_unconfirmed";return result;
        }
        const auto data=read<uintptr_t>(function+0x60);
        const auto count=read<int32_t>(function+0x68),capacity=read<int32_t>(function+0x6c);
        const auto implementation=read<uintptr_t>(function+0xd8);
        const auto event_graph=read<uintptr_t>(function+0xc8);
        const auto event_offset=read<int32_t>(function+0xd0);
        result["layout"]={{"script_data_offset",0x60},{"script_count_offset",0x68},
            {"script_capacity_offset",0x6c},{"function_pointer_offset",0xd8}};
        result["script"]={{"data",data},{"byte_count",count},{"capacity",capacity}};
        result["implementation"]=sample_hook_jump(implementation);
        result["process_event_entry"]=sample_hook_jump(reinterpret_cast<uintptr_t>(process_event_));
        result["event_graph_function"]=event_graph;
        result["event_graph_call_offset"]=event_offset;
        if(count<0 || capacity<count || (count && !data)) {
            result["state"]="invalid_script_header";return result;
        }
        std::vector<unsigned char> bytes(static_cast<size_t>(count));
        if(count && !game_memory::copy(bytes.data(),reinterpret_cast<const void*>(data),bytes.size())) {
            result["state"]="script_unreadable";return result;
        }
        if(read<uintptr_t>(function+0x60)!=data || read<int32_t>(function+0x68)!=count
           || read<int32_t>(function+0x6c)!=capacity || read<uintptr_t>(function+0xd8)!=implementation
           || read<uintptr_t>(function+0xc8)!=event_graph || read<int32_t>(function+0xd0)!=event_offset
           || !same_serial_identity(identity,serial_identity(validate_object(reinterpret_cast<void*>(function))))) {
            result["state"]="function_changed";return result;
        }
        constexpr char digits[]="0123456789abcdef";
        std::string encoded;encoded.reserve(bytes.size()*2);
        for(const auto value:bytes){encoded.push_back(digits[value>>4]);encoded.push_back(digits[value&15]);}
        result["script"]["bytecode_hex"]=std::move(encoded);
        result["state"]=count?"sampled":"empty_script";
    } catch(const HandlerError& error) {
        result["state"]="unavailable";result["error"]={{"code",error.code()},{"message",error.what()}};
    } catch(const std::exception& error) {
        result["state"]="unavailable";result["error"]={{"message",error.what()}};
    }
    return result;
}
bool GameAccess::is_compatible_object(void* object,void* expected_class) const {
    const auto identity=validate_object(object);
    validate_class(expected_class);
    validate_class(identity.object_class);
    return compatible_object_class(reinterpret_cast<uintptr_t>(identity.object_class),
        reinterpret_cast<uintptr_t>(expected_class),[this](uintptr_t klass) {
            validate_class(reinterpret_cast<void*>(klass));
            const auto parent=read<uintptr_t>(klass+0x40);
            if(parent)validate_class(reinterpret_cast<void*>(parent));
            return parent;
        });
}
bool GameAccess::is_compatible_class(void* represented_class,void* property_class,void* meta_class) const {
    validate_class(represented_class);
    validate_class(property_class);
    validate_class(meta_class);
    const auto identity=validate_object(represented_class);
    return compatible_class_argument(reinterpret_cast<uintptr_t>(represented_class),
        reinterpret_cast<uintptr_t>(identity.object_class),reinterpret_cast<uintptr_t>(property_class),
        reinterpret_cast<uintptr_t>(meta_class),[this](uintptr_t klass) {
            validate_class(reinterpret_cast<void*>(klass));
            const auto parent=read<uintptr_t>(klass+0x40);
            if(parent)validate_class(reinterpret_cast<void*>(parent));
            return parent;
        });
}
json GameAccess::object_descriptor(void* object,void* expected_class) {
    validate_class(expected_class);
    if(!object)return nullptr;
    if(!is_compatible_object(object,expected_class))
        throw HandlerError("incompatible_object_result","The object is incompatible with the property's declared class");
    const auto identity=validate_object(object);
    const auto path=object_path(object);
    json result={{"handle",handle_for(object,identity.index)},{"index",identity.index},
        {"name",object_name(object)},{"path",path},{"class",object_name(identity.object_class)},
        {"session_id",handle_session_}};
    const auto& reference=handles_.at(result.at("handle").get<std::string>());
    result["world_generation"]=reference.world_generation?json(reference.world_generation):json(nullptr);
    result["script_generation"]=json(nullptr);
    const auto after=validate_object(object);
    if(!same_serial_identity(serial_identity(identity),serial_identity(after)))
        throw HandlerError("stale_handle","The object value changed while being copied");
    return result;
}
json GameAccess::class_descriptor(void* object,void* property_class,void* meta_class) {
    validate_class(property_class);
    validate_class(meta_class);
    if(!object)return nullptr;
    validate_class(object);
    if(!is_compatible_class(object,property_class,meta_class))
        throw HandlerError("incompatible_class_result","UClass is incompatible with PropertyClass or MetaClass");
    auto result=object_descriptor(object,property_class);
    if(!is_compatible_class(object,property_class,meta_class))
        throw HandlerError("incompatible_class_result","The class value changed while being copied");
    return result;
}
json GameAccess::property_value(uintptr_t address,const std::string& type,int size,uintptr_t field) {
    if(type=="ClassProperty") {
        if(size!=8)throw HandlerError("unsupported_property_layout","ClassProperty requires an x64 pointer");
        return class_descriptor(reinterpret_cast<void*>(read<uintptr_t>(address)),
            reinterpret_cast<void*>(read<uintptr_t>(field+0x70)),reinterpret_cast<void*>(read<uintptr_t>(field+0x78)));
    }
    if(type=="ObjectProperty") {
        if(size!=sizeof(uintptr_t))throw HandlerError("unsupported_property_layout","ObjectProperty requires an x64 pointer");
        const auto expected_class=reinterpret_cast<void*>(read<uintptr_t>(field+0x70));
        return object_descriptor(reinterpret_cast<void*>(read<uintptr_t>(address)),expected_class);
    }
    if(type=="WeakObjectProperty") {
        struct WeakValue { int32_t index;int32_t serial; };
        if(size!=sizeof(WeakValue))throw HandlerError("unsupported_property_layout","WeakObjectProperty requires an 8-byte index/serial pair");
        const auto value=read<WeakValue>(address);
        if(value.serial==0 || value.index<0)return nullptr;
        const auto identity=slot_identity(value.index);
        if(!identity.object || identity.serial!=value.serial)return nullptr;
        auto result=object_descriptor(identity.object,reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
        const auto after=slot_identity(value.index);
        const auto copied=read<WeakValue>(address);
        if(after.object!=identity.object || after.serial!=identity.serial || copied.index!=value.index || copied.serial!=value.serial)
            throw HandlerError("stale_handle","The weak object value changed while copied");
        return result;
    }
    if(type=="NameProperty") {
        if(size!=8)throw HandlerError("unsupported_property_layout","NameProperty requires an 8-byte FName");
        return name(read<uint64_t>(address));
    }
    if(type=="EnumProperty" || type=="ByteProperty")return enum_property(address,type,size,field).at("value");
    return scalar(address,type,size,field);
}
GameAccess::EnumTable GameAccess::enum_table(void* object) const {
    if(!object)throw HandlerError("enum_validation_unavailable","The UEnum required for argument validation is missing");
    try {
        const auto identity=validate_object(object);
        const auto enum_class=reinterpret_cast<uintptr_t>(identity.object_class);
        validate_class(identity.object_class);
        // Root read-only probe on 2026-10-31 confirmed /Script/CoreUObject.Enum:
        // index 19, name Enum, class Class; UserDefinedEnum supports inheritance.
        bool valid_enum=false;
        std::set<uintptr_t> visited;
        auto current=enum_class;
        for(;current;) {
            if(!visited.insert(current).second)throw HandlerError("invalid_enum_class","The UEnum class chain contains a cycle");
            validate_class(reinterpret_cast<void*>(current));
            if(object_path(reinterpret_cast<void*>(current))=="/Script/CoreUObject.Enum"){valid_enum=true;break;}
            current=read<uintptr_t>(current+0x40);
        }
        if(!valid_enum)throw HandlerError("invalid_enum_class","The enum declaration is not a UEnum");
        const auto path=object_path(object);
        const auto ptr=reinterpret_cast<uintptr_t>(object);
        const auto data=read<uintptr_t>(ptr+0x40);
        const auto count=read<int32_t>(ptr+0x48),capacity=read<int32_t>(ptr+0x4c);
        const auto bytes=sn2_mcp::enum_values::table_byte_size(count,capacity,data!=0);
        if(data>std::numeric_limits<uintptr_t>::max()-bytes)
            throw HandlerError("invalid_enum_table","The enum table address range overflows");
        std::vector<std::byte> copied(bytes);
        if(bytes && !copy_memory(copied.data(),reinterpret_cast<void*>(data),bytes))
            throw HandlerError("invalid_enum_table","The enum table is unavailable");
        if(read<uintptr_t>(ptr+0x40)!=data || read<int32_t>(ptr+0x48)!=count
           || read<int32_t>(ptr+0x4c)!=capacity)
            throw HandlerError("enum_table_changed","The enum table changed while being copied");
        const auto after=validate_object(object);
        if(!same_serial_identity(serial_identity(identity),serial_identity(after)))
            throw HandlerError("stale_handle","The enum changed while being copied");
        auto entries=sn2_mcp::enum_values::decode_entries(copied,count,capacity);
        return {identity,path,std::move(entries)};
    } catch(const std::invalid_argument& error) {
        throw HandlerError("enum_validation_unavailable",error.what());
    }
}
json GameAccess::enum_metadata(void* object,const json& value,const std::string& underlying_type) {
    json metadata={{"path",nullptr},{"underlying_type",underlying_type},{"name_status","unavailable"},
        {"value_name",nullptr},{"matched_count",0},{"ambiguous",false},{"reason","enum_unavailable"}};
    if(!object){metadata["reason"]="enum_pointer_null";return metadata;}
    try {
        const auto table=enum_table(object);
        metadata["path"]=table.path;
        const sn2_mcp::enum_values::Integer number=value.is_number_unsigned()
            ?sn2_mcp::enum_values::Integer(value.get<uint64_t>()):sn2_mcp::enum_values::Integer(value.get<int64_t>());
        const auto match=sn2_mcp::enum_values::find_label(number,table.entries,[this](uint64_t fname){return name(fname);});
        metadata["name_status"]=match.status;
        metadata["value_name"]=match.name.empty()?json(nullptr):json(match.name);
        metadata["matched_count"]=match.matched_count;metadata["ambiguous"]=match.ambiguous;
        metadata["reason"]=match.reason.empty()?json(nullptr):json(match.reason);
    } catch(const HandlerError& error) {metadata["reason"]=error.code();}
      catch(const std::invalid_argument& error) {metadata["reason"]=error.what();}
    return metadata;
}
GameAccess::EnumLayout GameAccess::enum_layout(const std::string& type,int size,uintptr_t field) const {
    try {
        if(field_type(field,[this](auto f){return name(f);})!=type
           || read<int32_t>(field+0x34)!=size || read<int32_t>(field+0x30)!=1)
            throw HandlerError("unsupported_enum_underlying_layout","The outer enum layout changed or is unsupported");
        std::string underlying_type="ByteProperty";
        uintptr_t enum_ptr=0;
        if(type=="EnumProperty") {
            const auto underlying=read<uintptr_t>(field+0x70);
            if(!underlying)throw HandlerError("unsupported_enum_underlying_layout","UnderlyingProp is missing");
            underlying_type=field_type(underlying,[this](auto f){return name(f);});
            if(read<int32_t>(underlying+0x30)!=1 || read<int32_t>(underlying+0x34)!=size)
                throw HandlerError("unsupported_enum_underlying_layout","The UnderlyingProp size or array dimension does not match");
            enum_ptr=read<uintptr_t>(field+0x78);
        } else enum_ptr=read<uintptr_t>(field+0x70);
        sn2_mcp::enum_values::integer_layout(underlying_type,size);
        return {underlying_type,size,reinterpret_cast<void*>(enum_ptr)};
    } catch(const std::invalid_argument& error) {
        throw HandlerError(error.what(),"Unsupported enum layout");
    }
}
json GameAccess::enum_property(uintptr_t address,const std::string& type,int size,uintptr_t field) {
    namespace ev=sn2_mcp::enum_values;
    try {
        const auto declared=enum_layout(type,size,field);
        const auto layout=ev::integer_layout(declared.underlying_type,size);
        std::array<std::byte,8> copied{};
        if(!copy_memory(copied.data(),reinterpret_cast<void*>(address),layout.bytes))throw HandlerError("invalid_memory","The enum integer value is unavailable");
        const auto integer=ev::decode_integer(declared.underlying_type,size,std::span(copied).first(layout.bytes));
        auto value=std::visit([](auto number){return json(number);},integer);
        json result={{"value",value}};
        if(type=="EnumProperty" || declared.object)result["enum"]=enum_metadata(declared.object,value,declared.underlying_type);
        return result;
    } catch(const std::invalid_argument& error) {
        throw HandlerError(error.what(),"Unsupported enum layout");
    }
}
void* GameAccess::resolve(const std::string& id) {
    auto found=handles_.find(id);if(found==handles_.end())throw HandlerError("stale_handle","The handle is unknown or expired");
    const auto& h=found->second;
    if(h.bound_world && h.world_generation!=world_generation_.load())
        throw HandlerError("world_changed","The handle belongs to a previous selected world; obtain a new handle",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},
             {"previous_world_generation",h.world_generation},{"world_generation",world_generation_.load()}});
    const auto current=validate_object(h.object);
    if(!same_serial_identity(serial_identity(h),serial_identity(current)))
        throw HandlerError("stale_handle","Observed object or class identity changed; obtain a new handle");
    if(h.bound_world!=object_world(h.object))throw HandlerError("world_changed","The object belongs to a different world than when its handle was created");
    return h.object;
}
json GameAccess::object_info(void* object,int index) {
    const auto handle=handle_for(object,index);
    const auto& reference=handles_.at(handle);
    auto ptr=reinterpret_cast<uintptr_t>(object);return {{"handle",handle},{"index",index},{"name",object_name(object)},
        {"path",object_path(object)},{"class",object_name(reinterpret_cast<void*>(read<uintptr_t>(ptr+0x10)))},{"flags",read<uint32_t>(ptr+8)},
        {"world_generation",reference.world_generation?json(reference.world_generation):json(nullptr)},
        {"script_generation",json(nullptr)}};
}
json GameAccess::properties(void* object,std::size_t limit,std::size_t page_offset) {
    json items=json::array();auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);bool truncated=false;
    std::size_t scanned{};
    std::set<uintptr_t> visited_classes,visited_fields;
    for(;klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!visited_classes.insert(klass).second)
            throw HandlerError("property_scan_cycle","Class chain contains a cycle");
        auto field=read<uintptr_t>(klass+0x50);auto class_size=read<int32_t>(klass+0x58);
        for(;field;field=read<uintptr_t>(field+0x18)) {
            if(!visited_fields.insert(field).second)
                throw HandlerError("property_scan_cycle","Property chain contains a cycle");
            if(scanned<page_offset){++scanned;continue;}
            if(items.size()>=limit){truncated=true;break;}
            ++scanned;
            auto type=field_type(field,[this](auto f){return name(f);});auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34);
            json item={{"name",name(read<uint64_t>(field+0x20))},{"type",type},{"offset",offset},{"element_size",size},{"array_dim",read<int32_t>(field+0x30)}};
            if(offset>=0 && size>0 && offset<=class_size-size && item["array_dim"]==1) {
                try {
                    json value;
                    if(type=="EnumProperty" || type=="ByteProperty") {
                        const auto result=enum_property(reinterpret_cast<uintptr_t>(object)+offset,type,size,field);
                        value=result.at("value");if(result.contains("enum"))item["enum"]=result.at("enum");
                    } else value=property_value(reinterpret_cast<uintptr_t>(object)+offset,type,size,field);
                    item["value"]=value;item["supported"]=type=="ObjectProperty" || type=="ClassProperty" || type=="WeakObjectProperty" || !value.is_null();
                } catch(const HandlerError& error) {
                    item["supported"]=false;item["error"]={{"code",error.code()},{"message",error.what()}};
                }
            } else {item["supported"]=false;}
            items.push_back(item);
        } if(truncated)break;
    }
    return {{"items",items},{"truncated",truncated},{"scan_complete",!truncated},
            {"offset",page_offset},{"scanned_fields",scanned},{"next_offset",truncated?json(scanned):json(nullptr)},
            {"offset_kind","inherited_property_ordinal"},{"validation","game_thread_memory_read"}};
}
json GameAccess::selected_properties(void* object,const json& names) {
    json items=json::array();
    std::unordered_map<std::string,json> selected;
    auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);
    size_t scanned=0;bool scan_truncated=false;
    std::set<uintptr_t> visited_classes,visited_fields;
    for(;klass && selected.size()<names.size();klass=read<uintptr_t>(klass+0x40)) {
        if(!visited_classes.insert(klass).second){scan_truncated=true;break;}
        auto field=read<uintptr_t>(klass+0x50);auto class_size=read<int32_t>(klass+0x58);
        for(;field && selected.size()<names.size();field=read<uintptr_t>(field+0x18)) {
            if(!visited_fields.insert(field).second){scan_truncated=true;break;}
            ++scanned;
            const auto field_name=name(read<uint64_t>(field+0x20));
            if(selected.contains(field_name) || std::find(names.begin(),names.end(),json(field_name))==names.end())continue;
            const auto type=field_type(field,[this](auto f){return name(f);});
            const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34);
            json item={{"name",field_name},{"type",type},{"supported",false}};
            try {
                if(offset<0 || size<=0 || size>class_size || offset>class_size-size
                   || read<int32_t>(field+0x30)!=1)
                    throw HandlerError("unsupported_property_layout","Unsupported property layout");
                json value;
                if(type=="EnumProperty" || type=="ByteProperty") {
                    const auto result=enum_property(reinterpret_cast<uintptr_t>(object)+offset,type,size,field);
                    value=result.at("value");if(result.contains("enum"))item["enum"]=result.at("enum");
                } else value=property_value(reinterpret_cast<uintptr_t>(object)+offset,type,size,field);
                if(value.is_null() && type!="ObjectProperty" && type!="ClassProperty" && type!="WeakObjectProperty")throw HandlerError("unsupported_property_type","The property type is unsupported");
                item["value"]=std::move(value);item["supported"]=true;
            } catch(const HandlerError& error) {
                item["error"]={{"code",error.code()},{"message",error.what()}};
            }
            selected.emplace(field_name,std::move(item));
        }
        if(scan_truncated)break;
    }
    bool partial=scan_truncated;
    for(const auto& requested:names) {
        const auto key=requested.get<std::string>();
        auto found=selected.find(key);
        if(found==selected.end()) {
            items.push_back({{"name",key},{"type",nullptr},{"supported",false},
                {"error",{{"code",scan_truncated?"property_scan_truncated":"property_not_found"},
                           {"message",scan_truncated?"Property chain contains a cycle":"Property not found"}}}});
            partial=true;
        } else {
            if(!found->second.at("supported").get<bool>())partial=true;
            items.push_back(found->second);
        }
    }
    return {{"properties",items},{"partial",partial},{"truncated",scan_truncated},{"scanned_fields",scanned}};
}
json GameAccess::snapshot(const json& targets) {
    Observations::validate_targets(targets);
    json result={{"snapshot_id","snapshot-"+std::to_string(++next_snapshot_)},
        {"source","live_native"},{"session_id",handle_session_},{"process_id",GetCurrentProcessId()},{"world_generation",world_generation_.load()},
        {"script_generation",nullptr},{"reflection_generation",nullptr},
        {"thread_id",GetCurrentThreadId()},{"tick_count",ticks_.load()},
        {"consistency","single_tick_sequential"},{"objects",json::array()},
        {"partial",false},{"truncated",false},
        {"limits",{{"objects",nullptr},{"properties_per_object",nullptr},{"scan_fields_per_object",nullptr},
                   {"property_value_bytes",nullptr},{"snapshot_bytes",nullptr}}}};
    for(const auto& target:targets) {
        const auto handle=target.at("handle").get<std::string>();
        json item={{"handle",handle},{"properties",json::array()}};
        try {
            auto object=resolve(handle);
            item["path"]=object_path(object);
            item["class"]=object_name(reinterpret_cast<void*>(read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10)));
            const auto fields=selected_properties(object,target.at("properties"));
            resolve(handle);
            item["properties"]=fields.at("properties");
            item["partial"]=fields.at("partial");item["truncated"]=fields.at("truncated");
            if(fields.at("partial").get<bool>())result["partial"]=true;
            if(fields.at("truncated").get<bool>())result["truncated"]=true;
        } catch(const HandlerError& error) {
            item["properties"]=json::array();
            item["error"]={{"code",error.code()},{"message",error.what()}};
            result["partial"]=true;
        }
        result["objects"].push_back(std::move(item));
    }
    return result;
}
uint64_t GameAccess::encode_name_text(const json& value) const {
    if(!bindings_.name_construct.available())
        throw HandlerError("name_constructor_unavailable","The current executable FName constructor binding is unavailable",
            {{"executed",false},{"may_have_executed",false}});
    if(value.size()!=1 || !value.at("name").is_string())
        throw HandlerError("invalid_name_argument","Text FName requires only {name:string}");
    const auto& text=value.at("name").get_ref<const std::string&>();
    if(text.find('\0')!=std::string::npos)
        throw HandlerError("invalid_name_argument","Text FName cannot contain an embedded NUL");
    std::wstring encoded;
    if(!text.empty()) {
        if(text.size()>static_cast<std::size_t>(std::numeric_limits<int>::max()))
            throw HandlerError("invalid_name_argument","Text FName exceeds the Windows UTF-8 conversion extent");
        const auto length=static_cast<int>(text.size());
        const auto units=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),length,nullptr,0);
        if(!units)throw HandlerError("invalid_name_argument","Text FName contains invalid UTF-8");
        encoded.resize(static_cast<size_t>(units));
        if(MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),length,encoded.data(),units)!=units)
            throw HandlerError("invalid_name_argument","Text FName could not be converted to UTF-16");
    }
    uint64_t packed{};
    ++name_constructions_started_;
    if(!construct_name(base_+kNameConstruct,&packed,reinterpret_cast<const char16_t*>(encoded.c_str())))
        throw HandlerError("name_constructor_fault","The FName constructor raised a native exception");
    const auto decoded=name(packed);
    if(!text.empty() && (decoded.size()!=text.size() || !object_query::contains_case_insensitive(decoded,text)))
        throw HandlerError("name_constructor_mismatch","The constructed FName does not round-trip the requested text");
    return packed;
}
std::string GameAccess::read_copied_string(const StringHeader& header) const {return copied_string(header);}
scalar_struct_values::Snapshot GameAccess::capture_scalar_struct_parameter(uintptr_t field,int size,int dim) const {
    return scalar_struct_values::capture(field,size,dim,
        []<class Value>(uintptr_t address){return read<Value>(address);},
        [this](uint64_t value){return name(value);},
        [this](uintptr_t value){return serial_identity(validate_object(reinterpret_cast<void*>(value)));},
        [this](uintptr_t value){validate_class(reinterpret_cast<void*>(value));},
        [this](uintptr_t value){return object_path(reinterpret_cast<void*>(value));});
}
json GameAccess::scalar_struct_snapshot_json(const scalar_struct_values::Snapshot& value,bool raw) {
    json components=json::array();
    for(const auto& child:value.components) {
        auto item=scalar_output_metadata(child.component.layout);
        item["name"]=child.component.name;item["offset"]=child.component.offset;
        if(raw)item.update({{"pointer",child.pointer},{"next",child.next},{"field_class",child.field_class},
            {"owner",child.owner},{"field_name",child.name},{"type_name",child.type_name},{"flags",child.flags}});
        components.push_back(std::move(item));
    }
    json result={{"struct_path",value.path},{"struct_name",value.struct_name},{"properties_size",value.layout.properties_size},
        {"min_alignment",value.layout.min_alignment},{"components",std::move(components)}};
    if(raw) {
        const auto& identity=value.identity;
        result.update({{"field",value.field},{"next",value.next},{"field_class",value.field_class},{"field_owner",value.owner},
            {"field_name",value.name},{"field_type_name",value.type_name},{"flags",value.flags},{"offset",value.offset},
            {"structure",value.structure},{"head",value.head},{"super",value.super},{"element_size",value.element_size},
            {"array_dim",value.array_dim},{"identity",{{"object",identity.object},{"object_class",identity.object_class},
                {"index",identity.index},{"name",identity.name},{"serial",identity.serial},{"class_index",identity.class_index},
                {"class_name",identity.class_name},{"class_serial",identity.class_serial}}}});
    }
    return result;
}
scalar_struct_values::Snapshot GameAccess::capture_gameplay_tag_parameter(uintptr_t field,int size,int dim) const {
    scalar_struct_values::Snapshot result;
    result.field=field;result.next=read<uintptr_t>(field+0x18);result.field_class=read<uintptr_t>(field+8);
    result.owner=read<uint64_t>(field+0x10);result.name=read<uint64_t>(field+0x20);result.type_name=read<uint64_t>(result.field_class);
    result.flags=read<uint64_t>(field+0x38);result.offset=read<int32_t>(field+0x44);
    result.element_size=read<int32_t>(field+0x34);result.array_dim=read<int32_t>(field+0x30);
    if(name(result.type_name)!="StructProperty" || result.element_size!=size || result.array_dim!=dim || dim!=1 || size<8)
        throw HandlerError("unsupported_gameplay_tag_layout","GameplayTag requires one matching StructProperty");
    result.structure=read<uintptr_t>(field+0x70);
    result.identity=serial_identity(validate_object(reinterpret_cast<void*>(result.structure)));
    validate_class(reinterpret_cast<void*>(result.identity.object_class));
    result.path=object_path(reinterpret_cast<void*>(result.structure));result.struct_name=name(result.identity.name);
    result.head=read<uintptr_t>(result.structure+0x50);result.super=read<uintptr_t>(result.structure+0x40);
    result.layout={read<int32_t>(result.structure+0x58),read<int16_t>(result.structure+0x5c),result.super!=0,{}};
    if(result.path!="/Script/GameplayTags.GameplayTag" || result.struct_name!="GameplayTag")
        throw HandlerError("unsupported_gameplay_tag_type","The input requires exact GameplayTags.GameplayTag");
    if(result.layout.properties_size!=size || result.super || !result.head || result.layout.min_alignment<1
       || (result.layout.min_alignment&(result.layout.min_alignment-1)) || size%result.layout.min_alignment)
        throw HandlerError("unsupported_gameplay_tag_layout","GameplayTag requires matching reflected size, alignment, and own fields");
    scalar_struct_values::ComponentMetadata tag;const auto tag_field=result.head;
    tag.pointer=tag_field;tag.next=read<uintptr_t>(tag_field+0x18);tag.field_class=read<uintptr_t>(tag_field+8);
    tag.owner=read<uint64_t>(tag_field+0x10);tag.name=read<uint64_t>(tag_field+0x20);
    tag.type_name=read<uint64_t>(tag.field_class);tag.flags=read<uint64_t>(tag_field+0x38);
    tag.component={name(tag.name),read<int32_t>(tag_field+0x44),
        {name(tag.type_name),read<int32_t>(tag_field+0x34),read<int32_t>(tag_field+0x30)}};
    if(tag.next || tag.component.name!="TagName" || tag.component.layout.type!="NameProperty"
       || tag.component.layout.element_size!=8 || tag.component.layout.array_dim!=1
       || tag.component.offset<0 || tag.component.offset>size-8)
        throw HandlerError("unsupported_gameplay_tag_layout","GameplayTag requires one own TagName NameProperty");
    result.layout.components.push_back(tag.component);result.components.push_back(tag);
    return result;
}
json GameAccess::capture_function_contract(void* object,uintptr_t owner,uintptr_t function,const std::string& target_handle) {
    auto identity=[](const Handle& value) {
        const auto observed=serial_identity(value);
        return json{{"object",observed.object},{"object_class",observed.object_class},
            {"index",observed.index},{"name",observed.name},{"serial",observed.serial},
            {"class_index",observed.class_index},{"class_name",observed.class_name},{"class_serial",observed.class_serial}};
    };
    if(resolve(target_handle)!=object)throw HandlerError("signature_token_mismatch","The signature handle belongs to a different object");
    validate_class(reinterpret_cast<void*>(owner));
    const auto object_identity=validate_object(object),owner_identity=validate_object(reinterpret_cast<void*>(owner));
    const auto function_identity=validate_object(reinterpret_cast<void*>(function));
    if(!contains(object_name(function_identity.object_class),"Function"))
        throw HandlerError("unsupported_signature","The reflected entry is not a UFunction");
    const auto function_name=object_name(reinterpret_cast<void*>(function));
    require_flat_parameter_call(object_path(reinterpret_cast<void*>(owner)),function_name);
    const auto field_head=read<uintptr_t>(function+0x50);
    const auto function_flags=read<uint32_t>(function+0xb0);
    const auto declared_count=read<uint8_t>(function+0xb4);
    const auto parameter_bytes=read<uint16_t>(function+0xb6);
    const auto return_offset=read<uint16_t>(function+0xb8);
    json parameters=json::array();int count{},returns{},extent=parameter_bytes;
    const auto fields=capture_function_field_chain(field_head,[&](uintptr_t field) {
        const auto field_class=read<uintptr_t>(field+8),field_name=read<uint64_t>(field+0x20);
        const auto type_name=read<uint64_t>(field_class);
        const auto type=name(type_name);const auto flags=read<uint64_t>(field+0x38);
        const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34),array_dim=read<int32_t>(field+0x30);
        json captured={{"pointer",field},{"next",read<uintptr_t>(field+0x18)},
            {"owner",read<uintptr_t>(field+0x10)},{"field_class",field_class},{"type_name",type_name},
            {"type",type},{"name",field_name},{"flags",flags},{"offset",offset},{"size",size},{"array_dim",array_dim}};
        if(!(flags&0x80))return captured;
        ++count;extent=parameter_extent(extent,offset,size,array_dim);
        const bool returned=(flags&0x400)!=0;
        if(returned && ++returns>1)throw HandlerError("unsupported_signature","UFunction has multiple return parameters");
        const bool const_name_input_reference=type=="NameProperty" && !returned && (flags&kConstParm) && (flags&(0x100|kReferenceParm));
        if(const_name_input_reference)validate_name_argument_layout(size,array_dim);
        const bool scalar_output=!returned && supported_scalar_output_type(type) && (flags&0x100);
        json scalar_public=nullptr;
        if(scalar_output) {
            if(parameter_direction::classify(flags)!=parameter_direction::Direction::output)
                throw HandlerError("unsupported_signature","The scalar parameter is not a pure output");
            scalar_public=scalar_output_metadata(capture_scalar_output_layout(field,type,size,array_dim));
        } else if(!returned && !const_name_input_reference && type!="StructProperty" && type!="StrProperty" && type!="ArrayProperty" && (flags&(0x100|kReferenceParm)))
            throw HandlerError("unsupported_signature","Out or reference arguments of this type are unsupported");
        if(returned && supported_scalar_output_type(type) && (flags&(kReferenceParm|kConstParm)))
            throw HandlerError("unsupported_signature","Const or reference scalar returns are unsupported");
        bool supported=false;
        json vector_public=nullptr,transform_public=nullptr,rotator_public=nullptr,array_public=nullptr,color_public=nullptr,scalar_struct_public=nullptr,gameplay_tag_public=nullptr,set_public=nullptr;std::string vector_direction;
        if(type=="BoolProperty") {
            const auto byte_offset=read<uint8_t>(field+0x71),mask=read<uint8_t>(field+0x73);
            validate_bool_layout(size,byte_offset,mask);
            captured["aux"]={{"field_size",read<uint8_t>(field+0x70)},{"byte_offset",byte_offset},
                {"byte_mask",read<uint8_t>(field+0x72)},{"field_mask",mask}};supported=true;
        } else if(type=="IntProperty")supported=size==4;
        else if(type=="FloatProperty")supported=size==4;
        else if(type=="DoubleProperty")supported=size==8;
        else if(type=="NameProperty")supported=size==8;
        else if(type=="StructProperty") {
            const auto structure_path=object_path(reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
            if(structure_path=="/Script/GameplayTags.GameplayTag") {
                if(returned || vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","GameplayTag requires input by value or const reference");
                const auto tag=capture_gameplay_tag_parameter(field,size,array_dim);
                if(offset%tag.layout.min_alignment)
                    throw HandlerError("unsupported_gameplay_tag_layout","The GameplayTag offset does not match its reflected alignment");
                captured["aux"]={{"gameplay_tag",scalar_struct_snapshot_json(tag)}};
                gameplay_tag_public=scalar_struct_snapshot_json(tag,false);supported=true;
            } else if(structure_path=="/Script/CoreUObject.LinearColor") {
                if(flags&(kConstParm|kReferenceParm) || (!returned && (flags&0x100)))
                    throw HandlerError("unsupported_signature","FLinearColor is supported only by value without Const, Ref or Out");
                const auto color=capture_linear_color_parameter(field,size,array_dim);
                captured["aux"]={{"linear_color",linear_color_snapshot_json(color)}};
                color_public=linear_color_snapshot_json(color,false);supported=true;
            } else if(structure_path=="/Script/CoreUObject.Transform") {
                if(returned) {
                    if(flags&(kConstParm|kReferenceParm))
                        throw HandlerError("unsupported_signature","Const or reference Transform returns are unsupported");
                } else if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","Transform requires input by value or const reference; Out is unsupported");
                const auto transform=capture_transform_parameter(field,size,array_dim);
                if(offset%transform.layout.min_alignment)
                    throw HandlerError("unsupported_transform_layout","The Transform offset does not match its reflected alignment");
                captured["aux"]={{"transform",transform_snapshot_json(transform)}};
                transform_public=transform_snapshot_json(transform,false);supported=true;
            } else if(structure_path=="/Script/CoreUObject.Vector") {
                if(!returned) {
                    const auto direction=vector_parameter_direction::classify(flags);
                    vector_direction=direction==vector_parameter_direction::Direction::output?"output":"input";
                } else {
                    if(flags&(kReferenceParm|kConstParm))
                        throw HandlerError("unsupported_signature","Const or reference FVector returns are unsupported");
                    vector_direction="return";
                }
                const auto vector=capture_vector_parameter(field,size,array_dim);
                captured["aux"]={{"vector",vector_snapshot_json(vector)}};
                vector_public=vector_snapshot_json(vector,false);supported=true;
            } else if(structure_path=="/Script/CoreUObject.Rotator") {
                if(returned) {
                    if(flags&(kConstParm|kReferenceParm))
                        throw HandlerError("unsupported_signature","Const or reference FRotator returns are unsupported");
                } else if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","FRotator requires input by value or const reference; mutable Out is unsupported");
                const auto rotator=capture_scalar_struct_parameter(field,size,array_dim);
                rotator_arguments::validate(rotator);
                if(offset%rotator.layout.min_alignment)
                    throw HandlerError("unsupported_rotator_layout","The FRotator offset does not match its reflected alignment");
                captured["aux"]={{"rotator",scalar_struct_snapshot_json(rotator)}};
                rotator_public=scalar_struct_snapshot_json(rotator,false);supported=true;
            } else {
                const bool output=!returned && vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output;
                if((returned && (flags&(kConstParm|kReferenceParm)))
                   || (!returned && !output && (flags&(kConstParm|kReferenceParm|0x100))))
                    throw HandlerError("unsupported_signature","DTO requires input, return by value, or pure output");
                const auto scalar_struct=capture_pod_struct_parameter(field,size,array_dim);
                if(offset%scalar_struct.at("min_alignment").get<int>())
                    throw HandlerError("unsupported_scalar_struct_layout","The DTO offset does not match its reflected alignment");
                captured["aux"]={{"scalar_struct",scalar_struct}};
                if(output)captured["aux"]["scalar_struct_output"]=true;
                scalar_struct_public=pod_struct_metadata(scalar_struct);supported=true;
            }
        }
        else if(type=="StrProperty") {
            supported=size==16;
            if(offset%8)throw HandlerError("unsupported_signature","FString requires 8-byte parameter alignment");
            if(!returned && vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output) {
                if(array_dim!=1)throw HandlerError("unsupported_signature","FString output requires ArrayDim=1");
                parameter_direction::classify(flags);
            }
        }
        else if(type=="SetProperty") {
            const auto set=capture_object_set_return(field,size,array_dim);
            captured["aux"]={{"object_set",set}};set_public=set.at("public");supported=true;
        }
        else if(type=="ArrayProperty") {
            if(returned) {
                const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});
                const bool scalar_array=supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="EnumProperty" || inner_type=="ByteProperty" || inner_type=="StructProperty";
                const auto array=scalar_array?capture_scalar_array_return(field,size,array_dim):capture_object_array_return(field,size,array_dim);
                captured["aux"]={{scalar_array?"scalar_array_return":"object_array_return",array}};array_public=array.at("public");supported=true;
            } else {
                if((flags&0x100) && !(flags&kConstParm)) {
                    const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});
                    const bool scalar_array=supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="EnumProperty" || inner_type=="ByteProperty" || inner_type=="StructProperty";
                    const auto array=scalar_array?capture_scalar_array_return(field,size,array_dim,true):capture_object_array_return(field,size,array_dim,true);
                    captured["aux"]={{scalar_array?"scalar_array_output":"object_array_output",array}};array_public=array.at("public");supported=true;
                } else if(field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);})=="ObjectProperty") {
                    const auto array=capture_object_array_parameter(field,size,array_dim,flags);
                    captured["aux"]={{"object_array",array}};array_public=array.at("public");supported=true;
                } else if(const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="StructProperty") {
                    const auto array=capture_scalar_array_parameter(field,size,array_dim,flags);
                    captured["aux"]={{"scalar_array",array}};array_public=array.at("public");supported=true;
                } else {
                    const auto array=capture_enum_array_parameter(field,size,array_dim,flags);
                    captured["aux"]={{"enum_array",array}};array_public=array.at("public");supported=true;
                }
            }
        }
        else if(type=="Int64Property" || type=="UInt64Property")supported=size==8;
        else if(type=="UInt32Property")supported=size==4;
        else if(type=="ClassProperty" || type=="ObjectProperty") {
            const auto declared=read<uintptr_t>(field+0x70);
            validate_class(reinterpret_cast<void*>(declared));
            captured["aux"]={{"declared",identity(validate_object(reinterpret_cast<void*>(declared)))},
                {"declared_type",{{"name",object_name(reinterpret_cast<void*>(declared))},
                                  {"path",object_path(reinterpret_cast<void*>(declared))}}}};
            if(type=="ClassProperty") {
                const auto meta=read<uintptr_t>(field+0x78);validate_class(reinterpret_cast<void*>(meta));
                captured["aux"]["meta"]=identity(validate_object(reinterpret_cast<void*>(meta)));
                captured["aux"]["meta_type"]={{"name",object_name(reinterpret_cast<void*>(meta))},
                                              {"path",object_path(reinterpret_cast<void*>(meta))}};
            }
            supported=size==8;
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto layout=enum_layout(type,size,field);
            captured["aux"]={{"underlying_type",layout.underlying_type},{"size",layout.size}};
            if(type=="EnumProperty") {
                const auto underlying=read<uintptr_t>(field+0x70);
                const auto underlying_class=read<uintptr_t>(underlying+8);
                captured["aux"]["underlying"]={{"pointer",underlying},{"field_class",underlying_class},
                    {"type_name",read<uint64_t>(underlying_class)},{"owner",read<uintptr_t>(underlying+0x10)},
                    {"next",read<uintptr_t>(underlying+0x18)},{"name",read<uint64_t>(underlying+0x20)},
                    {"array_dim",read<int32_t>(underlying+0x30)},{"size",read<int32_t>(underlying+0x34)},
                    {"flags",read<uint64_t>(underlying+0x38)},{"offset",read<int32_t>(underlying+0x44)}};
            }
            if(layout.object) {
                const auto table=enum_table(layout.object);json entries=json::array();
                for(const auto& entry:table.entries)
                    entries.push_back({{"name",entry.name},{"label",name(entry.name)},{"value",entry.value}});
                const auto enum_address=reinterpret_cast<uintptr_t>(layout.object);
                captured["aux"]["enum"]={{"identity",identity(table.identity)},{"path",table.path},{"entries",std::move(entries)},
                    {"data",read<uintptr_t>(enum_address+0x40)},{"count",read<int32_t>(enum_address+0x48)},
                    {"capacity",read<int32_t>(enum_address+0x4c)}};
            } else captured["aux"]["enum"]=nullptr;
            supported=true;
        }
        if(!supported)throw HandlerError("unsupported_signature","The parameter type or size is unsupported: "+type);
        json public_parameter={{"name",name(field_name)},{"type",type},{"offset",offset},{"element_size",size},
            {"array_dim",array_dim},{"flags",flags},{"return",returned},{"out",(flags&0x100)!=0},
            {"reference",(flags&kReferenceParm)!=0},{"const",(flags&kConstParm)!=0}};
        if(captured.contains("aux"))public_parameter.update(public_parameter_type_metadata(type,captured.at("aux")));
        if(const_name_input_reference) {
            public_parameter["direction"]="input";public_parameter["passing"]="const_reference";
        }
        if(!gameplay_tag_public.is_null()) {
            public_parameter["struct"]=gameplay_tag_public;public_parameter["direction"]="input";
            public_parameter["passing"]=(flags&kConstParm)?"const_reference":"value";
        }
        if(!set_public.is_null()) {
            public_parameter["set"]=set_public;public_parameter["direction"]="return";public_parameter["passing"]="value";
        }
        if(!vector_public.is_null()) {
            public_parameter["struct"]=vector_public;public_parameter["direction"]=vector_direction;
            if(vector_direction=="input")public_parameter["passing"]=(flags&kConstParm)?"const_reference":"value";
            if(vector_direction=="output")public_parameter["argument_marker"]={{"out",true}};
        }
        if(!scalar_public.is_null()) {
            public_parameter["direction"]="output";public_parameter["argument_marker"]={{"out",true}};
            public_parameter["out_metadata"]=scalar_public;
        }
        if(!transform_public.is_null()) {
            public_parameter["struct"]=transform_public;public_parameter["direction"]=returned?"return":"input";
            public_parameter["passing"]=!returned && (flags&kConstParm)?"const_reference":"value";
        }
        if(!rotator_public.is_null()) {
            public_parameter["struct"]=rotator_public;public_parameter["direction"]=returned?"return":"input";
            public_parameter["passing"]=!returned && (flags&kConstParm)?"const_reference":"value";
        }
        if(type=="StrProperty" && !returned) {
            if(vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output) {
                public_parameter["direction"]="output";public_parameter["argument_marker"]={{"out",true}};
                public_parameter["out_metadata"]={{"type","StrProperty"},{"element_size",16},{"array_dim",1}};
            } else {
                public_parameter["direction"]="input";public_parameter["passing"]=(flags&kConstParm)?"const_reference":"value";
                public_parameter["argument_constraints"]={{"max_utf16_units",string_arguments::max_utf16_units},{"embedded_nul","rejected"},{"ownership","engine_owned_copy"}};
            }
        }
        if(!array_public.is_null()) {
            public_parameter["array"]=array_public;public_parameter["direction"]=returned?"return":"input";public_parameter["passing"]=returned?"value":"const_reference";
            if(!returned) {
                const bool value_array=captured["aux"].contains("enum_array") || captured["aux"].contains("scalar_array");
                if(captured["aux"].contains("object_array_output") || captured["aux"].contains("scalar_array_output")) {
                    public_parameter["direction"]="output";public_parameter.erase("passing");
                    public_parameter["argument_marker"]={{"out",true}};
                } else {
                    if(value_array)public_parameter["passing"]=(flags&kConstParm)?"const_reference":"value";
                    public_parameter["argument_constraints"]={{"max_elements",std::numeric_limits<int32_t>::max()},
                        {"element_form",value_array?(captured["aux"].contains("scalar_array") && captured["aux"]["scalar_array"].at("scalar_type")=="NameProperty"?"name_or_name_from":"integer"):"null_or_handle"},
                        {"ownership",value_array?"engine_owned_copy":"engine_owned_pointer_copy"}};
                }
            }
        }
        if(!color_public.is_null()) {
            public_parameter["struct"]=color_public;public_parameter["direction"]=returned?"return":"input";public_parameter["passing"]="value";
        }
        if(!scalar_struct_public.is_null()) {
            public_parameter["struct"]=scalar_struct_public;public_parameter["direction"]=returned?"return":"input";public_parameter["passing"]="value";
            if(captured["aux"].contains("scalar_struct_output")) {
                public_parameter["direction"]="output";public_parameter.erase("passing");public_parameter["argument_marker"]={{"out",true}};
            }
        }
        parameters.push_back(std::move(public_parameter));
        return captured;
    });
    if(count!=declared_count)throw HandlerError("unsupported_signature","NumParms does not match the complete CPF_Parm chain");
    json result={{"session",handle_session_},{"target_handle",target_handle},{"function_name",function_name},
        {"world_generation",handles_.at(target_handle).world_generation},
        {"script_generation",nullptr},{"reflection_generation",nullptr},
        {"target",identity(object_identity)},{"owner",identity(owner_identity)},{"function",identity(function_identity)},
        {"function_outer",read<uintptr_t>(function+0x20)},{"function_flags",function_flags},
        {"header",{{"parameter_count",declared_count},{"parameter_size",parameter_bytes},{"return_offset",return_offset},
                   {"child_head",field_head},{"computed_extent",extent}}},{"fields",fields},
        {"public_function",{{"name",function_name},{"owner",object_name(reinterpret_cast<void*>(owner))},
            {"flags",function_flags},{"parameter_count",declared_count},{"parameter_size",parameter_bytes},
            {"return_offset",return_offset},{"parameters",parameters},{"parameters_truncated",false}}}};
    if(!same_serial_identity(serial_identity(object_identity),serial_identity(validate_object(object)))
       || !same_serial_identity(serial_identity(owner_identity),serial_identity(validate_object(reinterpret_cast<void*>(owner))))
       || !same_serial_identity(serial_identity(function_identity),serial_identity(validate_object(reinterpret_cast<void*>(function)))))
        throw HandlerError("signature_changed","UFunction or owner identity changed during the read");
    if(read<uintptr_t>(function+0x50)!=field_head || read<uint32_t>(function+0xb0)!=function_flags
       || read<uint8_t>(function+0xb4)!=declared_count || read<uint16_t>(function+0xb6)!=parameter_bytes
       || read<uint16_t>(function+0xb8)!=return_offset || read<uintptr_t>(function+0x20)!=result.at("function_outer").get<uintptr_t>())
        throw HandlerError("signature_changed","The UFunction header changed during the read");
    for(const auto& captured:fields) {
        const auto field=captured.at("pointer").get<uintptr_t>(),field_class=captured.at("field_class").get<uintptr_t>();
        if(read<uintptr_t>(field+0x18)!=captured.at("next").get<uintptr_t>()
           || read<uintptr_t>(field+0x10)!=captured.at("owner").get<uintptr_t>()
           || read<uintptr_t>(field+8)!=field_class || read<uint64_t>(field_class)!=captured.at("type_name").get<uint64_t>()
           || read<uint64_t>(field+0x20)!=captured.at("name").get<uint64_t>()
           || read<uint64_t>(field+0x38)!=captured.at("flags").get<uint64_t>()
           || read<int32_t>(field+0x44)!=captured.at("offset").get<int32_t>()
           || read<int32_t>(field+0x34)!=captured.at("size").get<int32_t>()
           || read<int32_t>(field+0x30)!=captured.at("array_dim").get<int32_t>())
            throw HandlerError("signature_changed","The UFunction field chain changed during the read");
        if(!captured.contains("aux"))continue;
        const auto& auxiliary=captured.at("aux");const auto type=captured.at("type").get<std::string>();
        if(type=="StructProperty") {
            if(auxiliary.contains("gameplay_tag")) {
                const auto current=capture_gameplay_tag_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
                if(scalar_struct_snapshot_json(current)!=auxiliary.at("gameplay_tag"))
                    throw HandlerError("signature_changed","GameplayTag reflection changed while reading the signature");
                continue;
            }
            if(auxiliary.contains("rotator")) {
                const auto current=capture_scalar_struct_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
                rotator_arguments::validate(current);
                if(scalar_struct_snapshot_json(current)!=auxiliary.at("rotator"))
                    throw HandlerError("signature_changed","The nested FRotator reflection changed while reading the signature");
                continue;
            }
            if(auxiliary.contains("scalar_struct")) {
                const auto current=capture_pod_struct_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
                if(current!=auxiliary.at("scalar_struct"))
                    throw HandlerError("signature_changed","The scalar DTO changed while reading the signature");
            } else if(auxiliary.contains("linear_color")) {
                const auto current=capture_linear_color_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
                if(linear_color_snapshot_json(current)!=auxiliary.at("linear_color"))
                    throw HandlerError("signature_changed","FLinearColor metadata changed while reading the signature");
            } else if(auxiliary.contains("transform")) {
                const auto current=capture_transform_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
                if(transform_snapshot_json(current)!=auxiliary.at("transform"))
                    throw HandlerError("signature_changed","Transform layout changed while reading the signature");
            } else {
            const auto current=capture_vector_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>());
            if(vector_snapshot_json(current)!=auxiliary.at("vector"))
                throw HandlerError("signature_changed","FVector layout changed while reading the signature");
            }
        } else if(type=="SetProperty") {
            if(capture_object_set_return(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>())!=auxiliary.at("object_set"))
                throw HandlerError("signature_changed","The returned set metadata changed while reading the signature");
        } else if(type=="ArrayProperty") {
            if(captured.at("flags").get<uint64_t>()&0x400) {
                if(auxiliary.contains("scalar_array_return")) {
                    if(capture_scalar_array_return(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>())!=auxiliary.at("scalar_array_return"))
                        throw HandlerError("signature_changed","The returned scalar array metadata changed while reading the signature");
                } else if(capture_object_array_return(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>())!=auxiliary.at("object_array_return"))
                    throw HandlerError("signature_changed","The returned object array metadata changed while reading the signature");
            } else {
                if(auxiliary.contains("scalar_array_output")) {
                    if(capture_scalar_array_return(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>(),true)!=auxiliary.at("scalar_array_output"))
                        throw HandlerError("signature_changed","The scalar array output metadata changed while reading the signature");
                } else if(auxiliary.contains("object_array_output")) {
                    if(capture_object_array_return(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>(),true)!=auxiliary.at("object_array_output"))
                        throw HandlerError("signature_changed","The object array output metadata changed while reading the signature");
                } else if(auxiliary.contains("scalar_array")) {
                    if(capture_scalar_array_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>(),captured.at("flags").get<uint64_t>())!=auxiliary.at("scalar_array"))
                        throw HandlerError("signature_changed","The scalar array metadata changed while reading the signature");
                } else if(auxiliary.contains("enum_array")) {
                    if(capture_enum_array_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>(),captured.at("flags").get<uint64_t>())!=auxiliary.at("enum_array"))
                        throw HandlerError("signature_changed","The enum array metadata changed while reading the signature");
                } else {
                const auto current=capture_object_array_parameter(field,captured.at("size").get<int>(),captured.at("array_dim").get<int>(),captured.at("flags").get<uint64_t>());
                if(current!=auxiliary.at("object_array"))throw HandlerError("signature_changed","Object array metadata changed while reading the signature");
                }
            }
        } else if(type=="BoolProperty") {
            if(read<uint8_t>(field+0x70)!=auxiliary.at("field_size").get<uint8_t>()
               || read<uint8_t>(field+0x71)!=auxiliary.at("byte_offset").get<uint8_t>()
               || read<uint8_t>(field+0x72)!=auxiliary.at("byte_mask").get<uint8_t>()
               || read<uint8_t>(field+0x73)!=auxiliary.at("field_mask").get<uint8_t>())
                throw HandlerError("signature_changed","The BoolProperty mask changed while reading the signature");
        } else if(type=="ObjectProperty" || type=="ClassProperty") {
            const auto declared=read<uintptr_t>(field+0x70);
            if(identity(validate_object(reinterpret_cast<void*>(declared)))!=auxiliary.at("declared"))
                throw HandlerError("signature_changed","The declared argument class changed while reading the signature");
            if(json{{"name",object_name(reinterpret_cast<void*>(declared))},
                    {"path",object_path(reinterpret_cast<void*>(declared))}}!=auxiliary.at("declared_type"))
                throw HandlerError("signature_changed","The declared class name or path changed");
            if(type=="ClassProperty") {
                const auto meta=read<uintptr_t>(field+0x78);
                if(identity(validate_object(reinterpret_cast<void*>(meta)))!=auxiliary.at("meta"))
                    throw HandlerError("signature_changed","The argument metaclass changed while reading the signature");
                if(json{{"name",object_name(reinterpret_cast<void*>(meta))},
                        {"path",object_path(reinterpret_cast<void*>(meta))}}!=auxiliary.at("meta_type"))
                    throw HandlerError("signature_changed","The metaclass name or path changed");
            }
        } else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto declared=enum_layout(type,captured.at("size").get<int>(),field);
            if(declared.underlying_type!=auxiliary.at("underlying_type").get<std::string>() || declared.size!=auxiliary.at("size").get<int>())
                throw HandlerError("signature_changed","The enum layout changed while reading the signature");
            if(type=="EnumProperty") {
                const auto underlying=read<uintptr_t>(field+0x70);const auto& before=auxiliary.at("underlying");
                const auto underlying_class=read<uintptr_t>(underlying+8);
                const json current={{"pointer",underlying},{"field_class",underlying_class},
                    {"type_name",read<uint64_t>(underlying_class)},{"owner",read<uintptr_t>(underlying+0x10)},
                    {"next",read<uintptr_t>(underlying+0x18)},{"name",read<uint64_t>(underlying+0x20)},
                    {"array_dim",read<int32_t>(underlying+0x30)},{"size",read<int32_t>(underlying+0x34)},
                    {"flags",read<uint64_t>(underlying+0x38)},{"offset",read<int32_t>(underlying+0x44)}};
                if(current!=before)throw HandlerError("signature_changed","UnderlyingProp changed while reading the signature");
            }
            if(declared.object) {
                const auto table=enum_table(declared.object);const auto enum_address=reinterpret_cast<uintptr_t>(declared.object);
                json entries=json::array();
                for(const auto& entry:table.entries)
                    entries.push_back({{"name",entry.name},{"label",name(entry.name)},{"value",entry.value}});
                const json current={{"identity",identity(table.identity)},{"path",table.path},{"entries",std::move(entries)},
                    {"data",read<uintptr_t>(enum_address+0x40)},{"count",read<int32_t>(enum_address+0x48)},
                    {"capacity",read<int32_t>(enum_address+0x4c)}};
                if(current!=auxiliary.at("enum"))throw HandlerError("signature_changed","UEnum changed while reading the signature");
            } else if(!auxiliary.at("enum").is_null())throw HandlerError("signature_changed","UEnum disappeared while reading the signature");
        }
    }
    if(resolve(target_handle)!=object)throw HandlerError("signature_changed","The object handle changed while reading the signature");
    return result;
}
json GameAccess::functions(void* object,std::size_t limit,std::size_t offset,const std::string& query,const std::string& target_handle) {
    json items=json::array();auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);
    std::size_t matched{};
    bool truncated=false,scan_truncated=false;std::set<uintptr_t> visited_classes;
    for(;klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!visited_classes.insert(klass).second){scan_truncated=true;break;}
        auto field=read<uintptr_t>(klass+0x48);std::set<uintptr_t> visited_fields;
        for(;field;field=read<uintptr_t>(field+0x28)) {
            if(!visited_fields.insert(field).second){scan_truncated=true;break;}
            const auto fieldclass=read<uintptr_t>(field+0x10);
            if(!contains(object_name(reinterpret_cast<void*>(fieldclass)),"Function"))continue;
            const auto function_name=object_name(reinterpret_cast<void*>(field));
            if(!function_query::name_matches(function_name,query))continue;
            if(matched<offset){++matched;continue;}
            if(items.size()>=limit){truncated=true;break;}
            ++matched;
            json parameters=json::array();auto parameter=read<uintptr_t>(field+0x50);
            std::set<uintptr_t> visited_parameters;bool parameters_truncated=false;
            for(;parameter;parameter=read<uintptr_t>(parameter+0x18)) {
                if(!visited_parameters.insert(parameter).second){parameters_truncated=true;break;}
                const auto pf=read<uint64_t>(parameter+0x38);if(!(pf&0x80))continue;
                const auto parameter_type=field_type(parameter,[this](auto f){return name(f);});
                parameters.push_back({{"name",name(read<uint64_t>(parameter+0x20))},
                    {"type",parameter_type},
                    {"offset",read<int32_t>(parameter+0x44)},{"element_size",read<int32_t>(parameter+0x34)},
                    {"array_dim",read<int32_t>(parameter+0x30)},
                    {"flags",pf},{"return",(pf&0x400)!=0},{"out",(pf&0x100)!=0},
                    {"reference",(pf&kReferenceParm)!=0},{"const",(pf&kConstParm)!=0}});
                if(parameter_type=="BoolProperty")parameters.back()["bool_layout"]={
                    {"field_size",read<uint8_t>(parameter+0x70)},{"byte_offset",read<uint8_t>(parameter+0x71)},
                    {"byte_mask",read<uint8_t>(parameter+0x72)},{"field_mask",read<uint8_t>(parameter+0x73)}};
            }
            json item={{"name",function_name},{"owner",object_name(reinterpret_cast<void*>(klass))},
                {"flags",read<uint32_t>(field+0xb0)},{"parameter_count",read<uint8_t>(field+0xb4)},
                {"parameter_size",read<uint16_t>(field+0xb6)},{"return_offset",read<uint16_t>(field+0xb8)},
                {"parameters",parameters},{"parameters_truncated",parameters_truncated},
                {"signature_token",nullptr},{"signature_status",parameters_truncated?"parameters_truncated":"unsupported_signature"}};
            if(!parameters_truncated)try {
                const auto contract=capture_function_contract(object,klass,field,target_handle);
                require_same_function_contract(contract,capture_function_contract(object,klass,field,target_handle));
                item=contract.at("public_function");
                item["signature_token"]=nullptr;
                item["signature_token"]=store_function_contract(signature_contracts_,next_signature_token_,contract);
                item["signature_status"]="complete";
        }catch(const HandlerError& error){item["signature_status"]=error.code();item["signature_error"]={{"code",error.code()},{"message",error.what()},{"details",error.details()}};}
            if(object_path(reinterpret_cast<void*>(klass))=="/SN2MCPDevelopmentServices/BP_DevelopmentServices.BP_DevelopmentServices_C")
                item["execution_metadata"]=function_execution_metadata(field);
            items.push_back(std::move(item));
            if(parameters_truncated)scan_truncated=true;
        }
        if(truncated || scan_truncated)break;
    }
    return {{"items",items},{"truncated",truncated || scan_truncated},{"scan_truncated",scan_truncated},
            {"offset",offset},{"matching_functions_scanned",matched},
            {"next_offset",truncated&&!scan_truncated?json(matched):json(nullptr)},
            {"offset_kind","matching_inherited_function_ordinal"},
            {"scan_complete",!truncated && !scan_truncated}};
}
json GameAccess::invoke(void* object,const std::string& function_name,const json& arguments,
                        const std::string& target_handle,const json* expected_token) {
    const auto prepared_before=name_constructions_started_.load(),events_before=process_events_started_.load(),returned_before=process_events_returned_.load();
    try {
        auto result=invoke_impl(object,function_name,arguments,target_handle,expected_token);
        if(name_constructions_started_.load()!=prepared_before)result["preparation_effect"]={{"name_pool_may_have_changed",true}};
        return result;
    }catch(const HandlerError& error) {
        auto details=error.details();
        if(name_constructions_started_.load()!=prepared_before)
            details["preparation_effect"]={{"name_pool_may_have_changed",true}};
        if(process_events_started_.load()==events_before) {
            details["executed"]=false;details["may_have_executed"]=false;details["process_event_returned"]=false;
        }else if(process_events_returned_.load()!=returned_before) {
            details["executed"]=true;details["process_event_returned"]=true;
        }
        throw HandlerError(error.code(),error.what(),std::move(details));
    }catch(...) {
        if(name_constructions_started_.load()==prepared_before)throw;
        const bool event_started=process_events_started_.load()!=events_before;
        const bool event_returned=process_events_returned_.load()!=returned_before;
        throw HandlerError("invocation_preparation_error","Invocation failed after FName preparation",
            {{"executed",event_returned},{"process_event_returned",event_returned},{"may_have_executed",event_started},{"do_not_retry",event_started},
             {"preparation_effect",{{"name_pool_may_have_changed",true}}}});
    }
}
json GameAccess::invoke_impl(void* object,const std::string& function_name,const json& arguments,
                       const std::string& target_handle,const json* expected_token) {
    const auto invocation_identity=validate_object(object);
    json expected_contract=nullptr;
    if(expected_token)expected_contract=checked_function_contract(signature_contracts_,*expected_token,handle_session_,target_handle,function_name);
    auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);uintptr_t function=0,function_owner=0;
    std::set<uintptr_t> visited_classes;
    for(;klass && !function;klass=read<uintptr_t>(klass+0x40)) {
        if(!visited_classes.insert(klass).second)throw HandlerError("unsupported_signature","The function owner chain contains a cycle");
        auto field=read<uintptr_t>(klass+0x48);
        std::set<uintptr_t> visited_functions;
        for(;field;field=read<uintptr_t>(field+0x28)) {
            if(!visited_functions.insert(field).second)throw HandlerError("unsupported_signature","The UFunction chain contains a cycle");
            if(object_name(reinterpret_cast<void*>(field))==function_name && contains(object_name(reinterpret_cast<void*>(read<uintptr_t>(field+0x10))),"Function")){function=field;function_owner=klass;break;}
        }
    }
    if(!function)throw HandlerError("function_not_found","Reflected function not found");
    if(expected_token) {
        if(expected_contract.at("function").at("object")!=function || expected_contract.at("owner").at("object")!=function_owner)
            throw HandlerError("signature_token_mismatch","The token belongs to a different implementation of the same function name");
        require_same_function_contract(expected_contract,capture_function_contract(object,function_owner,function,target_handle));
    }
    const auto invocation_evidence_contract=capture_function_contract(object,function_owner,function,target_handle);
    auto bytes=read<uint16_t>(function+0xb6);
    std::vector<uintptr_t> parameter_fields;
    std::set<uintptr_t> visited_fields;
    auto parameter=read<uintptr_t>(function+0x50);
    int extent=bytes;
    for(;parameter;parameter=read<uintptr_t>(parameter+0x18)) {
        if(!visited_fields.insert(parameter).second)
            throw HandlerError("unsupported_signature","The reflected parameter chain contains a cycle");
        const auto flags=read<uint64_t>(parameter+0x38);
        if(!(flags&0x80))continue;
        extent=parameter_extent(extent,read<int32_t>(parameter+0x44),
            read<int32_t>(parameter+0x34),read<int32_t>(parameter+0x30));
        parameter_fields.push_back(parameter);
    }
    aligned_parameter_buffer::Buffer aligned(static_cast<size_t>(extent));auto buffer=aligned.data();
    uintptr_t return_field{};std::string return_type;int return_size{};int return_offset{};size_t input_count{};
    std::vector<std::pair<std::string,void*>> object_arguments;
    struct ClassArgument {std::string handle;void* property_class;void* meta_class;};
    std::vector<ClassArgument> class_arguments;
    struct EnumArgument {uintptr_t field;std::string type;EnumLayout layout;
                         sn2_mcp::enum_values::Integer value;std::optional<Handle> enum_identity;};
    std::vector<EnumArgument> enum_inputs;
    struct NameSource {
        NameArgumentSource source;
        ObjectSerialIdentity object_identity,owner_identity;
        uintptr_t field{},field_next{},field_class{},owner{};
        uint64_t field_name{},packed{};
        int32_t offset{},size{},dim{},owner_size{};
        std::string text;
        bool operator==(const NameSource&) const=default;
    };
    auto capture_name=[this](const NameArgumentSource& source) {
        NameSource result;result.source=source;
        auto object=resolve(source.handle);
        result.object_identity=serial_identity(validate_object(object));
        auto klass=result.object_identity.object_class;
        std::set<uintptr_t> classes,fields;
        for(;klass && !result.field;klass=read<uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)
                throw HandlerError("name_source_scan_incomplete","FName source class chain contains a cycle");
            validate_class(reinterpret_cast<void*>(klass));
            for(auto field=read<uintptr_t>(klass+0x50);field;field=read<uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)
                    throw HandlerError("name_source_scan_incomplete","FName source property chain contains a cycle");
                if(name(read<uint64_t>(field+0x20))==source.property_name){result.field=field;result.owner=klass;break;}
            }
        }
        if(!result.field)throw HandlerError("property_not_found","FName source property not found");
        result.owner_identity=serial_identity(validate_object(reinterpret_cast<void*>(result.owner)));
        result.field_next=read<uintptr_t>(result.field+0x18);result.field_class=read<uintptr_t>(result.field+8);
        result.field_name=read<uint64_t>(result.field+0x20);
        result.offset=read<int32_t>(result.field+0x44);result.size=read<int32_t>(result.field+0x34);
        result.dim=read<int32_t>(result.field+0x30);result.owner_size=read<int32_t>(result.owner+0x58);
        if(field_type(result.field,[this](uint64_t value){return name(value);})!="NameProperty"
           || result.size!=8 || result.dim!=1 || result.offset<0
           || result.owner_size<8 || result.offset>result.owner_size-8)
            throw HandlerError("unsupported_name_source","The source must be an exact 8-byte NameProperty");
        const auto address=reinterpret_cast<uintptr_t>(object)+static_cast<uintptr_t>(result.offset);
        if(address<reinterpret_cast<uintptr_t>(object) || address>UINTPTR_MAX-sizeof(uint64_t))
            throw HandlerError("unsupported_name_source","The FName source address overflows");
        result.packed=read<uint64_t>(address);
        result.text=name(result.packed);
        if(result.packed!=read<uint64_t>(address)
           || result.field_next!=read<uintptr_t>(result.field+0x18)
           || result.field_class!=read<uintptr_t>(result.field+8)
           || result.field_name!=read<uint64_t>(result.field+0x20)
           || result.offset!=read<int32_t>(result.field+0x44) || result.size!=read<int32_t>(result.field+0x34)
           || result.dim!=read<int32_t>(result.field+0x30) || result.owner_size!=read<int32_t>(result.owner+0x58)
           || field_type(result.field,[this](uint64_t value){return name(value);})!="NameProperty"
           || !same_serial_identity(result.owner_identity,serial_identity(validate_object(reinterpret_cast<void*>(result.owner))))
           || !same_serial_identity(result.object_identity,serial_identity(validate_object(object))))
            throw HandlerError("name_source_changed","FName source metadata or identity changed while being copied");
        if(resolve(source.handle)!=object)throw HandlerError("stale_handle","The FName source handle changed");
        return result;
    };
    struct NameInput {uintptr_t field{};int32_t field_offset{},destination_offset{};NameSource source;};
    std::vector<NameInput> name_inputs;
    auto encode_name=[&](uintptr_t field,int32_t field_offset,int32_t destination_offset,const json& value)->uint64_t {
        validate_name_argument_layout(read<int32_t>(field+0x34),read<int32_t>(field+0x30));
        if(value.is_object() && value.contains("name")) {
            return encode_name_text(value);
        } else {
            const auto source=validated_name_argument(read<int32_t>(field+0x34),read<int32_t>(field+0x30),value,capture_name);
            name_inputs.push_back({field,field_offset,destination_offset,source});
            return source.packed;
        }
    };
    auto pack_name=[&](uintptr_t field,int32_t field_offset,int32_t destination_offset,const json& value) {
        const auto packed=encode_name(field,field_offset,destination_offset,value);
        std::memcpy(buffer+destination_offset,&packed,sizeof(packed));
    };
    struct VectorParameter {uintptr_t field{};int offset{},size{};uint64_t flags{};std::string key;vector_layout::Snapshot layout;bool output{},returned{};};
    std::vector<VectorParameter> vector_parameters;
    struct ColorParameter {uintptr_t field{};int offset{};uint64_t flags{};linear_color_layout::Snapshot layout;bool returned{};};
    std::vector<ColorParameter> color_parameters;
    struct ScalarOutput {uintptr_t field{};int offset{};uint64_t flags{};std::string key;scalar_outputs::Layout layout;};
    std::vector<ScalarOutput> scalar_parameters;
    struct TransformReturn {uintptr_t field{};int offset{};transform_layout::Snapshot layout;};
    std::optional<TransformReturn> transform_return;
    std::optional<scalar_struct_values::Snapshot> scalar_struct_return;
    json pod_struct_return=nullptr;
    struct PodStructOutput {uintptr_t field{};int offset{},size{};std::string key;json metadata;};
    std::vector<PodStructOutput> pod_struct_outputs;
    json set_return_metadata=nullptr;
    json array_return_metadata=nullptr;
    json pod_function_contract=nullptr;
    bool contains_vector=false,contains_transform=false,contains_color=false,contains_rotator=false,contains_scalar_struct=false,contains_scalar_output=false,contains_string_input=false,contains_object_array=false,contains_set_return=false;
    bool has_string_return=false,contains_string_output=false,contains_array_return=false,contains_array_output=false;
    for(const auto field:parameter_fields) {
        const auto type=field_type(field,[this](auto f){return name(f);});const auto flags=read<uint64_t>(field+0x38);
        if(type=="StructProperty") {
            const auto structure_path=object_path(reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
            if(structure_path=="/Script/CoreUObject.LinearColor")contains_color=true;
            else if(structure_path=="/Script/CoreUObject.Transform")contains_transform=true;
            else if(structure_path=="/Script/CoreUObject.Vector")contains_vector=true;
            else if(structure_path=="/Script/CoreUObject.Rotator")contains_rotator=true;
            else contains_scalar_struct=true;
        }
        if(supported_scalar_output_type(type) && (flags&0x100) && !(flags&0x400))contains_scalar_output=true;
        if(type=="StrProperty" && !(flags&0x400)) {
            if(vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output) {
                parameter_direction::classify(flags);contains_string_output=true;
            } else contains_string_input=true;
        }
        if(type=="StrProperty" && (flags&0x400))has_string_return=true;
        if(type=="ArrayProperty" && !(flags&0x400)) {
            if((flags&0x100) && !(flags&0x2))contains_array_output=true;
            else contains_object_array=true;
        }
        if(type=="ArrayProperty" && (flags&0x400))contains_array_return=true;
        if(type=="SetProperty" && (flags&0x400))contains_set_return=true;
    }
    const bool contains_container_return=contains_set_return || contains_array_return;
    const bool contains_owned_containers=contains_container_return || contains_array_output;
    if((contains_string_input || contains_string_output || has_string_return || contains_object_array || contains_owned_containers)
       && !bindings_.free.available())
        throw HandlerError("free_binding_unavailable","This signature owns engine values and requires the target free binding",
            {{"executed",false},{"may_have_executed",false}});
    if(contains_object_array && !bindings_.realloc.available())
        throw HandlerError("realloc_binding_unavailable","Array inputs require the target realloc binding",
            {{"executed",false},{"may_have_executed",false}});
    const bool string_array_input=std::any_of(parameter_fields.begin(),parameter_fields.end(),[&](auto field){
        const auto flags=read<std::uint64_t>(field+0x38);
        return !(flags&0x400) && (!((flags&0x100) && !(flags&0x2)))
            && field_type(field,[this](auto value){return name(value);})=="ArrayProperty"
            && field_type(read<std::uintptr_t>(field+0x78),[this](auto value){return name(value);})=="StrProperty";
    });
    if((contains_string_input || string_array_input) && !bindings_.string_construct.available())
        throw HandlerError("string_constructor_unavailable","FString inputs require the target wide string constructor",
            {{"executed",false},{"may_have_executed",false}});
    const bool strict_pod_contract=contains_vector || contains_transform || contains_color || contains_rotator || contains_scalar_struct || contains_scalar_output || contains_string_input || contains_string_output || contains_object_array || contains_owned_containers;
    if(strict_pod_contract) {
        pod_function_contract=capture_function_contract(object,function_owner,function,target_handle);
        std::vector<std::pair<int,int>> intervals;
        for(const auto field:parameter_fields) {
            const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34);
            if(parameter_extent(0,offset,size,read<int32_t>(field+0x30))>extent)
                throw HandlerError("unsupported_signature","The parameter exceeds the allocated UFunction buffer");
            for(const auto& [start,end]:intervals)if(offset<end && start<offset+size)
                throw HandlerError("unsupported_signature","The POD function contains overlapping parameters");
            intervals.emplace_back(offset,offset+size);
        }
    }
    struct ArrayElement {std::string handle;ObjectSerialIdentity identity{};uintptr_t pointer{};};
    struct ObjectArrayInput {uintptr_t field{};int offset{};uint64_t flags{};std::string key;json metadata;std::vector<ArrayElement> elements;ArrayHeader header{};std::vector<uintptr_t> pointers;std::vector<std::byte> scalar_bytes;size_t stride{8};std::vector<std::u16string> string_sources;std::vector<size_t> string_indices;};
    std::vector<ObjectArrayInput> array_inputs;
    struct ObjectArrayOutput {uintptr_t field{};int offset{};json metadata;std::string key;};
    std::vector<ObjectArrayOutput> array_outputs;
    object_array_input_lifetime::Ledger array_ledger;
    bool array_cleanup_done=false;
    CleanupOnce<object_array_input_lifetime::Cleanup> array_cleanup_once;
    std::size_t extra_array_count{},extra_array_bytes{};
    uint32_t array_quarantine_cause{};
    auto quarantine_arrays=[&](uint32_t cause) {
        array_ledger.mark_uncertain();object_array_inputs_quarantine_cause_=cause;
        object_array_inputs_retained_count_=array_ledger.retained_count()+extra_array_count;
        object_array_inputs_retained_bytes_=array_ledger.retained_bytes()+extra_array_bytes;
        object_array_inputs_quarantined_=true;
    };
    auto release_arrays=[&] {
        return array_cleanup_once.run([&]() noexcept -> object_array_input_lifetime::Cleanup {
            const auto cleanup=array_ledger.release([this](uintptr_t memory){return free_string(base_+kFree,reinterpret_cast<void*>(memory));});
            array_cleanup_done=cleanup.confirmed;
            if(!cleanup.confirmed)quarantine_arrays(array_quarantine_cause?array_quarantine_cause:5);
            return cleanup;
        });
    };
    if(contains_object_array) {
        if(object_array_inputs_quarantined_)throw HandlerError("object_array_input_quarantined","Object array inputs are blocked after unconfirmed cleanup");
        object_array_input_lifetime::InputBudget budget;
        array_inputs.reserve(parameter_fields.size());
        for(const auto field:parameter_fields) {
            const auto flags=read<uint64_t>(field+0x38);
            if((flags&0x400) || ((flags&0x100) && !(flags&0x2)) || field_type(field,[this](auto value){return name(value);})!="ArrayProperty")continue;
            const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});
            const auto metadata=inner_type=="ObjectProperty"?capture_object_array_parameter(field,read<int32_t>(field+0x34),read<int32_t>(field+0x30),flags)
                :(supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="StructProperty")?capture_scalar_array_parameter(field,read<int32_t>(field+0x34),read<int32_t>(field+0x30),flags)
                :capture_enum_array_parameter(field,read<int32_t>(field+0x34),read<int32_t>(field+0x30),flags);
            const auto key=name(read<uint64_t>(field+0x20));
            if(!arguments.contains(key))throw HandlerError("missing_argument","Object array argument is missing: "+key);
            const auto& values=arguments.at(key);
            if(!values.is_array())
                throw HandlerError("invalid_argument","Object array requires a JSON array");
            const bool enum_array=metadata.contains("enum");
            const bool scalar_array=metadata.contains("scalar_type");
            const auto stride=enum_array?size_t{1}:scalar_array?metadata.at("inner").at("element_size").get<size_t>():size_t{8};
            budget.add(values.size(),stride);
            ObjectArrayInput input{field,read<int32_t>(field+0x44),flags,key,metadata,{},{},{}};
            if(enum_array || scalar_array) {
                input.stride=stride;input.scalar_bytes.resize(values.size()*input.stride);
                json structure=nullptr;
                if(scalar_array && metadata.at("scalar_type")=="StructProperty") {
                    structure=capture_pod_struct_parameter(metadata.at("inner").at("pointer").get<uintptr_t>(),static_cast<int>(stride),1);
                    if(structure!=metadata.at("struct"))
                        throw HandlerError("signature_changed","The input DTO array metadata changed while encoding its values");
                }
                size_t index{};
                for(const auto& value:values) {
                    if(scalar_array && metadata.at("scalar_type")=="StrProperty") {
                        input.string_sources.push_back(string_arguments::encode(value));
                    } else if(enum_array) {
                        const auto encoded=enum_arguments::encode_integer("ByteProperty",1,value);
                        input.scalar_bytes[index]=encoded.bytes[0];
                    } else if(metadata.at("scalar_type")=="NameProperty") {
                        const auto encoded=encode_name(metadata.at("inner").at("pointer").get<uintptr_t>(),0,0,value);
                        std::memcpy(input.scalar_bytes.data()+index*input.stride,&encoded,sizeof(encoded));
                    } else if(!structure.is_null()) {
                        const auto encoded=encode_pod_struct(structure,value);
                        std::memcpy(input.scalar_bytes.data()+index*input.stride,encoded.data(),encoded.size());
                    } else {
                        const auto& scalar_type=metadata.at("scalar_type");
                        const auto write=[&](auto encoded) {
                            std::memcpy(input.scalar_bytes.data()+index*input.stride,&encoded,sizeof(encoded));
                        };
                        if(scalar_type=="BoolProperty") {
                            if(!value.is_boolean())throw HandlerError("invalid_argument","A boolean array element is required");
                            const auto mask=metadata.at("inner").at("bool_layout").at("byte_mask").get<uint8_t>();
                            write(value.get<bool>()?mask:uint8_t{});
                        } else if(scalar_type=="Int64Property")write(int64_argument(value));
                        else if(scalar_type=="UInt32Property")write(uint32_argument(value));
                        else if(scalar_type=="UInt64Property")write(uint64_argument(value));
                        else if(scalar_type=="FloatProperty")write(float_argument(value));
                        else if(scalar_type=="DoubleProperty")write(double_argument(value));
                        else write(int32_argument(value));
                    }
                    ++index;
                }
                array_inputs.push_back(std::move(input));continue;
            }
            input.elements.reserve(values.size());
            input.pointers.resize(values.size());
            const auto declared=reinterpret_cast<void*>(metadata.at("property_class").get<uintptr_t>());
            for(const auto& value:values) {
                auto handle=object_argument_handle(value);ArrayElement element{handle,{},0};
                if(!handle.empty()) {
                    auto object_value=resolve(handle);
                    if(!is_compatible_object(object_value,declared))
                        throw HandlerError("incompatible_object_argument","The array element is incompatible with PropertyClass");
                    element.identity=serial_identity(validate_object(object_value));element.pointer=reinterpret_cast<uintptr_t>(object_value);
                }
                input.elements.push_back(std::move(element));
            }
            array_inputs.push_back(std::move(input));
        }
        array_ledger.reserve_inputs(array_inputs.size());
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
    }
    auto validate_array_elements=[&] {
        for(const auto& input:array_inputs) {
            if(input.metadata.contains("enum") || input.metadata.contains("scalar_type"))continue;
            const auto declared=reinterpret_cast<void*>(input.metadata.at("property_class").get<uintptr_t>());
            for(const auto& element:input.elements)if(!element.handle.empty()) {
                auto current=resolve(element.handle);
                if(reinterpret_cast<uintptr_t>(current)!=element.pointer
                   || !same_serial_identity(element.identity,serial_identity(validate_object(current)))
                   || !is_compatible_object(current,declared))
                    throw HandlerError("stale_handle","Object array element identity changed before the call");
            }
        }
    };
    auto validate_array_inputs=[&] {
        if(!contains_object_array)return;
        for(size_t index=0;index<array_inputs.size();++index) {
            auto& input=array_inputs[index];ArrayHeader current{};
            if(!copy_memory(&current,buffer+input.offset,sizeof(current))) {
                array_ledger.mark_uncertain();array_quarantine_cause=3;
                throw HandlerError("array_lifetime_unknown","The Object array header is unavailable");
            }
            array_ledger.validate_unchanged_input(index,current);
            if(input.metadata.contains("enum") || input.metadata.contains("scalar_type")) {
                std::vector<std::byte> copied(input.scalar_bytes.size());
                if((current.count && !copy_memory(copied.data(),reinterpret_cast<void*>(current.data),copied.size())) || copied!=input.scalar_bytes) {
                    array_ledger.mark_uncertain();array_quarantine_cause=3;
                    throw HandlerError("array_lifetime_unknown","The scalar array values changed during the call");
                }
                continue;
            }
            if(current.count && !copy_memory(input.pointers.data(),reinterpret_cast<void*>(current.data),input.elements.size()*sizeof(uintptr_t))) {
                array_ledger.mark_uncertain();array_quarantine_cause=3;
                throw HandlerError("array_lifetime_unknown","Object array bytes are unavailable");
            }
            for(size_t item=0;item<input.elements.size();++item)if(input.pointers[item]!=input.elements[item].pointer) {
                array_ledger.mark_uncertain();array_quarantine_cause=3;
                throw HandlerError("array_lifetime_unknown","The const Object array pointers changed");
            }
        }
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
    };
    struct StringInput {uintptr_t field{};int offset{};uint64_t flags{};std::string key;std::u16string source;StringHeader header{};};
    std::vector<StringInput> string_inputs;
    struct StringOutput {uintptr_t field{};int offset{};uint64_t flags{};std::string key;StringHeader header{};};
    std::vector<StringOutput> string_outputs;
    string_input_lifetime::Ledger string_ledger;
    bool string_event_started=false,string_event_completed=false,string_return_registered=false,string_cleanup_done=false;
    bool string_outputs_registered=false;
    auto register_string_outputs=[&]() {
        if(string_outputs_registered)return;
        try {
            for(auto& output:string_outputs) {
                if(!copy_memory(&output.header,buffer+output.offset,sizeof(output.header)))
                    throw HandlerError("invalid_string_storage","The FString output header is unavailable");
                string_ledger.add_owned_range(std::span<const StringHeader>(&output.header,1));
            }
        } catch(...) {string_ledger.mark_uncertain();throw;}
        string_outputs_registered=true;
    };
    std::function<void()> validate_string_inputs;
    const bool contains_string_array_input=std::any_of(array_inputs.begin(),array_inputs.end(),[](const auto& input){return input.metadata.value("scalar_type",std::string{})=="StrProperty";});
    bool string_array_results_registered=false;
    auto register_string_array_results=[&]() {
        if(string_array_results_registered)return;
        auto collect=[&](int offset,const json& metadata) {
            if(metadata.value("scalar_type",std::string{})!="StrProperty")return;
            const auto header=read<ArrayHeader>(reinterpret_cast<uintptr_t>(buffer+offset));
            const auto page=array_page(header,16,1,0,std::numeric_limits<size_t>::max());
            std::vector<StringHeader> strings(static_cast<size_t>(page.count));
            if(page.bytes && !copy_memory(strings.data(),reinterpret_cast<void*>(page.begin),page.bytes))
                throw HandlerError("invalid_memory","The returned FString array headers are unavailable");
            string_ledger.add_owned_range(strings);
        };
        if(contains_array_return)collect(return_offset,array_return_metadata);
        for(const auto& output:array_outputs)collect(output.offset,output.metadata);
        string_array_results_registered=true;
    };
    bool container_return_cleanup_done=false,container_return_cleanup_confirmed=false;
    auto release_containers=[&]() {
        if(!contains_owned_containers)return true;
        if(container_return_cleanup_done)return container_return_cleanup_confirmed;
        container_return_cleanup_done=true;
        try {
            if(validate_string_inputs)validate_string_inputs();
            register_string_outputs();
            if(contains_string_output && has_string_return && !string_return_registered && string_event_completed) {
                const auto header=read<StringHeader>(reinterpret_cast<uintptr_t>(buffer+return_offset));
                string_ledger.add_return(header);string_return_registered=true;
            }
            register_string_array_results();
            const auto strings=string_ledger.release([this](uintptr_t memory){return free_string(base_+kFree,reinterpret_cast<void*>(memory));});
            if(!strings.confirmed){container_return_cleanup_confirmed=false;return false;}
            string_cleanup_done=true;
        } catch(...) {string_ledger.mark_uncertain();container_return_cleanup_confirmed=false;return false;}
        std::set<uintptr_t> allocations;
        bool confirmed=true;
        auto collect=[&](int offset,bool set_return) {
            std::array<std::byte,80> header{};
            const auto header_size=set_return?header.size():size_t{16};
            if(!copy_memory(header.data(),buffer+offset,header_size)){confirmed=false;return;}
            for(const auto pointer_offset:{size_t{0},size_t{32},size_t{64}}) {
                uintptr_t pointer{};std::memcpy(&pointer,header.data()+pointer_offset,sizeof(pointer));
                if(pointer)allocations.insert(pointer);
            }
        };
        if(contains_container_return)collect(return_offset,contains_set_return);
        for(const auto& output:array_outputs)collect(output.offset,false);
        for(const auto allocation:allocations)
            if(!free_string(base_+kFree,reinterpret_cast<void*>(allocation)))confirmed=false;
        if(contains_container_return)std::memset(buffer+return_offset,0,contains_set_return?size_t{80}:size_t{16});
        for(const auto& output:array_outputs)std::memset(buffer+output.offset,0,16);
        container_return_cleanup_confirmed=confirmed;
        return container_return_cleanup_confirmed;
    };
    auto record_container_cleanup=[&](json& details) {
        if(!contains_owned_containers)return;
        const bool confirmed=!string_event_started || (string_event_completed && container_return_cleanup_confirmed);
        if(contains_container_return)details["return_cleanup_confirmed"]=confirmed;
        if(contains_array_output)details["array_output_cleanup_confirmed"]=confirmed;
        details["cleanup_confirmed"]=details.value("cleanup_confirmed",true) && confirmed;
        details["cleanup_state"]=details["cleanup_confirmed"].get<bool>()?"released":"unknown";
    };
    CleanupOnce<string_input_lifetime::Cleanup> string_cleanup_once;
    std::size_t extra_retained_count{},extra_retained_bytes{};
    uint32_t string_quarantine_cause{};
    auto quarantine_strings=[&](uint32_t cause) {
        string_ledger.mark_uncertain();
        string_inputs_quarantine_cause_=cause;
        string_inputs_retained_count_=string_ledger.retained_count()+extra_retained_count;
        string_inputs_retained_bytes_=string_ledger.retained_bytes()+extra_retained_bytes;
        string_inputs_quarantined_=true;
    };
    auto release_strings=[&] {
        return string_cleanup_once.run([&]() noexcept -> string_input_lifetime::Cleanup {
            const auto cleanup=string_ledger.release([this](uintptr_t memory){return free_string(base_+kFree,reinterpret_cast<void*>(memory));});
            string_cleanup_done=cleanup.confirmed;
            if(!cleanup.confirmed)quarantine_strings(string_quarantine_cause?string_quarantine_cause:5);
            return cleanup;
        });
    };
    if(contains_string_input || contains_string_output) {
        if(string_inputs_quarantined_)throw HandlerError("string_input_quarantined","FString inputs are blocked after unconfirmed cleanup");
        string_input_lifetime::InputBudget budget;
        string_inputs.reserve(parameter_fields.size());
        for(const auto field:parameter_fields) {
            const auto flags=read<uint64_t>(field+0x38);
            if((flags&0x400) || field_type(field,[this](auto value){return name(value);})!="StrProperty")continue;
            if(vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output)continue;
            const auto offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34),dim=read<int32_t>(field+0x30);
            if(size!=16 || dim!=1 || offset%8
               || vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                throw HandlerError("unsupported_signature","FString requires aligned value or const reference input of 16 bytes");
            const auto key=name(read<uint64_t>(field+0x20));
            if(!arguments.contains(key))throw HandlerError("missing_argument","FString argument is missing: "+key);
            auto source=string_arguments::encode(arguments.at(key));budget.add(source.size());
            string_inputs.push_back({field,offset,flags,key,std::move(source),{}});
        }
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
    }
    size_t prepared_string_count=string_inputs.size();
    for(const auto& input:array_inputs)prepared_string_count+=input.string_sources.size();
    string_ledger.prepare(prepared_string_count,parameter_fields.size()+1);
    validate_string_inputs=[&] {
        if(string_cleanup_done)return;
        if(!contains_string_input && !contains_string_array_input)return;
        for(size_t index=0;index<string_inputs.size();++index) {
            const auto& input=string_inputs[index];StringHeader current{};
            if(!copy_memory(&current,buffer+input.offset,sizeof(current))) {
                string_ledger.mark_uncertain();string_quarantine_cause=3;
                throw HandlerError("string_lifetime_unknown","The FString input header is unavailable");
            }
            string_ledger.validate_unchanged_input(index,current);
            std::u16string copied(input.source.size()+1,u'\0');
            if(current.data && (!copy_memory(copied.data(),reinterpret_cast<void*>(current.data),copied.size()*sizeof(char16_t))
               || copied.back()!=0 || !std::equal(input.source.begin(),input.source.end(),copied.begin()))) {
                string_ledger.mark_uncertain();string_quarantine_cause=3;
                throw HandlerError("string_lifetime_unknown","Const FString input bytes changed or are unavailable");
            }
        }
        for(const auto& input:array_inputs) {
            for(size_t index=0;index<input.string_sources.size();++index) {
                StringHeader header{};std::memcpy(&header,input.scalar_bytes.data()+index*16,sizeof(header));
                string_ledger.validate_unchanged_input(input.string_indices[index],header);
                if(read_copied_string(header)!=utf8(reinterpret_cast<const wchar_t*>(input.string_sources[index].data()),static_cast<int>(input.string_sources[index].size())))
                    throw HandlerError("string_lifetime_unknown","The input FString array text changed during the call");
            }
        }
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
    };
    auto release_mixed_strings=[&](json& details) {
        if(!contains_string_input && !contains_string_array_input && !has_string_return && !contains_string_output)return;
        if(string_event_completed && (contains_string_input || contains_string_array_input) && !string_cleanup_done && !string_ledger.uncertain()) {
            try{validate_string_inputs();}catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;}
        }
        if(string_event_completed && has_string_return && !string_cleanup_done && !string_return_registered && !string_ledger.uncertain()) {
            StringHeader header{};
            try {
                if(!copy_memory(&header,buffer+return_offset,sizeof(header)))throw HandlerError("invalid_string_storage","The FString result is unavailable");
                string_ledger.add_return(header);string_return_registered=true;
            }catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=4;}
        }
        if(string_event_completed && contains_string_output && !string_cleanup_done && !string_outputs_registered && !string_ledger.uncertain()) {
            try{register_string_outputs();}catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=4;}
        }
        const auto cleanup=release_strings();
        if(contains_string_input)details["input_cleanup_confirmed"]=cleanup.confirmed;
        const bool return_confirmed=!has_string_return || !string_event_started || (string_return_registered && cleanup.confirmed);
        if(has_string_return)details["return_cleanup_confirmed"]=return_confirmed;
        const bool output_confirmed=!contains_string_output || !string_event_started || (string_outputs_registered && cleanup.confirmed);
        if(contains_string_output)details["string_output_cleanup_confirmed"]=output_confirmed;
        details["cleanup_confirmed"]=details.value("cleanup_confirmed",true) && cleanup.confirmed && return_confirmed && output_confirmed;
        details["cleanup_attempted"]=details.value("cleanup_attempted",false) || cleanup.attempted;
        details["cleanup_state"]=details["cleanup_confirmed"].get<bool>()?"released":"unknown";
        details["do_not_retry"]=details.value("do_not_retry",false) || !details["cleanup_confirmed"].get<bool>();
        details["string_input_quarantined"]=string_inputs_quarantined_.load();
    };
    try {
    for(const auto field:parameter_fields) {
        auto flags=read<uint64_t>(field+0x38);if(!(flags&0x80))continue;
        auto type=field_type(field,[this](auto f){return name(f);});int offset=read<int32_t>(field+0x44),size=read<int32_t>(field+0x34);
        const auto current_extent=parameter_extent(extent,offset,size,read<int32_t>(field+0x30));
        if(current_extent>extent)
            throw HandlerError("unsupported_signature","The parameter changed and exceeds the allocated UFunction buffer");
        if(flags&0x400){
            if(return_field)throw HandlerError("unsupported_signature","The function has multiple return parameters");
            return_field=field;return_type=type;return_size=size;return_offset=offset;
            if(supported_scalar_output_type(type) && (flags&(kReferenceParm|kConstParm)))
                throw HandlerError("unsupported_signature","Const or reference scalar returns are unsupported");
            if(type=="StructProperty") {
                const auto structure_path=object_path(reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
                if(structure_path=="/Script/CoreUObject.LinearColor") {
                    if(!contains_color)throw HandlerError("signature_changed","The FLinearColor return appeared after reading the complete signature");
                    if(flags&(kConstParm|kReferenceParm))throw HandlerError("unsupported_signature","Const or reference FLinearColor returns are unsupported");
                    const auto color=capture_linear_color_parameter(field,size,read<int32_t>(field+0x30));
                    color_parameters.push_back({field,offset,flags,color,true});
                } else if(structure_path=="/Script/CoreUObject.Transform") {
                    if(!contains_transform)throw HandlerError("signature_changed","The Transform return appeared after reading the complete signature");
                    if(flags&(kConstParm|kReferenceParm))throw HandlerError("unsupported_signature","Const or reference Transform returns are unsupported");
                    const auto transform=capture_transform_parameter(field,size,read<int32_t>(field+0x30));
                    if(offset%transform.layout.min_alignment
                       || reinterpret_cast<uintptr_t>(buffer+offset)%static_cast<uintptr_t>(transform.layout.min_alignment))
                        throw HandlerError("unsupported_transform_layout","The Transform buffer does not match its reflected alignment");
                    transform_return=TransformReturn{field,offset,transform};
                } else if(structure_path=="/Script/CoreUObject.Vector") {
                    if(!contains_vector)throw HandlerError("signature_changed","The FVector return appeared after reading the complete signature");
                    if(flags&(kReferenceParm|kConstParm))
                        throw HandlerError("unsupported_signature","Const or reference FVector returns are unsupported");
                    const auto layout=capture_vector_parameter(field,size,read<int32_t>(field+0x30));
                    vector_parameters.push_back({field,offset,size,flags,name(read<uint64_t>(field+0x20)),layout,false,true});
                } else if(structure_path=="/Script/CoreUObject.Rotator") {
                    if(!contains_rotator)throw HandlerError("signature_changed","The FRotator return appeared after reading the complete signature");
                    if(flags&(kConstParm|kReferenceParm))throw HandlerError("unsupported_signature","Const or reference FRotator returns are unsupported");
                    auto rotator=capture_scalar_struct_parameter(field,size,read<int32_t>(field+0x30));
                    rotator_arguments::validate(rotator);
                    if(offset%rotator.layout.min_alignment
                       || reinterpret_cast<uintptr_t>(buffer+offset)%static_cast<uintptr_t>(rotator.layout.min_alignment))
                        throw HandlerError("unsupported_rotator_layout","The FRotator buffer does not match its reflected alignment");
                    scalar_struct_return=std::move(rotator);
                } else {
                    if(!contains_scalar_struct)throw HandlerError("signature_changed","The scalar DTO return appeared after reading the complete signature");
                    if(flags&(kConstParm|kReferenceParm))throw HandlerError("unsupported_signature","Const or reference DTO returns are unsupported");
                    auto scalar_struct=capture_pod_struct_parameter(field,size,read<int32_t>(field+0x30));
                    const auto alignment=scalar_struct.at("min_alignment").get<uintptr_t>();
                    if(offset%alignment || reinterpret_cast<uintptr_t>(buffer+offset)%alignment)
                        throw HandlerError("unsupported_scalar_struct_layout","The DTO buffer does not match its reflected alignment");
                    pod_struct_return=std::move(scalar_struct);
                }
                continue;
            }
            if(type=="SetProperty") {
                set_return_metadata=capture_object_set_return(field,size,read<int32_t>(field+0x30));
                continue;
            }
            if(type=="ArrayProperty") {
                const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});
                const bool scalar_array=supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="EnumProperty" || inner_type=="ByteProperty" || inner_type=="StructProperty";
                array_return_metadata=scalar_array
                    ?capture_scalar_array_return(field,size,read<int32_t>(field+0x30))
                    :capture_object_array_return(field,size,read<int32_t>(field+0x30));
                continue;
            }
            if(type=="NameProperty")validate_name_return_layout(size,read<int32_t>(field+0x30));
            else if(type=="EnumProperty" || type=="ByteProperty")enum_layout(type,size,field);
            else if(type=="ObjectProperty" || type=="ClassProperty") {
                if(size!=8)throw HandlerError("unsupported_signature","The object return size is unsupported");
                validate_class(reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
                if(type=="ClassProperty")validate_class(reinterpret_cast<void*>(read<uintptr_t>(field+0x78)));
            } else if((type=="StrProperty" && size!=16)
               || (type!="StrProperty" && property_value(reinterpret_cast<uintptr_t>(buffer+offset),type,size,field).is_null()))
                throw HandlerError("unsupported_signature","The result type is unsupported");
            continue;
        }
        if(type=="StructProperty") {
            const auto structure_path=object_path(reinterpret_cast<void*>(read<uintptr_t>(field+0x70)));
            if(structure_path=="/Script/GameplayTags.GameplayTag") {
                if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","GameplayTag requires input by value or const reference");
                const auto tag=capture_gameplay_tag_parameter(field,size,read<int32_t>(field+0x30));
                if(offset%tag.layout.min_alignment)
                    throw HandlerError("unsupported_gameplay_tag_layout","The GameplayTag offset does not match its reflected alignment");
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","GameplayTag argument is missing: "+key);
                const auto& value=arguments.at(key);
                if(!value.is_object() || value.size()!=1 || !value.contains("TagName"))
                    throw HandlerError("invalid_argument","GameplayTag requires only {TagName:FName_argument}");
                const auto& tag_name=tag.components.front();
                pack_name(tag_name.pointer,tag_name.component.offset,offset+tag_name.component.offset,value.at("TagName"));
                ++input_count;continue;
            }
            if(structure_path=="/Script/CoreUObject.Rotator") {
                if(!contains_rotator)throw HandlerError("signature_changed","The FRotator parameter appeared after reading the complete signature");
                if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","FRotator requires input by value or const reference; mutable Out is unsupported");
                const auto rotator=capture_scalar_struct_parameter(field,size,read<int32_t>(field+0x30));
                rotator_arguments::validate(rotator);
                if(offset%rotator.layout.min_alignment
                   || reinterpret_cast<uintptr_t>(buffer+offset)%static_cast<uintptr_t>(rotator.layout.min_alignment))
                    throw HandlerError("unsupported_rotator_layout","The FRotator buffer does not match its reflected alignment");
                const auto key=name(read<uint64_t>(field+0x20));const auto found=arguments.find(key);
                if(found==arguments.end())throw HandlerError("invalid_arguments","FRotator argument is missing: "+key);
                if(!found->is_object() || found->size()!=1 || !found->contains("rotator"))
                    throw HandlerError("invalid_argument","FRotator requires only {rotator:{Pitch,Yaw,Roll}}");
                const auto encoded=rotator_arguments::encode(rotator.layout,found->at("rotator"));
                std::memcpy(buffer+offset,encoded.data(),encoded.size());
                ++input_count;continue;
            }
            if(structure_path=="/Script/CoreUObject.LinearColor") {
                if(!contains_color)throw HandlerError("signature_changed","The FLinearColor input appeared after reading the complete signature");
                if(flags&(kConstParm|kReferenceParm|0x100))throw HandlerError("unsupported_signature","FLinearColor input is supported only by value");
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","FLinearColor argument is missing: "+key);
                const auto color=capture_linear_color_parameter(field,size,read<int32_t>(field+0x30));
                const auto encoded=linear_color_arguments::encode(color.layout,arguments.at(key));
                std::memcpy(buffer+offset,encoded.bytes.data(),static_cast<size_t>(encoded.size));
                color_parameters.push_back({field,offset,flags,color,false});++input_count;continue;
            } else if(structure_path=="/Script/CoreUObject.Transform") {
                if(!contains_transform)throw HandlerError("signature_changed","Transform appeared after reading the complete signature");
                if(vector_parameter_direction::classify(flags)!=vector_parameter_direction::Direction::input)
                    throw HandlerError("unsupported_signature","Transform requires input by value or const reference; Out and Return are unsupported");
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","Transform argument is missing: "+key);
                const auto transform=capture_transform_parameter(field,size,read<int32_t>(field+0x30));
                if(offset%transform.layout.min_alignment
                   || reinterpret_cast<uintptr_t>(buffer+offset)%static_cast<uintptr_t>(transform.layout.min_alignment))
                    throw HandlerError("unsupported_transform_layout","The Transform buffer does not match its reflected alignment");
                const auto encoded=transform_arguments::encode(transform.layout,arguments.at(key));
                std::memcpy(buffer+offset,encoded.bytes.data(),static_cast<size_t>(encoded.size));
                ++input_count;continue;
            }
            if(structure_path!="/Script/CoreUObject.Vector") {
                if(!contains_scalar_struct)throw HandlerError("signature_changed","The scalar DTO appeared after reading the complete signature");
                const bool output=vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output;
                if(!output && (flags&(kConstParm|kReferenceParm|0x100)))
                    throw HandlerError("unsupported_signature","DTO requires input by value or pure output");
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","Scalar DTO argument is missing: "+key);
                const auto scalar_struct=capture_pod_struct_parameter(field,size,read<int32_t>(field+0x30));
                const auto alignment=scalar_struct.at("min_alignment").get<uintptr_t>();
                if(offset%alignment || reinterpret_cast<uintptr_t>(buffer+offset)%alignment)
                    throw HandlerError("unsupported_scalar_struct_layout","The DTO buffer does not match its reflected alignment");
                if(output) {
                    vector_parameter_direction::validate_output_marker(arguments.at(key));
                    std::memset(buffer+offset,0,static_cast<size_t>(size));
                    pod_struct_outputs.push_back({field,offset,size,key,scalar_struct});
                } else {
                    const auto encoded=encode_pod_struct(scalar_struct,arguments.at(key));
                    std::memcpy(buffer+offset,encoded.data(),encoded.size());
                }
                ++input_count;continue;
            }
            if(!contains_vector)throw HandlerError("signature_changed","The FVector parameter appeared after reading the complete signature");
            const auto direction=vector_parameter_direction::classify(flags);
            const auto key=name(read<uint64_t>(field+0x20));
            if(!arguments.contains(key))throw HandlerError("missing_argument","FVector argument is missing: "+key);
            const auto layout=capture_vector_parameter(field,size,read<int32_t>(field+0x30));
            if(direction==vector_parameter_direction::Direction::output) {
                vector_parameter_direction::validate_output_marker(arguments.at(key));
                std::memset(buffer+offset,0,static_cast<size_t>(size));
            } else {
                const auto encoded=vector_arguments::encode(layout.layout,arguments.at(key));
                std::memcpy(buffer+offset,encoded.bytes.data(),static_cast<size_t>(encoded.size));
            }
            ++input_count;
            vector_parameters.push_back({field,offset,size,flags,key,layout,direction==vector_parameter_direction::Direction::output,false});
            continue;
        }
        if(type=="StrProperty") {
            if(vector_parameter_direction::classify(flags)==vector_parameter_direction::Direction::output) {
                if(!contains_string_output)throw HandlerError("signature_changed","The FString output appeared after reading the complete signature");
                parameter_direction::classify(flags);
                if(size!=16 || read<int32_t>(field+0x30)!=1 || offset%8)
                    throw HandlerError("unsupported_signature","FString output requires aligned 16-byte pure output");
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","FString output argument is missing: "+key);
                parameter_direction::validate_output_marker(arguments.at(key));
                std::memset(buffer+offset,0,sizeof(StringHeader));
                string_outputs.push_back({field,offset,flags,key,{}});++input_count;continue;
            }
            if(!contains_string_input)throw HandlerError("signature_changed","The FString input appeared after reading the complete signature");
            auto input=std::find_if(string_inputs.begin(),string_inputs.end(),[field](const auto& value){return value.field==field;});
            if(input==string_inputs.end() || input->offset!=offset || input->flags!=flags || size!=16 || offset%8)
                throw HandlerError("signature_changed","The FString input changed after prevalidation");
            ++input_count;continue;
        }
        if(type=="ArrayProperty") {
            if((flags&0x100) && !(flags&0x2)) {
                if(!contains_array_output)throw HandlerError("signature_changed","The array output appeared after reading the complete signature");
                const auto inner_type=field_type(read<uintptr_t>(field+0x78),[this](auto value){return name(value);});
                const bool scalar_array=supported_scalar_output_type(inner_type) || inner_type=="NameProperty" || inner_type=="StrProperty" || inner_type=="EnumProperty" || inner_type=="ByteProperty" || inner_type=="StructProperty";
                const auto metadata=scalar_array?capture_scalar_array_return(field,size,read<int32_t>(field+0x30),true)
                    :capture_object_array_return(field,size,read<int32_t>(field+0x30),true);
                const auto key=name(read<uint64_t>(field+0x20));
                if(!arguments.contains(key))throw HandlerError("missing_argument","Array output argument is missing: "+key);
                parameter_direction::validate_output_marker(arguments.at(key));
                if(offset%8)throw HandlerError("unsupported_signature","The array output offset is not aligned");
                std::memset(buffer+offset,0,16);
                array_outputs.push_back({field,offset,metadata,key});
                ++input_count;continue;
            }
            if(!contains_object_array)throw HandlerError("signature_changed","Object array appeared after reading the complete signature");
            auto input=std::find_if(array_inputs.begin(),array_inputs.end(),[field](const auto& value){return value.field==field;});
            if(input==array_inputs.end() || input->offset!=offset || input->flags!=flags || size!=16 || offset%8)
                throw HandlerError("signature_changed","The Object array input changed after prevalidation");
            ++input_count;continue;
        }
        if(supported_scalar_output_type(type) && (flags&0x100)) {
            if(!contains_scalar_output)throw HandlerError("signature_changed","The scalar output appeared after reading the complete signature");
            if(parameter_direction::classify(flags)!=parameter_direction::Direction::output)
                throw HandlerError("unsupported_signature","The scalar parameter is not a pure output");
            const auto key=name(read<uint64_t>(field+0x20));
            if(!arguments.contains(key))throw HandlerError("missing_argument","Scalar output argument is missing: "+key);
            parameter_direction::validate_output_marker(arguments.at(key));
            const auto layout=capture_scalar_output_layout(field,type,size,read<int32_t>(field+0x30));
            std::memset(buffer+offset,0,static_cast<size_t>(size));
            scalar_parameters.push_back({field,offset,flags,key,layout});
            ++input_count;continue;
        }
        const bool const_name_input_reference=type=="NameProperty" && (flags&kConstParm) && (flags&(0x100|kReferenceParm)) && !(flags&0x400);
        if((flags&(0x100|kReferenceParm)) && !const_name_input_reference)
            throw HandlerError("unsupported_signature","Out or reference arguments of this type are unsupported");
        if(type=="NameProperty")validate_name_argument_layout(size,read<int32_t>(field+0x30));
        auto key=name(read<uint64_t>(field+0x20));if(!arguments.contains(key))throw HandlerError("missing_argument","Argument is missing: "+key);auto v=arguments.at(key);++input_count;
        if(type=="NameProperty")pack_name(field,offset,offset,v);
        else if(type=="BoolProperty" && v.is_boolean()){auto bo=read<uint8_t>(field+0x71),mask=read<uint8_t>(field+0x73);validate_bool_layout(size,bo,mask);buffer[offset+bo]=v.get<bool>()?mask:0;}
        else if(type=="IntProperty" && size==4){*reinterpret_cast<int32_t*>(buffer+offset)=int32_argument(v);}
        else if(type=="Int64Property" && size==8){const auto integer=int64_argument(v);std::memcpy(buffer+offset,&integer,sizeof(integer));}
        else if(type=="UInt64Property" && size==8){const auto integer=uint64_argument(v);std::memcpy(buffer+offset,&integer,sizeof(integer));}
        else if(type=="UInt32Property" && size==4){const auto integer=uint32_argument(v);std::memcpy(buffer+offset,&integer,sizeof(integer));}
        else if(type=="FloatProperty" && size==4){*reinterpret_cast<float*>(buffer+offset)=float_argument(v);}
        else if(type=="DoubleProperty" && size==8){*reinterpret_cast<double*>(buffer+offset)=double_argument(v);}
        else if(type=="EnumProperty" || type=="ByteProperty") {
            const auto declared=enum_layout(type,size,field);
            const auto encoded=enum_arguments::encode_integer(declared.underlying_type,size,v);
            std::optional<Handle> identity;
            if(type=="EnumProperty" || declared.object) {
                const auto table=enum_table(declared.object);
                identity=table.identity;
            }
            std::memcpy(buffer+offset,encoded.bytes.data(),encoded.size);
            enum_inputs.push_back({field,type,declared,encoded.value,identity});
        }
        else if(type=="ObjectProperty" && size==8) {
            auto expected=reinterpret_cast<void*>(read<uintptr_t>(field+0x70));
            validate_class(expected);
            const auto handle=object_argument_handle(v);
            void* argument=nullptr;
            if(!handle.empty()) {
                argument=resolve(handle);
                if(!is_compatible_object(argument,expected))
                    throw HandlerError("incompatible_object_argument","The handle is incompatible with the declared argument class");
                object_arguments.emplace_back(handle,expected);
            }
            *reinterpret_cast<void**>(buffer+offset)=argument;
        }
        else if(type=="ClassProperty" && size==8) {
            const auto property_class=reinterpret_cast<void*>(read<uintptr_t>(field+0x70));
            const auto meta_class=reinterpret_cast<void*>(read<uintptr_t>(field+0x78));
            validate_class(property_class);validate_class(meta_class);
            const auto handle=class_argument_handle(v);
            void* argument=nullptr;
            if(!handle.empty()) {
                argument=resolve(handle);
                try { validate_class(argument); }
                catch(const HandlerError& error) {
                    if(error.code()!="invalid_object_class")throw;
                    throw HandlerError("invalid_class_argument","The handle refers to a UObject that is not a UClass");
                }
                if(!is_compatible_class(argument,property_class,meta_class))
                    throw HandlerError("incompatible_class_argument","The represented class is incompatible with the argument constraint");
                class_arguments.push_back({handle,property_class,meta_class});
            }
            *reinterpret_cast<void**>(buffer+offset)=argument;
        }
        else throw HandlerError("unsupported_signature","The argument type is unsupported: "+type);
    }
    if(arguments.size()!=input_count)throw HandlerError("unknown_argument","Extra arguments were provided");
    if(strict_pod_contract)require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
    for(const auto& [handle,expected]:object_arguments) {
        if(!is_compatible_object(resolve(handle),expected))
            throw HandlerError("incompatible_object_argument","The argument changed before the function call");
    }
    for(const auto& argument:class_arguments) {
        if(!is_compatible_class(resolve(argument.handle),argument.property_class,argument.meta_class))
            throw HandlerError("incompatible_class_argument","The class argument changed before the function call");
    }
    for(const auto& argument:enum_inputs) {
        const auto current=enum_layout(argument.type,argument.layout.size,argument.field);
        if(current.underlying_type!=argument.layout.underlying_type || current.size!=argument.layout.size
           || current.object!=argument.layout.object)
            throw HandlerError("unsupported_signature","The enum argument layout changed before the call");
        if(argument.enum_identity) {
            const auto table=enum_table(current.object);
            const auto& before=*argument.enum_identity;
            if(!same_serial_identity(serial_identity(before),serial_identity(table.identity)))
                throw HandlerError("unsupported_signature","The declared UEnum changed before the call");
        }
    }
    const auto invocation_after=validate_object(object);
    if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(invocation_after)))
        throw HandlerError("stale_handle","Target identity changed before the call; obtain a new handle");
    for(auto& input:string_inputs) {
        const auto constructor_contract=capture_function_contract(object,function_owner,function,target_handle);
        require_same_function_contract(pod_function_contract,constructor_contract);
        if(expected_token)require_same_function_contract(expected_contract,constructor_contract);
        StringHeader header{};
        if(!construct_string(base_+kStringConstruct,&header,input.source.c_str(),static_cast<int>(input.source.size()))) {
            string_ledger.mark_uncertain();string_quarantine_cause=1;
            extra_retained_count=1;
            extra_retained_bytes=header.capacity>0?static_cast<size_t>(header.capacity)*sizeof(char16_t):0;
            throw HandlerError("string_constructor_fault","The FString constructor raised a native exception; ownership is unknown");
        }
        extra_retained_count=header.data?1:0;
        extra_retained_bytes=header.capacity>0?static_cast<size_t>(header.capacity)*sizeof(char16_t):0;
        string_quarantine_cause=4;
        string_ledger.add_input(header,input.source.size());
        extra_retained_count=extra_retained_bytes=0;string_quarantine_cause=0;input.header=header;
        std::memcpy(buffer+input.offset,&header,sizeof(header));
    }
    validate_array_elements();
    for(auto& input:array_inputs) {
        const auto allocator_contract=capture_function_contract(object,function_owner,function,target_handle);
        require_same_function_contract(pod_function_contract,allocator_contract);
        if(expected_token)require_same_function_contract(expected_contract,allocator_contract);
        const auto input_count=(input.metadata.contains("enum") || input.metadata.contains("scalar_type"))?
            input.scalar_bytes.size()/input.stride:input.elements.size();
        if(input.metadata.value("scalar_type",std::string{})=="StrProperty") {
            input.string_indices.reserve(input.string_sources.size());
            for(size_t index=0;index<input.string_sources.size();++index) {
                const auto& source=input.string_sources[index];StringHeader string{};
                if(!construct_string(base_+kStringConstruct,&string,source.c_str(),static_cast<int>(source.size()))) {
                    string_ledger.mark_uncertain();
                    throw HandlerError("string_constructor_fault","An array FString constructor raised a native exception");
                }
                input.string_indices.push_back(string_ledger.input_count());
                string_ledger.add_input(string,source.size());
                std::memcpy(input.scalar_bytes.data()+index*16,&string,sizeof(string));
            }
        }
    const auto count=static_cast<int32_t>(input_count);ArrayHeader header{};
        if(count) {
        void* memory{};const auto allocation_bytes=static_cast<size_t>(count)*input.stride;
            if(!allocate_object_array(base_+kObjectArrayRealloc,allocation_bytes,&memory)) {
                array_ledger.mark_uncertain();array_quarantine_cause=1;
                extra_array_count=1;extra_array_bytes=allocation_bytes;
                throw HandlerError("array_allocator_fault","Object array Realloc raised a native exception; ownership is unknown");
            }
            header={reinterpret_cast<uintptr_t>(memory),count,count};
            extra_array_count=memory?1:0;extra_array_bytes=allocation_bytes;
        }
    array_quarantine_cause=4;array_ledger.add_input(header,input_count,input.stride,
        input.metadata.contains("struct")?input.metadata.at("struct").at("min_alignment").get<size_t>():
        input.metadata.value("scalar_type",std::string{})=="StrProperty"?size_t{8}:input.stride);
        extra_array_count=extra_array_bytes=0;array_quarantine_cause=0;input.header=header;
        if(count) {
        if(input.metadata.contains("enum") || input.metadata.contains("scalar_type")) {
            if(!copy_memory(reinterpret_cast<void*>(header.data),input.scalar_bytes.data(),input.scalar_bytes.size()))
                throw HandlerError("invalid_memory","The engine scalar array buffer is unavailable for writing");
        } else {
            for(size_t index=0;index<input.elements.size();++index)input.pointers[index]=input.elements[index].pointer;
            if(!copy_memory(reinterpret_cast<void*>(header.data),input.pointers.data(),static_cast<size_t>(count)*sizeof(uintptr_t)))
                throw HandlerError("invalid_memory","The engine Object array buffer is unavailable for writing");
        }
        }
        std::memcpy(buffer+input.offset,&header,sizeof(header));
    }
    validate_array_elements();validate_array_inputs();
    validate_string_inputs();
    if(expected_token || strict_pod_contract) {
        auto current_class=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);
        uintptr_t current_function{},current_owner{};std::set<uintptr_t> current_classes;
        for(;current_class && !current_function;current_class=read<uintptr_t>(current_class+0x40)) {
            if(!current_classes.insert(current_class).second)
                throw HandlerError("signature_changed","The function owner chain changed before the call");
            auto field=read<uintptr_t>(current_class+0x48);std::set<uintptr_t> current_fields;
            for(;field;field=read<uintptr_t>(field+0x28)) {
                if(!current_fields.insert(field).second)
                    throw HandlerError("signature_changed","The function chain changed before the call");
                if(object_name(reinterpret_cast<void*>(field))==function_name
                   && contains(object_name(reinterpret_cast<void*>(read<uintptr_t>(field+0x10))),"Function")) {
                    current_function=field;current_owner=current_class;break;
                }
            }
        }
        if(current_function!=function || current_owner!=function_owner)
            throw HandlerError("signature_changed","The nearest function implementation changed before the call");
        const auto final_contract=capture_function_contract(object,function_owner,function,target_handle);
        if(expected_token)require_same_function_contract(expected_contract,final_contract);
        if(strict_pod_contract)require_same_function_contract(pod_function_contract,final_contract);
    }
    for(const auto& input:name_inputs) {
        if(field_type(input.field,[this](uint64_t value){return name(value);})!="NameProperty"
           || read<int32_t>(input.field+0x44)!=input.field_offset)
            throw HandlerError("unsupported_signature","FName argument metadata changed before the call");
        validate_name_argument_layout(read<int32_t>(input.field+0x34),read<int32_t>(input.field+0x30));
        if(!(capture_name(input.source.source)==input.source))
            throw HandlerError("name_source_changed","The FName source value or metadata changed before the call");
    }
    validate_development_service_call(object,reinterpret_cast<void*>(function_owner),function_name,function,buffer);
    string_event_started=true;
    native_exception::Diagnostic native_fault{};
    bool event_returned{};
    {
    ++process_events_started_;
    event_returned=call_event(reinterpret_cast<void*>(process_event_),object,reinterpret_cast<void*>(function),buffer,&native_fault);
    if(event_returned)++process_events_returned_;
    }
    if(!event_returned) {
        if(contains_string_input || contains_string_output || contains_string_array_input || (contains_object_array && has_string_return)){string_ledger.mark_uncertain();string_quarantine_cause=2;}
        if(contains_object_array){array_ledger.mark_uncertain();array_quarantine_cause=2;}
        json details={{"executed",false},{"may_have_executed",true},{"do_not_retry",true},
            {"native_exception",native_exception::describe(native_fault)}};
        if(return_type=="StrProperty" || contains_string_output) {
            details["cleanup_attempted"]=false;details["cleanup_confirmed"]=false;
            details["cleanup_state"]="unknown";
            if(contains_string_output)details["string_output_cleanup_confirmed"]=false;
        }
        if(contains_owned_containers) {
            if(contains_container_return)details["return_cleanup_confirmed"]=false;
            if(contains_array_output)details["array_output_cleanup_confirmed"]=false;
            details["cleanup_confirmed"]=false;
            details["cleanup_state"]="unknown";
        }
        throw HandlerError("game_call_fault","The game call raised a native exception; its effect is unknown",std::move(details));
    }
    string_event_completed=true;

    validate_array_inputs();
    validate_string_inputs();
    register_string_outputs();
    json result,return_enum,result_metadata=nullptr,out_parameters=json::object(),out_parameter_metadata=json::object();
    auto decode_vector_outputs=[&] {
        if(vector_parameters.empty())return;
        const auto current_target=validate_object(object);
        if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(current_target)))
            throw HandlerError("stale_handle","The target changed while obtaining the FVector result");
        for(const auto& vector:vector_parameters) {
            if(!vector.output && !vector.returned)continue;
            if(read<int32_t>(vector.field+0x44)!=vector.offset || read<uint64_t>(vector.field+0x38)!=vector.flags)
                throw HandlerError("vector_metadata_changed","The FVector parameter changed after the call");
            const auto before=capture_vector_parameter(vector.field,vector.size,1);
            if(!(before==vector.layout))throw HandlerError("vector_metadata_changed","The FVector layout changed after the call");
            std::array<std::byte,24> copied{};
            if(!copy_memory(copied.data(),buffer+vector.offset,static_cast<size_t>(vector.size)))
                throw HandlerError("invalid_struct_copy","The FVector result could not be copied");
            const auto value=vector_arguments::decode(vector.layout.layout,std::span(copied).first(vector.size));
            if(!(capture_vector_parameter(vector.field,vector.size,1)==vector.layout)
               || read<int32_t>(vector.field+0x44)!=vector.offset || read<uint64_t>(vector.field+0x38)!=vector.flags)
                throw HandlerError("vector_metadata_changed","The FVector layout changed while copying the result");
            if(vector.returned){result=value;result_metadata=vector_snapshot_json(vector.layout,false);}
            else {out_parameters[vector.key]=value;out_parameter_metadata[vector.key]=vector_snapshot_json(vector.layout,false);}
        }
    };
    auto decode_color_outputs=[&] {
        for(const auto& color:color_parameters) {
            if(!color.returned)continue;
            if(read<int32_t>(color.field+0x44)!=color.offset || read<uint64_t>(color.field+0x38)!=color.flags
               || !(capture_linear_color_parameter(color.field,16,1)==color.layout))
                throw HandlerError("linear_color_metadata_changed","The FLinearColor return changed after the call");
            std::array<std::byte,16> copied{};
            if(!copy_memory(copied.data(),buffer+color.offset,copied.size()))
                throw HandlerError("invalid_struct_copy","The FLinearColor result could not be copied");
            result=linear_color_arguments::decode(color.layout.layout,copied);
            if(!(capture_linear_color_parameter(color.field,16,1)==color.layout)
               || read<int32_t>(color.field+0x44)!=color.offset || read<uint64_t>(color.field+0x38)!=color.flags)
                throw HandlerError("linear_color_metadata_changed","FLinearColor metadata changed while copying the result");
            result_metadata=linear_color_snapshot_json(color.layout,false);
        }
    };
    auto decode_transform_return=[&] {
        if(!transform_return)return;
        const auto& saved=*transform_return;
        if(read<int32_t>(saved.field+0x44)!=saved.offset
           || !(capture_transform_parameter(saved.field,96,1)==saved.layout))
            throw HandlerError("signature_changed","The Transform return changed after the call");
        std::array<std::byte,96> copied{};
        if(!copy_memory(copied.data(),buffer+saved.offset,copied.size()))
            throw HandlerError("invalid_struct_copy","The Transform result could not be copied");
        const auto value=transform_arguments::decode(saved.layout.layout,copied);
        if(read<int32_t>(saved.field+0x44)!=saved.offset
           || !(capture_transform_parameter(saved.field,96,1)==saved.layout))
            throw HandlerError("signature_changed","Transform metadata changed while copying the result");
        result=value;result_metadata=transform_snapshot_json(saved.layout,false);
    };
    auto decode_scalar_outputs=[&] {
        if(scalar_parameters.empty())return;
        if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(validate_object(object))))
            throw HandlerError("stale_handle","The target changed while obtaining the scalar output result");
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
        for(const auto& scalar:scalar_parameters) {
            if(read<int32_t>(scalar.field+0x44)!=scalar.offset || read<uint64_t>(scalar.field+0x38)!=scalar.flags)
                throw HandlerError("signature_changed","The scalar output parameter changed after the call");
            const auto current_type=field_type(scalar.field,[this](auto value){return name(value);});
            const auto before=capture_scalar_output_layout(scalar.field,current_type,read<int32_t>(scalar.field+0x34),read<int32_t>(scalar.field+0x30));
            if(!(before==scalar.layout))throw HandlerError("signature_changed","The scalar output layout changed after the call");
            std::array<std::byte,8> copied{};
            if(!copy_memory(copied.data(),buffer+scalar.offset,static_cast<size_t>(scalar.layout.element_size)))
                throw HandlerError("invalid_scalar_copy","The scalar output result could not be copied");
            const auto value=scalar_outputs::decode(scalar.layout,std::span(copied).first(scalar.layout.element_size));
            const auto after=capture_scalar_output_layout(scalar.field,field_type(scalar.field,[this](auto packed){return name(packed);}),
                read<int32_t>(scalar.field+0x34),read<int32_t>(scalar.field+0x30));
            if(!(after==scalar.layout) || read<int32_t>(scalar.field+0x44)!=scalar.offset || read<uint64_t>(scalar.field+0x38)!=scalar.flags)
                throw HandlerError("signature_changed","The scalar output layout changed while copying the result");
            out_parameters[scalar.key]=value;out_parameter_metadata[scalar.key]=scalar_output_metadata(scalar.layout);
        }
        require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
        if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(validate_object(object))))
            throw HandlerError("stale_handle","The target changed while copying the scalar output result");
    };
    auto decode_scalar_struct_return=[&] {
        if(!pod_struct_return.is_null()) {
            const auto field=pod_struct_return.at("pointer").get<uintptr_t>();
            const auto size=pod_struct_return.at("element_size").get<int>();
            const auto offset=pod_struct_return.at("offset").get<int>();
            if(capture_pod_struct_parameter(field,size,1)!=pod_struct_return)
                throw HandlerError("signature_changed","The DTO return metadata changed after the call");
            std::vector<std::byte> copied(static_cast<size_t>(size));
            if(!copy_memory(copied.data(),buffer+offset,copied.size()))
                throw HandlerError("invalid_struct_copy","The DTO result could not be copied");
            result=decode_pod_struct(pod_struct_return,copied);result_metadata=pod_struct_metadata(pod_struct_return);
            if(capture_pod_struct_parameter(field,size,1)!=pod_struct_return)
                throw HandlerError("signature_changed","The DTO metadata changed while copying the result");
            return;
        }
        if(!scalar_struct_return)return;
        const auto& saved=*scalar_struct_return;
        if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(validate_object(object))))
            throw HandlerError("stale_handle","The target changed while obtaining the DTO result");
        const auto before=capture_scalar_struct_parameter(saved.field,saved.element_size,saved.array_dim);
        if(!(before==saved))throw HandlerError("signature_changed","The DTO return changed after the call");
        std::vector<std::byte> copied(static_cast<size_t>(saved.element_size));
        if(!copy_memory(copied.data(),buffer+saved.offset,copied.size()))
            throw HandlerError("invalid_struct_copy","The DTO result could not be copied");
        const auto value=saved.path=="/Script/CoreUObject.Rotator"?
            rotator_arguments::decode(saved.layout,copied):scalar_struct_values::decode(saved.layout,copied);
        if(!(capture_scalar_struct_parameter(saved.field,saved.element_size,saved.array_dim)==saved))
            throw HandlerError("signature_changed","DTO metadata changed while copying the result");
        result=value;result_metadata=scalar_struct_snapshot_json(saved,false);
    };
    auto decode_pod_outputs=[&] {
        auto require_transform_result_contract=[&] {
            if(!contains_transform && !contains_color && !contains_rotator && !contains_scalar_struct && !contains_string_input && !contains_string_output && !contains_object_array && !contains_owned_containers)return;
            if(!same_serial_identity(serial_identity(invocation_identity),serial_identity(validate_object(object))))
                throw HandlerError("stale_handle","The target changed while processing the POD call result");
            require_same_function_contract(pod_function_contract,capture_function_contract(object,function_owner,function,target_handle));
        };
        require_transform_result_contract();
        decode_vector_outputs();decode_color_outputs();decode_transform_return();decode_scalar_outputs();decode_scalar_struct_return();
        for(const auto& output:string_outputs) {
            auto validate_output=[&]() {
                if(field_type(output.field,[this](auto value){return name(value);})!="StrProperty"
                   || read<int32_t>(output.field+0x44)!=output.offset || read<uint64_t>(output.field+0x38)!=output.flags
                   || read<int32_t>(output.field+0x34)!=16 || read<int32_t>(output.field+0x30)!=1)
                    throw HandlerError("signature_changed","The FString output metadata changed after the call");
                parameter_direction::classify(output.flags);
                const auto current=read<StringHeader>(reinterpret_cast<uintptr_t>(buffer+output.offset));
                if(!string_input_lifetime::same_header(current,output.header)) {
                    string_ledger.mark_uncertain();
                    throw HandlerError("string_lifetime_unknown","The FString output header changed while copying its value");
                }
            };
            validate_output();
            out_parameters[output.key]=copied_string(output.header);
            out_parameter_metadata[output.key]={{"type","StrProperty"},{"element_size",16},{"array_dim",1}};
            validate_output();
        }
        for(const auto& output:pod_struct_outputs) {
            if(capture_pod_struct_parameter(output.field,output.size,1)!=output.metadata)
                throw HandlerError("signature_changed","The output DTO metadata changed after the call");
            std::vector<std::byte> copied(static_cast<size_t>(output.size));
            if(!copy_memory(copied.data(),buffer+output.offset,copied.size()))
                throw HandlerError("invalid_struct_copy","The output DTO could not be copied");
            out_parameters[output.key]=decode_pod_struct(output.metadata,copied);
            out_parameter_metadata[output.key]=pod_struct_metadata(output.metadata);
            if(capture_pod_struct_parameter(output.field,output.size,1)!=output.metadata)
                throw HandlerError("signature_changed","The output DTO metadata changed while copying its value");
        }
        for(const auto& output:array_outputs) {
            out_parameters[output.key]=output.metadata.contains("scalar_type")
                ?read_scalar_array_return(output.field,reinterpret_cast<uintptr_t>(buffer+output.offset),output.metadata)
                :read_object_array_return(output.field,reinterpret_cast<uintptr_t>(buffer+output.offset),output.metadata);
            out_parameter_metadata[output.key]=output.metadata.at("public");
        }
        require_transform_result_contract();
    };
    auto append_pod_results=[&](json response) {
        if(contains_owned_containers) {
            if(!release_containers())throw HandlerError("game_result_error","Object container release is unconfirmed; do not repeat the request");
            record_container_cleanup(response);
        }
        if(contains_string_output)response["string_output_cleanup_confirmed"]=string_outputs_registered && string_cleanup_done;
        if(!result_metadata.is_null())response["result_metadata"]=result_metadata;
        if(!out_parameters.empty()) {
            response["out_parameters"]=out_parameters;response["out_parameter_metadata"]=out_parameter_metadata;
        }
        try {
        const auto& contract=invocation_evidence_contract;
        bool complete_call=true;
        for(const auto& parameter:contract.at("public_function").at("parameters")) {
            const auto type=parameter.at("type").get<std::string>();
            if(type!="ArrayProperty" && type!="SetProperty")continue;
            const auto key=parameter.at("name").get<std::string>();
            const bool returned=parameter.value("return",false),output=parameter.value("out",false)&&!parameter.value("const",false)&&!returned;
            if(!returned && !output)continue;
            const auto& value=returned?response.at("result"):out_parameters.at(key);
            if(value.is_object() && value.value("partial",false)){complete_call=false;break;}
        }
        if(complete_call)
        for(const auto& parameter:contract.at("public_function").at("parameters")) {
            auto shape=public_shape(parameter);
            if(!shape.contains("direction"))shape["direction"]=parameter.value("return",false)?"return":
                parameter.value("out",false)&&!parameter.value("const",false)?"output":"input";
            const auto key=parameter.at("name").get<std::string>();
            const bool returned=shape.at("direction")=="return";
            const bool output=shape.at("direction")=="output";
            const auto& value=returned?response.at("result"):output?out_parameters.at(key):arguments.at(key);
            const bool container=shape.at("type")=="ArrayProperty" || shape.at("type")=="SetProperty";
            const bool partial=container && (returned || output) && value.is_object() && value.value("partial",false);
            const bool populated=shape.at("type")=="ArrayProperty" || shape.at("type")=="SetProperty"
                ?value.is_array()?!value.empty():value.is_object()&&value.contains("items")&&!value.at("items").empty():true;
            const bool owned=owned_shape(shape);
            evidence_.observe("invoke",shape,ticks_.load(),owned,response.value("cleanup_confirmed",!owned),partial,populated);
        }
        }catch(...) {} // Observation failure does not change a completed call.
        return response;
    };
    if(return_type=="StrProperty") {
        StringHeader header{};
        if(!copy_memory(&header,buffer+return_offset,sizeof(header)))
            throw HandlerError("game_result_error","The call executed but FString could not be read; cleanup is unconfirmed",
                               string_cleanup_details(false,false,"invalid_string_storage"));
        if(contains_string_input || contains_string_output || contains_object_array || contains_owned_containers) {
            string_ledger.add_return(header);string_return_registered=true;
            result=copied_string(header);
            decode_pod_outputs();validate_string_inputs();
            register_string_array_results();
            const auto cleanup=release_strings();
            if(!cleanup.confirmed)throw HandlerError("game_result_error","FString release is unconfirmed; do not repeat the request");
            json response={{"function",function_name},{"return_type",return_type},{"result",result},{"executed",true},{"process_event_returned",true},
                {"thread_id",GetCurrentThreadId()},{"cleanup_confirmed",true},{"return_cleanup_confirmed",true}};
            if(contains_string_input)response["input_cleanup_confirmed"]=true;
            if(contains_object_array) {
                const auto arrays=release_arrays();
                if(!arrays.confirmed)throw HandlerError("game_result_error","Object array release is unconfirmed; do not repeat the request");
                response["array_input_cleanup_confirmed"]=true;
            }
            return append_pod_results(std::move(response));
        }
        result=copied_owned_string_return(header,
            [](const StringHeader& copied){return copied_string(copied);},
            [this](uintptr_t memory){return free_string(base_+kFree,reinterpret_cast<void*>(memory));});
        try{decode_pod_outputs();}catch(const HandlerError& error) {
            throw HandlerError("game_result_error","The call executed but POD output could not be read; do not repeat the request",
                {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"cause_code",error.code()},{"cleanup_confirmed",true}});
        } catch(...) {
            throw HandlerError("game_result_error","The call executed but POD output could not be read; do not repeat the request",
                {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"cause_code","result_processing_failed"},{"cleanup_confirmed",true}});
        }
        return append_pod_results({{"function",function_name},{"return_type",return_type},{"result",result},
                {"executed",true},{"process_event_returned",true},{"thread_id",GetCurrentThreadId()},{"cleanup_confirmed",true}});
    }
    try {
        if(return_type=="SetProperty") {
            result=read_object_set_return(return_field,reinterpret_cast<uintptr_t>(buffer+return_offset),set_return_metadata);
            result_metadata=set_return_metadata.at("public");
        } else if(return_type=="ArrayProperty") {
            result=array_return_metadata.contains("scalar_type")
                ?read_scalar_array_return(return_field,reinterpret_cast<uintptr_t>(buffer+return_offset),array_return_metadata)
                :read_object_array_return(return_field,reinterpret_cast<uintptr_t>(buffer+return_offset),array_return_metadata);
            result_metadata=array_return_metadata.at("public");
        } else if(return_type=="StructProperty") {
            // The POD result is copied below using its saved reflected layout.
        } else if(return_type=="NameProperty") {
            result=copied_name_return(return_size,1,
                [&]{return read<uint64_t>(reinterpret_cast<uintptr_t>(buffer+return_offset));},
                [this](uint64_t copied){return name(copied);});
        } else if(return_type=="EnumProperty" || return_type=="ByteProperty") {
            const auto returned=enum_property(reinterpret_cast<uintptr_t>(buffer+return_offset),return_type,return_size,return_field);
            result=returned.at("value");if(returned.contains("enum"))return_enum=returned.at("enum");
        } else result=return_field?property_value(reinterpret_cast<uintptr_t>(buffer+return_offset),return_type,return_size,return_field):json(nullptr);
        decode_pod_outputs();
        if(contains_owned_containers && !release_containers())
            throw HandlerError("game_result_error","Object container release is unconfirmed; do not repeat the request");
    }
    catch(const HandlerError& error) {
        json details={{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"cause_code",error.code()}};
        if(contains_owned_containers){release_containers();record_container_cleanup(details);}
        throw HandlerError("game_result_error","The call executed but its result was not obtained; do not repeat the request",std::move(details));
    } catch(...) {
        json details={{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"cause_code","result_processing_failed"}};
        if(contains_owned_containers){release_containers();record_container_cleanup(details);}
        throw HandlerError("game_result_error","The call executed but its result was not obtained; do not repeat the request",std::move(details));
    }
    json response={{"function",function_name},{"return_type",return_type},{"result",result},{"executed",true},{"process_event_returned",true},{"thread_id",GetCurrentThreadId()}};
    record_container_cleanup(response);
    if(!return_enum.is_null())response["return_enum"]=return_enum;
    if(contains_string_input || contains_string_output || contains_string_array_input) {
        validate_string_inputs();const auto cleanup=release_strings();
        if(!cleanup.confirmed)throw HandlerError("game_result_error","FString input release is unconfirmed; do not repeat the request");
        response["cleanup_confirmed"]=true;
        if(contains_string_input || contains_string_array_input)response["input_cleanup_confirmed"]=true;
        if(contains_string_array_input)response["string_array_cleanup_confirmed"]=true;
    }
    if(contains_object_array) {
        validate_array_inputs();const auto cleanup=release_arrays();
        if(!cleanup.confirmed)throw HandlerError("game_result_error","Object array input release is unconfirmed; do not repeat the request");
        response["cleanup_confirmed"]=true;response["array_input_cleanup_confirmed"]=true;
    }
    return append_pod_results(std::move(response));
    } catch(const HandlerError& error) {
        if(contains_owned_containers && string_event_completed)release_containers();
        if(contains_object_array) {
            if(string_event_completed && !array_cleanup_done && !array_ledger.uncertain()) {
                try{validate_array_inputs();}
                catch(...) {array_ledger.mark_uncertain();array_quarantine_cause=3;}
            }
            const auto cleanup=release_arrays();
            json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},
                {"do_not_retry",string_event_started || !cleanup.confirmed},{"cause_code",error.code()},
                {"cleanup_attempted",cleanup.attempted},{"cleanup_confirmed",cleanup.confirmed},
                {"array_input_cleanup_confirmed",cleanup.confirmed},{"cleanup_state",cleanup.confirmed?"released":"unknown"},
                {"object_array_input_quarantined",object_array_inputs_quarantined_.load()}};
            release_mixed_strings(details);
            record_container_cleanup(details);

            if(string_event_started || !details["cleanup_confirmed"].get<bool>())
                throw HandlerError("game_result_error",string_event_completed?"The call executed; Object array processing or cleanup is incomplete":
                    string_event_started?"The call effect is unknown; Object array ownership is unconfirmed":"The call did not execute; Object array ownership is unconfirmed",std::move(details));
            throw HandlerError(error.code(),error.what(),std::move(details));
        }
        if(contains_string_output) {
            auto details=error.details();
            details.update({{"executed",string_event_completed},{"may_have_executed",string_event_started},
                {"do_not_retry",string_event_started},{"cause_code",error.code()}});
            release_mixed_strings(details);record_container_cleanup(details);
            if(string_event_started || !details["cleanup_confirmed"].get<bool>())
                throw HandlerError("game_result_error","FString output processing or cleanup is incomplete; do not repeat the request",std::move(details));
            throw HandlerError(error.code(),error.what(),std::move(details));
        }
        if(!contains_string_input) {
            if(contains_owned_containers) {
                auto details=error.details();record_container_cleanup(details);
                throw HandlerError(error.code(),error.what(),std::move(details));
            }
            throw;
        }
        if(string_event_completed && !string_cleanup_done && !string_ledger.uncertain()) {
            try{validate_string_inputs();}
            catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;}
        }
        if(string_event_completed && !string_cleanup_done && return_type=="StrProperty" && !string_return_registered && !string_ledger.uncertain()) {
            StringHeader header{};
            try {
                if(!copy_memory(&header,buffer+return_offset,sizeof(header)))throw HandlerError("invalid_string_storage","The FString result is unavailable");
                string_ledger.add_return(header);string_return_registered=true;
            } catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=4;}
        }
        const auto cleanup=release_strings();
        json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},{"do_not_retry",string_event_started || !cleanup.confirmed},
            {"cause_code",error.code()},{"cleanup_attempted",cleanup.attempted},{"cleanup_confirmed",cleanup.confirmed},
            {"input_cleanup_confirmed",cleanup.confirmed},{"cleanup_state",cleanup.confirmed?"released":"unknown"},
            {"string_input_quarantined",string_inputs_quarantined_.load()}};
        record_container_cleanup(details);

        if(string_event_started || !cleanup.confirmed)
            throw HandlerError("game_result_error",string_event_completed?"The call executed; FString processing or cleanup is incomplete":
                string_event_started?"The call effect is unknown; FString ownership is unconfirmed":"The call did not execute; FString ownership is unconfirmed",std::move(details));
        throw HandlerError(error.code(),error.what(),std::move(details));
    } catch(...) {
        if(contains_owned_containers && string_event_completed)release_containers();
        if(contains_object_array) {
            if(string_event_completed && !array_cleanup_done && !array_ledger.uncertain()) {
                try{validate_array_inputs();}
                catch(...) {array_ledger.mark_uncertain();array_quarantine_cause=3;}
            }
            const auto cleanup=release_arrays();
            json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},{"do_not_retry",true},
                {"cause_code","result_processing_failed"},{"cleanup_attempted",cleanup.attempted},{"cleanup_confirmed",cleanup.confirmed},
                {"array_input_cleanup_confirmed",cleanup.confirmed},{"cleanup_state",cleanup.confirmed?"released":"unknown"},
                {"object_array_input_quarantined",object_array_inputs_quarantined_.load()}};
            release_mixed_strings(details);
            record_container_cleanup(details);

            throw HandlerError("game_result_error","Object array processing is incomplete; do not repeat the request",std::move(details));
        }
        if(contains_string_output) {
            json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},
                {"do_not_retry",true},{"cause_code","result_processing_failed"}};
            release_mixed_strings(details);record_container_cleanup(details);

            throw HandlerError("game_result_error","FString output processing or cleanup is incomplete; do not repeat the request",std::move(details));
        }
        if(!contains_string_input) {
            if(contains_owned_containers) {
                json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},{"do_not_retry",true},{"cause_code","result_processing_failed"}};
                record_container_cleanup(details);

                throw HandlerError("game_result_error","Object container processing is incomplete; do not repeat the request",std::move(details));
            }
            throw;
        }
        if(string_event_completed && !string_cleanup_done && !string_ledger.uncertain()) {
            try{validate_string_inputs();}
            catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;}
        }
        if(string_event_completed && !string_cleanup_done && return_type=="StrProperty" && !string_return_registered) {
            string_ledger.mark_uncertain();string_quarantine_cause=4;
        }
        const auto cleanup=release_strings();

        json details=
            {{"executed",string_event_completed},{"may_have_executed",string_event_started},{"do_not_retry",true},{"cause_code","result_processing_failed"},
             {"cleanup_attempted",cleanup.attempted},{"cleanup_confirmed",cleanup.confirmed && (!contains_owned_containers || container_return_cleanup_confirmed)},
             {"return_cleanup_confirmed",!contains_container_return || container_return_cleanup_confirmed},{"input_cleanup_confirmed",cleanup.confirmed},
             {"cleanup_state",cleanup.confirmed && (!contains_owned_containers || container_return_cleanup_confirmed)?"released":"unknown"},{"string_input_quarantined",string_inputs_quarantined_.load()}};
        record_container_cleanup(details);
        throw HandlerError("game_result_error","FString processing is incomplete; do not repeat the request",
            std::move(details));
    }
}
#if defined(SN2_MCP_DEVELOPMENT_PROBES)
json GameAccess::development_uint64_fixture(const json& params) {
    auto object=resolve(params.at("handle").get<std::string>());
    const auto identity=serial_identity(validate_object(object));
    const auto function_name=params.at("function").get<std::string>(),parameter_name=params.at("parameter").get<std::string>();
    std::uintptr_t function{},parameter{};std::set<std::uintptr_t> owners;
    for(auto klass=reinterpret_cast<std::uintptr_t>(validate_object(object).object_class);klass&&!function;klass=read<std::uintptr_t>(klass+0x40)) {
        if(!owners.insert(klass).second)throw HandlerError("fixture_metadata_cycle","The function owner chain contains a cycle");
        std::set<std::uintptr_t> fields;
        for(auto child=read<std::uintptr_t>(klass+0x48);child;child=read<std::uintptr_t>(child+0x28)) {
            if(!fields.insert(child).second)throw HandlerError("fixture_metadata_cycle","The UField chain contains a cycle");
            if(object_name(reinterpret_cast<void*>(child))==function_name && class_is(reinterpret_cast<void*>(child),"/Script/CoreUObject.Function")){function=child;break;}
        }
    }
    if(!function)throw HandlerError("function_not_found","The fixture's reflected function was not found");
    std::set<std::uintptr_t> parameters;
    for(auto field=read<std::uintptr_t>(function+0x50);field;field=read<std::uintptr_t>(field+0x18)) {
        if(!parameters.insert(field).second)throw HandlerError("fixture_metadata_cycle","The parameter chain contains a cycle");
        if(name(read<std::uint64_t>(field+0x20))==parameter_name){parameter=field;break;}
    }
    if(!parameter || field_type(parameter,[this](auto value){return name(value);})!="StructProperty")
        throw HandlerError("unsupported_fixture_metadata","The uint64 fixture requires a reflected TimerHandle struct parameter");
    const auto structure=read<std::uintptr_t>(parameter+0x70);
    if(object_path(reinterpret_cast<void*>(structure))!="/Script/Engine.TimerHandle")
        throw HandlerError("unsupported_fixture_metadata","The uint64 fixture requires exact Engine.TimerHandle metadata");
    std::uintptr_t handle_field{};std::set<std::uintptr_t> children;
    for(auto field=read<std::uintptr_t>(structure+0x50);field;field=read<std::uintptr_t>(field+0x18)) {
        if(!children.insert(field).second)throw HandlerError("fixture_metadata_cycle","The TimerHandle field chain contains a cycle");
        if(name(read<std::uint64_t>(field+0x20))=="Handle"){handle_field=field;break;}
    }
    if(!handle_field || field_type(handle_field,[this](auto value){return name(value);})!="UInt64Property"
       || read<std::int32_t>(handle_field+0x34)!=8 || read<std::int32_t>(handle_field+0x30)!=1 || read<std::int32_t>(handle_field+0x44)!=0)
        throw HandlerError("unsupported_fixture_metadata","TimerHandle.Handle is not an exact UInt64Property");
    auto values=json::array();
    for(const auto source:{std::uint64_t{},std::uint64_t{1},std::numeric_limits<std::uint64_t>::max()}) {
        alignas(16) std::array<std::byte,8> storage{};std::memcpy(storage.data(),&source,sizeof(source));
        values.push_back(property_value(reinterpret_cast<std::uintptr_t>(storage.data()),"UInt64Property",8,handle_field));
    }
    if(!same_serial_identity(identity,serial_identity(validate_object(object))))
        throw HandlerError("fixture_metadata_changed","The fixture owner identity changed");
    return {{"source","development_fixture"},{"thread_id",GetCurrentThreadId()},{"tick_count",ticks_.load()},
        {"type","UInt64Property"},{"element_size",8},{"array_dim",1},{"metadata_source","Engine.TimerHandle.Handle"},
        {"values",values},{"live_object_modified",false},{"engine_allocation_required",false},{"process_event_called",false}};
}
#endif
json GameAccess::execute(const json& request) {
    auto op=request.at("op").get<std::string>();auto params=request.at("params");
    if(GetCurrentThreadId()!=game_thread_.load())
        throw HandlerError("inspection_wrong_thread","Reflection work requires the confirmed game Tick thread");
    require_runtime_operation(op);
    auto session=request.at("session_id").get<std::string>();
    if(session!=handle_session_){observations_.disconnect(handle_session_);handles_.clear();signature_contracts_.clear();handle_session_=session;}
    const auto now=static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
    if(op=="load_asset")return load_asset(params,false);
    if(op=="load_class")return load_asset(params,true);
    if(op=="blueprint_mod_enable")return blueprint_mod_enable(params);
    if(op=="blueprint_mod_status")return blueprint_mod_status(params);
    if(op=="blueprint_mod_disable")return blueprint_mod_disable(params);
if(op=="runtime_create_package")return runtime_create_package(params);
if(op=="runtime_compile_blueprint")return runtime_compile_blueprint(params);
if(op=="runtime_save_package")return runtime_save_package(params);
if(op=="runtime_release_blueprint")return runtime_release_blueprint(params);
    if(op=="snapshot") {
        if(!params.contains("targets"))throw HandlerError("invalid_snapshot_params","targets is required");
        return snapshot(params.at("targets"));
    }
    if(op=="watch_start") {
        if(!params.contains("handle") || !params.contains("properties"))
            throw HandlerError("invalid_watch_params","handle and properties are required");
        auto targets=json::array({{{"handle",params.at("handle")},{"properties",params.at("properties")}}});
        Observations::validate_targets(targets);
        return observations_.start(session,params.value("client_owner",session),params,snapshot(targets),now,ticks_.load());
    }
    if(op=="watch_read")return observations_.read(params.value("client_owner",session),params);
    if(op=="watch_stop")return observations_.stop(params.value("client_owner",session),params,now,ticks_.load());
    if(op=="watch_forget")return observations_.forget(params.value("client_owner",session),params);
    if(op=="watch_list")return observations_.list(session,params.value("client_owner",session));
    if(op=="watch_resume")return observations_.resume(session,params.value("client_owner",session),params);
#if defined(SN2_MCP_DEVELOPMENT_PROBES)
    if(op=="_development_sparse_fixture")return development_sparse_fixture(params);
    if(op=="_development_uint64_fixture")return development_uint64_fixture(params);
    if(op=="_development_array_fixture")return development_array_fixture(params);
    if(op=="_development_owned_output_fixture")return development_owned_output_fixture(params);
#endif
    if(op!="objects" && op!="properties" && op!="functions" && op!="invoke" && op!="read_array" && op!="read_set" && op!="read_map" && op!="read_struct")throw HandlerError("unknown_operation","The operation is unsupported");
    if(op=="objects") {
        auto query=params.value("query",std::string{}),class_query=params.value("class_query",std::string{});const auto limit=limit_of(params);const auto offset=offset_of(params);
        if(params.contains("exact_path") && !params.at("exact_path").is_string())
            throw HandlerError("invalid_query","exact_path requires a string");
        const auto exact_path=params.value("exact_path",std::string{});
        if(!object_query::valid_exact_path(exact_path))
            throw HandlerError("invalid_query","exact_path must contain no NUL");
        auto count=read<int32_t>(base_+kObjects+0x14);if(count<0)throw HandlerError("invalid_registry","Invalid object registry");
        json items=json::array();int scanned=0,skipped=0;
        const auto begin=static_cast<int>(std::min(offset,static_cast<std::size_t>(count)));
        for(int index=begin;index<count;++index){++scanned;
            try {
                auto object=slot(index);if(!object)continue;
                const auto ptr=reinterpret_cast<uintptr_t>(object);
                if(read<int32_t>(ptr+0xc)!=index)continue;
                const auto n=object_name(object),c=object_name(reinterpret_cast<void*>(read<uintptr_t>(ptr+0x10)));
                if(!contains(c,class_query))continue;
                if(!contains(n,query) && !object_query::query_uses_path(query))continue;
                if(!object_query::exact_path_may_match_name(exact_path,n))continue;
                const auto path=object_query::needs_path(query,exact_path,n)?object_path(object):std::string{};
                if(!object_query::matches(query,exact_path,n,path))continue;
                items.push_back(object_info(object,index));
                if(items.size()>=limit)break;
            }
            catch(const HandlerError& error){if(error.code()=="handle_limit")throw;++skipped;}
        }
        const bool more=begin+scanned<count;
        return {{"items",items},{"total_slots",count},{"scanned_slots",scanned},{"skipped",skipped},
                {"offset",offset},{"offset_kind","registry_slot"},{"next_offset",more?json(begin+scanned):json(nullptr)},
                {"truncated",more},{"scan_complete",!more},{"thread_id",GetCurrentThreadId()}};
    }
    if(op=="read_array")return read_array(params);
    if(op=="read_set")return read_set(params);
    if(op=="read_map")return read_map(params);
    if(op=="properties" && params.contains("property_names"))selected_property_query::validate(params.at("property_names"));
    auto object=resolve(params.at("handle").get<std::string>());
    if(op=="read_struct") {
        if(!params.contains("property_name") || !params.at("property_name").is_string())
            throw HandlerError("invalid_struct_params","A string property name is required");
        return read_struct(object,params.at("handle").get<std::string>(),params.at("property_name").get<std::string>());
    }
    if(op=="properties") {
        if(!params.contains("property_names")) {
            std::size_t limit=50;
            if(params.contains("limit")) {
                const auto& value=params.at("limit");
                if(!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>()<1)
                   || value.get<uint64_t>()<1 || value.get<uint64_t>()>std::numeric_limits<std::size_t>::max())
                    throw HandlerError("invalid_limit","limit must be a positive integer representable by size_t");
                limit=static_cast<std::size_t>(value.get<uint64_t>());
            }
            const auto before=serial_identity(validate_object(object));
            auto result=properties(object,limit,offset_of(params));
            if(resolve(params.at("handle").get<std::string>())!=object || !same_serial_identity(before,serial_identity(validate_object(object))))
                throw HandlerError("stale_handle","Object identity changed while reading properties");
            return result;
        }
        const auto before=serial_identity(validate_object(object));
        auto result=selected_properties(object,params.at("property_names"));
        if(resolve(params.at("handle").get<std::string>())!=object || !same_serial_identity(before,serial_identity(validate_object(object))))
            throw HandlerError("stale_handle","The object changed while reading selected properties");
        result["items"]=std::move(result["properties"]);result.erase("properties");
        result["validation"]="game_thread_memory_read";return result;
    }
    if(op=="functions") {
        if(params.contains("function_query") && !params.at("function_query").is_string())
            throw HandlerError("invalid_query","function_query requires a string");
        const auto query=params.value("function_query",std::string{});
        if(!function_query::valid(query))throw HandlerError("invalid_query","function_query must contain no NUL");
        return functions(object,limit_of(params),offset_of(params),query,params.at("handle").get<std::string>());
    }
    if(op=="invoke")return invoke(object,params.at("function").get<std::string>(),params.value("arguments",json::object()),
                                  params.at("handle").get<std::string>(),params.contains("expected_signature_token")?&params.at("expected_signature_token"):nullptr);
    throw HandlerError("unknown_operation","The operation is unsupported");
}
}
