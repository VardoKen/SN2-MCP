#include "g1r_mcp/game_access.hpp"
#include "g1r_mcp/transport.hpp"
#include "g1r_mcp/scalar_arguments.hpp"
#include "g1r_mcp/object_arguments.hpp"
#include "g1r_mcp/class_arguments.hpp"
#include "g1r_mcp/object_query.hpp"
#include "g1r_mcp/function_query.hpp"
#include "g1r_mcp/function_contract.hpp"
#include "g1r_mcp/selected_property_query.hpp"
#include "g1r_mcp/name_return.hpp"
#include "g1r_mcp/name_argument.hpp"
#include "g1r_mcp/string_return.hpp"
#include "g1r_mcp/array_page.hpp"
#include "g1r_mcp/enum_values.hpp"
#include "g1r_mcp/enum_arguments.hpp"
#include "g1r_mcp/vector_layout.hpp"
#include "g1r_mcp/vector_arguments.hpp"
#include "g1r_mcp/vector_parameter_direction.hpp"
#include "g1r_mcp/parameter_direction.hpp"
#include "g1r_mcp/scalar_outputs.hpp"
#include "g1r_mcp/scalar_struct_values.hpp"
#include "g1r_mcp/scalar_struct_arguments.hpp"
#include "g1r_mcp/rotator_arguments.hpp"
#include "g1r_mcp/aligned_parameter_buffer.hpp"
#include "g1r_mcp/transform_layout.hpp"
#include "g1r_mcp/transform_arguments.hpp"
#include "g1r_mcp/string_arguments.hpp"
#include "g1r_mcp/string_input_lifetime.hpp"
#include "g1r_mcp/object_array_input_lifetime.hpp"
#include "g1r_mcp/linear_color_layout.hpp"
#include "g1r_mcp/linear_color_arguments.hpp"
#include "g1r_mcp/cleanup_once.hpp"
#include "g1r_mcp/native_exception.hpp"
#include "g1r_mcp/as_exception_capture.hpp"
#include "g1r_mcp/image_patterns.hpp"
#include <MinHook.h>
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstring>
#include <fstream>
#include <optional>
#include <sstream>
#include <set>
#include <vector>

namespace g1r_mcp {
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
    HMODULE self{};GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS|GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
        reinterpret_cast<LPCWSTR>(&diagnostic),&self);
    wchar_t path[32768]{};GetModuleFileNameW(self,path,32768);std::wstring file(path);file=file.substr(0,file.find_last_of(L"\\/"))+L"\\native-tick.log";
    std::ofstream output(file,std::ios::app);output<<text<<"\n";
}
uintptr_t kObjects{};
uintptr_t kNames{};
uintptr_t kTick{};
uintptr_t kProcessEvent{};
uintptr_t kFree{};
uintptr_t kAngelscriptManager{};
uintptr_t kFormatDiagnostics{};
uintptr_t kASExceptionLogger{};
uintptr_t kCompilationLockOffset{};
uintptr_t kASStackTrace{};
uintptr_t kASReload{};
uintptr_t kMoviePlayerInterfaceVtable{};
uintptr_t kMoviePlayerWait{};
uintptr_t kPlatformPumpMessages{};
uintptr_t kMoviePlayerLoadingFinished{};
uintptr_t kMoviePlayerPlaying{};
uintptr_t kMoviePlayerStop{};
uintptr_t kStringConstruct{};
uintptr_t kNameConstruct{};
uintptr_t kObjectArrayRealloc{};

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
bool safe_movieplayer_flag(uintptr_t function,uintptr_t player,bool* result,native_exception::Diagnostic* fault) {
    __try {*result=reinterpret_cast<bool(__fastcall*)(void*)>(function)(reinterpret_cast<void*>(player));return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)) {return false;}
}
bool safe_movieplayer_stop(uintptr_t function,uintptr_t player,native_exception::Diagnostic* fault) {
    __try {reinterpret_cast<void(__fastcall*)(void*)>(function)(reinterpret_cast<void*>(player));return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)) {return false;}
}
void movieplayer_wait_frame_cleanup(MoviePlayerJobs::WaitFrame* frame,bool returned) noexcept {
    if(!frame)return;
    if(returned)frame->returned(steady_ms());
    delete frame;
}
void movieplayer_wait_with_frame(uintptr_t original,void* player,bool allow_engine_tick,MoviePlayerJobs::WaitFrame* frame) {
    __try {reinterpret_cast<void(__fastcall*)(void*,bool)>(original)(player,allow_engine_tick);}
    __finally {movieplayer_wait_frame_cleanup(frame,AbnormalTermination()==FALSE);}
}
bool free_string(uintptr_t free_address, void* memory) {
    if (!memory) return true;
    __try { reinterpret_cast<void(__fastcall*)(void*)>(free_address)(memory); return true; }
    __except(EXCEPTION_EXECUTE_HANDLER) { return false; }
}
bool construct_string(uintptr_t constructor,g1r_mcp::StringHeader* header,const char16_t* text,int units) {
    __try {reinterpret_cast<void(__fastcall*)(g1r_mcp::StringHeader*,const char16_t*,int)>(constructor)(header,text,units);return true;}
    __except(EXCEPTION_EXECUTE_HANDLER) {return false;}
}
bool construct_name(uintptr_t constructor,uint64_t* value,const char16_t* text) {
    __try {reinterpret_cast<uint64_t*(__fastcall*)(uint64_t*,const char16_t*,uint32_t)>(constructor)(value,text,0);return true;}
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
std::string copied_string(const g1r_mcp::StringHeader& header) {
    g1r_mcp::validate_string_header(header);
    std::vector<wchar_t> text(static_cast<size_t>(header.count));
    if(header.count && !copy_memory(text.data(),reinterpret_cast<void*>(header.data),
                                   static_cast<size_t>(header.count)*sizeof(wchar_t)))
        throw HandlerError("invalid_memory","String memory is unavailable");
    if(header.count && text.back()==0)text.pop_back();
    return utf8(text.data(),static_cast<int>(text.size()));
}
// Current executable evidence: dev91-script-reload-source-evidence.json.
// The caller passes a 16-byte array header of two-FString, 32-byte entries.
// The source meanings of these two fields remain unknown.
struct ReloadChangedFilePair {
    StringHeader first;
    StringHeader second;
};
static_assert(sizeof(ReloadChangedFilePair)==32);
void capture_reload_changed_files(void* changed_files,json& output) {
    const auto address=reinterpret_cast<uintptr_t>(changed_files);
    const auto header=read<ArrayHeader>(address);
    const auto page=array_page(header,sizeof(ReloadChangedFilePair),1,0,
        std::numeric_limits<std::size_t>::max());
    for(int32_t index=0;index<page.count;++index) {
        const auto pair_address=page.begin+static_cast<uintptr_t>(index)*sizeof(ReloadChangedFilePair);
        const auto pair=read<ReloadChangedFilePair>(pair_address);
        auto first=copied_string(pair.first);
        auto second=copied_string(pair.second);
        const auto after=read<ReloadChangedFilePair>(pair_address);
        if(std::memcmp(&pair,&after,sizeof(pair)))
            throw HandlerError("reload_capture_changed","The changed-file string headers changed while being copied");
        output.push_back({{"first",std::move(first)},{"second",std::move(second)}});
    }
    if(!same_array_header(header,read<ArrayHeader>(address)))
        throw HandlerError("reload_capture_changed","The changed-file array header changed while being copied");
}
std::size_t limit_of(const json& params,std::size_t default_value=50) {
    if(!params.contains("limit"))return default_value;
    const auto& value=params.at("limit");
    if(!value.is_number_integer() || (!value.is_number_unsigned() && value.get<int64_t>()<1)
       || value.get<uint64_t>()<1 || value.get<uint64_t>()>std::numeric_limits<std::size_t>::max())
        throw HandlerError("invalid_limit","limit must be a positive integer representable by size_t");
    return static_cast<std::size_t>(value.get<uint64_t>());
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
        return copied_string(read<g1r_mcp::StringHeader>(address));
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
    const image_patterns::Image image(candidate_base);
    kTick=image.find_unique("game_tick",
        "4C 8B DC 55 57 41 54 49 8D AB 08 FC FF FF 48 81 EC E0 04 00 00 "
        "48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 85 B8 03 00 00");
    kProcessEvent=image.find_unique("process_event",
        "40 55 56 57 41 54 41 55 41 56 41 57 48 81 EC 70 01 00 00 48 8D 6C 24 30");
    const auto name_string=image.find_unique("name_pool_reference",
        "48 89 5C 24 10 48 89 74 24 18 57 48 83 EC 30 83 79 04 00 "
        "48 8B FA 8B 19 48 8B F1 0F 85 ?? ?? ?? ?? 80 3D ?? ?? ?? ?? 00 "
        "74 09 4C 8D 05 ?? ?? ?? ?? EB 16 48 8D 0D ?? ?? ?? ??");
    kNames=image.rip_target(name_string+42,3,7);
    const auto object_walk=image.find_unique("object_registry_reference",
        "40 53 48 83 EC 40 48 8D 15 ?? ?? ?? ?? 48 8D 4C 24 20 E8 ?? ?? ?? ?? "
        "44 8B 05 ?? ?? ?? ?? 33 DB 45 85 C0 0F 8E ?? ?? ?? ?? "
        "4C 8B 0D ?? ?? ?? ?? 48 89 7C 24 50");
    kObjects=image.rip_target(object_walk+41,3,7);
    const auto compilation_lock=image.find_unique("angelscript_manager_lock",
        "48 8B 35 ?? ?? ?? ?? 80 BE ?? ?? ?? ?? 00 0F 85 ?? ?? ?? ?? "
        "48 89 9C 24 C8 01 00 00 48 8D 9E ?? ?? ?? ?? "
        "4C 89 A4 24 D0 01 00 00 48 8B CB");
    kAngelscriptManager=image.rip_target(compilation_lock,3,7);
    kCompilationLockOffset=read<int32_t>(candidate_base+compilation_lock+31);
    kFormatDiagnostics=image.find_unique("angelscript_diagnostics",
        "48 89 5C 24 18 55 56 57 41 54 41 55 41 56 41 57 "
        "48 8D 6C 24 D9 48 81 EC 00 01 00 00 45 33 F6 C7 44 24 34 01 00 00 00 "
        "48 8D 99 ?? ?? ?? ?? 4C 89 32");
    kASExceptionLogger=image.find_unique("angelscript_exception_logger",
        "48 81 EC C8 01 00 00 48 8B 05 ?? ?? ?? ?? 48 33 C4 "
        "48 89 84 24 A0 01 00 00 48 8B 05 ?? ?? ?? ?? "
        "48 8B 90 ?? ?? ?? ?? 48 85 D2");
    kASReload=image.find_unique("angelscript_reload",
        "48 89 5C 24 10 55 56 57 41 54 41 55 41 56 41 57 "
        "48 8D AC 24 B0 FB FF FF 48 81 EC 50 05 00 00 "
        "48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 85 40 04 00 00 "
        "0F B6 05 ?? ?? ?? ?? 44 0F B6 EA");
    kASStackTrace=image.find_unique("angelscript_stack_trace",
        "40 55 53 57 41 55 41 56 48 8D AC 24 A0 FC FF FF 48 81 EC 60 04 00 00");
    kFree=image.find_unique("engine_free",
        "48 85 C9 74 2E 53 48 83 EC 20 48 8B D9 48 8B 0D ?? ?? ?? ?? "
        "48 85 C9 75 0C E8 ?? ?? ?? ?? 48 8B");
    kObjectArrayRealloc=image.find_unique("engine_realloc",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 "
        "48 8B F1 41 8B D8 48 8B 0D ?? ?? ?? ?? 48 8B FA");
    kStringConstruct=image.find_unique("string_constructor",
        "48 89 6C 24 18 56 41 56 41 57 48 83 EC 20 45 33 FF "
        "49 63 E8 4C 89 39 4C 8B F2 4C 89 79 08");
    kNameConstruct=image.find_unique("name_constructor",
        "40 53 48 83 EC 30 45 33 D2 48 89 54 24 20 48 8B D9 48 8B C2 "
        "48 85 D2 74 2C 44 0F B7 0A 66 45 85 C9");
    kMoviePlayerWait=image.find_unique("movieplayer_wait",
        "40 55 53 41 56 41 57 48 8D AC 24 18 FF FF FF 48 81 EC E8 01 00 00 "
        "48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 85 80 00 00 00 "
        "48 8B 01 0F 57 C0 0F 2F 81 C8 00 00 00");
    kPlatformPumpMessages=image.find_unique("platform_message_pump",
        "48 81 EC 88 00 00 00 0F B6 05 ?? ?? ?? ?? 84 C0");
    kMoviePlayerLoadingFinished=image.find_unique("movieplayer_loading_finished",
        "33 D2 33 C0 F0 0F B1 91 9C 00 00 00 0F 95 C0 C3");
    kMoviePlayerPlaying=image.find_unique("movieplayer_playing",
        "48 83 B9 90 00 00 00 00 75 0C 80 B9 A0 00 00 00 00 75 03 32 C0 C3 B0 01 C3");
    kMoviePlayerStop=image.find_unique("movieplayer_stop",
        "48 C7 81 50 01 00 00 00 00 00 00 C6 81 A1 00 00 00 01 C3");
    const std::array<uintptr_t,4> movieplayer_entries={candidate_base+kMoviePlayerStop,
        candidate_base+kMoviePlayerWait,candidate_base+kMoviePlayerLoadingFinished,candidate_base+kMoviePlayerPlaying};
    const auto movieplayer_table=image.unique("movieplayer_interface_table",
        std::span<const uint8_t>{reinterpret_cast<const uint8_t*>(movieplayer_entries.data()),sizeof(movieplayer_entries)},
        std::string(sizeof(movieplayer_entries),'x'),false);
    kMoviePlayerInterfaceVtable=movieplayer_table-0x50;
    base_=candidate_base;
    return base_;
}
bool GameAccess::start() {
    if(enabled_) return true;
    try {ensure_image_base();}
    catch(const HandlerError& error) {startup_error_=error.code();return false;}
    auto initialized=MH_Initialize();
    if(initialized!=MH_OK && initialized!=MH_ERROR_ALREADY_INITIALIZED) {startup_error_="hook_initialize_failed";return false;}
    active_=this;process_event_=reinterpret_cast<ProcessEventFn>(base_+kProcessEvent);
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kASExceptionLogger),reinterpret_cast<void*>(&hooked_as_exception_logger),
                     reinterpret_cast<void**>(&original_as_exception_logger_))!=MH_OK) {startup_error_="as_exception_hook_failed";return false;}
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kASExceptionLogger))!=MH_OK) {
        MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));startup_error_="as_exception_hook_failed";return false;
    }
    diagnostic("hook_create_enter");
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kASStackTrace),reinterpret_cast<void*>(&hooked_as_stack_trace),
                     reinterpret_cast<void**>(&original_as_stack_trace_))!=MH_OK) {
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        startup_error_="as_stack_hook_failed";return false;
    }
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kASStackTrace))!=MH_OK) {
        MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        startup_error_="as_stack_hook_failed";return false;
    }
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kTick),reinterpret_cast<void*>(&hooked_tick),reinterpret_cast<void**>(&original_tick_))!=MH_OK) {
        MH_DisableHook(reinterpret_cast<void*>(base_+kASStackTrace));MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        startup_error_="tick_hook_failed";return false;
    }
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kTick))!=MH_OK) {
        MH_DisableHook(reinterpret_cast<void*>(base_+kASStackTrace));MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_RemoveHook(reinterpret_cast<void*>(base_+kTick));MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));startup_error_="tick_hook_failed";return false;
    }
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kASReload),reinterpret_cast<void*>(&hooked_as_reload),
           reinterpret_cast<void**>(&original_as_reload_))!=MH_OK) {
        MH_DisableHook(reinterpret_cast<void*>(base_+kTick));MH_RemoveHook(reinterpret_cast<void*>(base_+kTick));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASStackTrace));MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        startup_error_="script_reload_hook_failed";return false;
    }
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kASReload))!=MH_OK) {
        MH_RemoveHook(reinterpret_cast<void*>(base_+kASReload));
        MH_DisableHook(reinterpret_cast<void*>(base_+kTick));MH_RemoveHook(reinterpret_cast<void*>(base_+kTick));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASStackTrace));MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
        startup_error_="script_reload_hook_failed";return false;
    }
    script_reload_hook_installed_=true;
    bool movie_wait_created=false,platform_pump_created=false;
    const auto undo_movieplayer_installation=[&] {
        MH_DisableHook(reinterpret_cast<void*>(base_+kASReload));MH_RemoveHook(reinterpret_cast<void*>(base_+kASReload));script_reload_hook_installed_=false;
        if(platform_pump_created){MH_DisableHook(reinterpret_cast<void*>(base_+kPlatformPumpMessages));MH_RemoveHook(reinterpret_cast<void*>(base_+kPlatformPumpMessages));}
        if(movie_wait_created){MH_DisableHook(reinterpret_cast<void*>(base_+kMoviePlayerWait));MH_RemoveHook(reinterpret_cast<void*>(base_+kMoviePlayerWait));}
        MH_DisableHook(reinterpret_cast<void*>(base_+kTick));MH_RemoveHook(reinterpret_cast<void*>(base_+kTick));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASStackTrace));MH_RemoveHook(reinterpret_cast<void*>(base_+kASStackTrace));
        MH_DisableHook(reinterpret_cast<void*>(base_+kASExceptionLogger));MH_RemoveHook(reinterpret_cast<void*>(base_+kASExceptionLogger));
    };
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kMoviePlayerWait),reinterpret_cast<void*>(&hooked_movieplayer_wait),
                     reinterpret_cast<void**>(&original_movieplayer_wait_))!=MH_OK) {
        undo_movieplayer_installation();startup_error_="movieplayer_wait_hook_failed";return false;
    }
    movie_wait_created=true;
    if(MH_CreateHook(reinterpret_cast<void*>(base_+kPlatformPumpMessages),reinterpret_cast<void*>(&hooked_platform_pump_messages),
                     reinterpret_cast<void**>(&original_platform_pump_messages_))!=MH_OK) {
        undo_movieplayer_installation();startup_error_="movieplayer_message_pump_hook_failed";return false;
    }
    platform_pump_created=true;
    if(MH_EnableHook(reinterpret_cast<void*>(base_+kMoviePlayerWait))!=MH_OK ||
       MH_EnableHook(reinterpret_cast<void*>(base_+kPlatformPumpMessages))!=MH_OK) {
        undo_movieplayer_installation();startup_error_="movieplayer_hook_enable_failed";return false;
    }
    movieplayer_hooks_installed_=true;
    diagnostic("hook_enabled");enabled_=true;return true;
}
bool __fastcall GameAccess::hooked_as_reload(void* manager,std::uint8_t mode,void* changed_files) {
    auto self=active_;
    const auto attempt_id=++self->script_reload_attempts_started_;
    const auto generation_before=self->successful_script_reloads_.load();
    json receipt;
    try {
        receipt={{"changed_files",json::array()},{"capture_complete",false}};
        capture_reload_changed_files(changed_files,receipt["changed_files"]);
        receipt["capture_complete"]=true;
    } catch(const HandlerError& error) {
        try {receipt["capture_error"]={{"code",error.code()},{"message",error.what()}};}catch(...) {}
    } catch(const std::exception& error) {
        try {receipt["capture_error"]={{"code","reload_capture_failed"},{"message",error.what()}};}catch(...) {}
    } catch(...) {
        try {receipt["capture_error"]={{"code","reload_capture_failed"},{"message","Could not capture changed-file metadata"}};}catch(...) {}
    }
    const bool result=self->original_as_reload_(manager,mode,changed_files);
    if(result)++self->successful_script_reloads_;else ++self->failed_script_reloads_;
    const auto completion_id=++self->script_reload_attempts_completed_;
    try {
        const bool capture_complete=receipt.is_object() && receipt.value("capture_complete",false);
        receipt.update({{"attempt_id",attempt_id},{"completion_id",completion_id},{"mode",mode},{"success",result},
            {"script_generation_before",generation_before},
            {"script_generation_after",self->successful_script_reloads_.load()},{"capture_complete",capture_complete}});
        if(!receipt.contains("changed_files"))receipt["changed_files"]=json::array();
        std::lock_guard lock(self->script_reload_receipt_mutex_);
        if(self->last_completed_script_reload_.is_null() ||
           self->last_completed_script_reload_.value("completion_id",std::uint64_t{})<completion_id)
            self->last_completed_script_reload_=std::move(receipt);
    } catch(...) {
        // A failed bridge capture must not change the engine reload result.
    }
    return result;
}
void __fastcall GameAccess::hooked_as_exception_logger(const char* message) {
    as_exception_capture::LoggerScope logger;
    as_exception_capture::record(message,[](uintptr_t address,char& value) {
        return copy_memory(&value,reinterpret_cast<const void*>(address),sizeof(value));
    });
    active_->original_as_exception_logger_(message);
}
void __fastcall GameAccess::hooked_as_stack_trace(void* output) {
    active_->original_as_stack_trace_(output);
    if(!as_exception_capture::active_logger || !as_exception_capture::active ||
       as_exception_capture::active_logger->owner()!=as_exception_capture::active)return;
    std::vector<std::string> frames;
    bool complete=false;
    try {
        ArrayHeader header{};
        if(!copy_memory(&header,output,sizeof(header)))throw HandlerError("invalid_memory","Stack memory is unavailable");
        if(header.count<0 || header.capacity<header.count ||
           (!header.data && (header.count || header.capacity)) ||
           (header.data && (!header.capacity || header.data%alignof(StringHeader))))
            throw HandlerError("invalid_array_header","Invalid stack array");
        const auto capacity_bytes=static_cast<uintptr_t>(header.capacity)*sizeof(StringHeader);
        if(header.data>std::numeric_limits<uintptr_t>::max()-capacity_bytes)
            throw HandlerError("invalid_array_header","The stack address range overflows");
        frames.reserve(static_cast<size_t>(header.count));
        for(int32_t index=0;index<header.count;++index) {
            StringHeader frame{};
            if(!copy_memory(&frame,reinterpret_cast<const void*>(header.data+static_cast<uintptr_t>(index)*sizeof(frame)),sizeof(frame)))
                throw HandlerError("invalid_memory","Stack frame memory is unavailable");
            frames.push_back(copied_string(frame));
        }
        complete=true;
    } catch(...) {}
    as_exception_capture::record_stack(std::move(frames),complete);
}
void __fastcall GameAccess::hooked_tick(void* loop) {
    static std::atomic<int> reports{0};auto count=reports.fetch_add(1);
    if(count<2)diagnostic("tick_enter");
    auto self=active_; if(!self) return;
    self->original_tick_(loop);
    if(count<2)diagnostic("tick_original_return");
    if(self->enabled_) self->pump();
    if(count<2)diagnostic("tick_pump_return");
}
bool GameAccess::movieplayer_flag(uintptr_t player,std::size_t slot,uintptr_t expected_rva) const {
    uintptr_t table{},function{};
    if(!copy_memory(&table,reinterpret_cast<void*>(player),sizeof(table)))
        throw HandlerError("movieplayer_memory_unavailable","MoviePlayer interface memory is unavailable");
    if(table!=base_+kMoviePlayerInterfaceVtable || !copy_memory(&function,reinterpret_cast<void*>(table+slot),sizeof(function))
       || function!=base_+expected_rva)
        throw HandlerError("movieplayer_interface_mismatch","MoviePlayer interface or method differs from the confirmed executable");
    bool value=false;native_exception::Diagnostic fault{};
    if(!safe_movieplayer_flag(base_+expected_rva,player,&value,&fault))
        throw HandlerError("movieplayer_read_failed","MoviePlayer status call raised a native exception",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},{"native_exception",native_exception::describe(fault)}});
    return value;
}
void GameAccess::movieplayer_stop(uintptr_t player) const {
    uintptr_t table{},function{};
    if(!copy_memory(&table,reinterpret_cast<void*>(player),sizeof(table)))
        throw HandlerError("movieplayer_memory_unavailable","MoviePlayer interface memory is unavailable");
    if(table!=base_+kMoviePlayerInterfaceVtable || !copy_memory(&function,reinterpret_cast<void*>(table+0x50),sizeof(function))
       || function!=base_+kMoviePlayerStop)
        throw HandlerError("movieplayer_interface_mismatch","MoviePlayer StopMovie method differs from the confirmed executable");
    native_exception::Diagnostic fault{};
    if(!safe_movieplayer_stop(base_+kMoviePlayerStop,player,&fault))
        throw HandlerError("movieplayer_stop_failed","MoviePlayer StopMovie call raised a native exception",
            {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"native_exception",native_exception::describe(fault)}});
}
void __fastcall GameAccess::hooked_movieplayer_wait(void* player,bool allow_engine_tick) {
    auto* self=active_;if(!self)return;
    MoviePlayerJobs::WaitFrame* frame=nullptr;
    try {frame=new MoviePlayerJobs::WaitFrame(self->movieplayer_jobs_,reinterpret_cast<uintptr_t>(player),
              GetCurrentThreadId(),allow_engine_tick,steady_ms());}
    catch(...) {diagnostic("movieplayer_wait_frame_failed");}
    movieplayer_wait_with_frame(reinterpret_cast<uintptr_t>(self->original_movieplayer_wait_),player,allow_engine_tick,frame);
}
void __fastcall GameAccess::hooked_platform_pump_messages(bool from_main_loop) {
    auto* self=active_;if(!self)return;
    self->original_platform_pump_messages_(from_main_loop);
    if(!self->movieplayer_hooks_installed_)return;
    try {
        self->movieplayer_jobs_.sample_and_service(GetCurrentThreadId(),steady_ms(),
            [self](uintptr_t player){return self->movieplayer_flag(player,0x60,kMoviePlayerLoadingFinished);},
            [self](uintptr_t player){return self->movieplayer_flag(player,0x68,kMoviePlayerPlaying);},
            [self](uintptr_t player){self->movieplayer_stop(player);});
    } catch(...) {diagnostic("movieplayer_service_callback_setup_failed");}
}
json GameAccess::movieplayer_status(const std::string& session) const {
    auto result=movieplayer_jobs_.cached_status();
    result["hooks_installed"]=movieplayer_hooks_installed_.load();
    result["wait_hook_installed"]=movieplayer_hooks_installed_.load();
    result["message_pump_hook_installed"]=movieplayer_hooks_installed_.load();
    result["adapter_source"]="movieplayer_wait_message_pump";
    result["game_calls_on_status"]=false;
    const auto observed=last_tick_ms_.load(),now=steady_ms();
    result["ordinary_tick_ready"]=enabled_.load() && observed && now>=observed && now-observed<=kRequestWaitMs;
    result["session_id"]=session.empty()?json(nullptr):json(session);
    result["process_id"]=GetCurrentProcessId();result["protocol"]=1;
    return result;
}
json GameAccess::request_movieplayer_finish(const json& envelope) {
    if(!movieplayer_hooks_installed_)
        throw HandlerError("movieplayer_adapter_not_ready","MoviePlayer hooks are not installed");
    const auto wait=request_wait(envelope);
    const auto& params=envelope.at("params");
    if(!params.contains("expected_wait_id") || !params.at("expected_wait_id").is_string()
       || params.at("expected_wait_id").get_ref<const std::string&>().empty())
        throw HandlerError("invalid_movieplayer_request","A nonempty expected_wait_id string is required");
    auto job=movieplayer_jobs_.request_finish(envelope.at("session_id").get<std::string>(),params.at("expected_wait_id").get<std::string>());
    auto future=job->result.get_future();
    const auto completed=[&] {
        auto result=future.get();
        result["session_id"]=envelope.at("session_id");result["process_id"]=GetCurrentProcessId();result["protocol"]=1;
        return result;
    };
    if(future.wait_for(wait)!=std::future_status::ready) {
        if(movieplayer_jobs_.cancel_finish(job))
            throw HandlerError("movieplayer_not_servicing","MoviePlayer request was cancelled before its wait-thread service started",
                {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},{"request_started",false},
                 {"stage","queued_cancelled"},{"movieplayer_status",movieplayer_status(envelope.at("session_id").get<std::string>())}});
        if(future.wait_for(std::chrono::milliseconds(0))==std::future_status::ready)return completed();
        if(job->state.get()==JobState::cancelled)
            throw HandlerError("session_closed","MoviePlayer request was cancelled before execution after its session closed");
        throw HandlerError("movieplayer_timeout","MoviePlayer finish request started; its outcome is not yet known",
            {{"executed",false},{"may_have_executed",true},{"do_not_retry",true},{"request_started",true},
             {"stage","running_outcome_unknown"},{"movieplayer_status",movieplayer_status(envelope.at("session_id").get<std::string>())}});
    }
    return completed();
}
json GameAccess::status() const {
    const auto observed=last_tick_ms_.load(), now=steady_ms();
    const auto age=observed && now>=observed ? now-observed : 0;
    const bool recent=observed && age<=kRequestWaitMs;
    json pending=nullptr, active=nullptr, world=nullptr;bool queue_metadata_available{};
    {
        std::unique_lock lock(queue_mutex_,std::try_to_lock);
        queue_metadata_available=lock.owns_lock();
        if(queue_metadata_available)world=world_metadata_;
        if(queue_metadata_available)pending=queue_.size();
        if(queue_metadata_available && active_job_)active={{"request_id",active_job_->request.value("id",json(nullptr))},
                              {"operation",active_job_->request.value("op",std::string{})},
                              {"state","running"}};
    }
    return {{"version","1.0.0-dev.115"},{"native_route","standalone_in_process"},{"hook_installed",enabled_.load()},
            {"movieplayer",movieplayer_status({})},
            {"as_exception_capture",{{"hook_installed",enabled_.load()},{"source","observed_logging_entry"},
                {"scope","current_thread_process_event"},{"historical_errors_available",false},{"stack_capture_available",enabled_.load()},{"stack_captured",false}}},
            {"game_thread_id",game_thread_.load()},{"tick_count",ticks_.load()},{"ready",enabled_.load()&&recent},
            {"pump",{{"state",!observed?"not_observed":recent?"recent_tick":"tick_stale"},
                     {"last_tick_monotonic_ms",observed?json(observed):json(nullptr)},
                     {"tick_age_ms",observed?json(age):json(nullptr)},
                     {"freshness_limit_ms",kRequestWaitMs},{"world_ready_confirmed",false}}},
            {"requests",{{"queue_depth",pending},{"queue_limit",nullptr},{"active",active},{"metadata_available",queue_metadata_available},
                         {"completed",completed_jobs_.load()},{"cancelled_before_start",cancelled_jobs_.load()},
                         {"wait_timeout_ms",nullptr},{"default_wait_timeout_ms",kRequestWaitMs},{"cancelled_jobs_can_execute",false}}},
            {"bindings_source","runtime_image_patterns"},{"startup_error",startup_error_},
            {"process_id",GetCurrentProcessId()},
            {"string_inputs",{{"quarantined",string_inputs_quarantined_.load()},
                {"quarantine_cause",string_inputs_quarantine_cause_.load()},
                {"retained_allocations",string_inputs_retained_count_.load()},
                {"retained_bytes",string_inputs_retained_bytes_.load()},
                {"max_inputs",nullptr},{"max_utf16_units",string_arguments::max_utf16_units},{"max_input_bytes",nullptr}}},
            {"object_array_inputs",{{"quarantined",object_array_inputs_quarantined_.load()},
                {"quarantine_cause",object_array_inputs_quarantine_cause_.load()},
                {"retained_allocations",object_array_inputs_retained_count_.load()},
                {"retained_bytes",object_array_inputs_retained_bytes_.load()},
                {"max_inputs",nullptr},{"max_elements",std::numeric_limits<int32_t>::max()},{"max_input_bytes",nullptr}}},
            {"operations",{"status","enable_game","compile_diagnostics","movieplayer_status","movieplayer_finish","objects","properties","functions","invoke","read_array","read_set","read_map","read_struct",
                           "snapshot","watch_start","watch_read","watch_stop","watch_forget","watch_list","watch_resume"}},
            {"snapshot_limits",{{"objects",nullptr},{"properties_per_object",nullptr},{"scan_fields_per_object",nullptr},
                                {"property_value_bytes",nullptr},{"object_path_bytes",nullptr},
                                {"snapshot_bytes",nullptr}}},
            {"watch_limits",{{"records",nullptr},{"properties",nullptr},
                             {"duration_ms_min",1},{"duration_ms_max",nullptr},
                             {"interval_ms_min",1},{"interval_ms_max",nullptr},
                             {"capacity_min",1},{"capacity_max",nullptr},
                             {"buffer_bytes",nullptr},{"read_events",nullptr},
                             {"due_watches_per_tick",nullptr}}},
            {"snapshot_consistency","single_tick_sequential"},
            {"world_generation_supported",true},{"world",world},{"script_reload_generation_supported",true},
            {"scripts",{{"source","observed_native_reload_return"},{"hook_installed",script_reload_hook_installed_.load()},
                {"generation",successful_script_reloads_.load()},{"failed_attempts",failed_script_reloads_.load()},
                {"generation_semantics","successful_reload_attempt_completed"},{"specific_source_compiled_confirmed",false}}},
            {"serial_guard",{{"policy","strict_observed_equality_including_zero"},
                              {"checks",{"object_serial","class_identity","class_serial"}},
                              {"coverage","conditional_on_observed_nonzero_object_and_class_serial"},
                              {"allocates_serial",false},{"zero_aba_protection",false}}},
            {"limitations",{"Native binding requires uniquely resolved compatible image patterns","Zero serial values do not protect against ABA; an observed serial change invalidates the previous handle","One active connection; handles and observations belong to its session","World generation follows GameEngine.GameViewport.World identity; script generation counts successful reload attempts and does not prove a particular source changed","The DLL cannot be unloaded; close the game to terminate it"}}};
}
json GameAccess::compile_diagnostics() {
    const auto image_base=ensure_image_base();
    const auto manager=read<uintptr_t>(image_base+kAngelscriptManager);
    if(!manager)throw HandlerError("compile_diagnostics_unavailable","The AngelScript manager has not been created");
    auto* compilation_lock=reinterpret_cast<CRITICAL_SECTION*>(manager+kCompilationLockOffset);
    std::string text;
    std::uint64_t started_before{},completed_before{},started_after{},completed_after{};
    json reload_receipt;
    EnterCriticalSection(compilation_lock);
    try {
        started_before=script_reload_attempts_started_.load();
        completed_before=script_reload_attempts_completed_.load();
        StringHeader header{};
        using Formatter=StringHeader*(__fastcall*)(void*,StringHeader*);
        reinterpret_cast<Formatter>(image_base+kFormatDiagnostics)(reinterpret_cast<void*>(manager),&header);
        text=copied_owned_string_return(header,[](const StringHeader& value) {
            std::wstring copied(static_cast<size_t>(value.count),L'\0');
            const auto bytes=static_cast<size_t>(value.count)*sizeof(wchar_t);
            size_t offset=0;
            while(offset<bytes) {
                const auto count=std::min<size_t>(bytes-offset,1048576);
                if(!copy_memory(reinterpret_cast<std::byte*>(copied.data())+offset,
                                reinterpret_cast<const void*>(value.data+offset),count))
                    throw HandlerError("invalid_memory","Diagnostic text is unavailable");
                offset+=count;
            }
            if(!copied.empty() && copied.back()==0)copied.pop_back();
            return utf8(copied.data(),static_cast<int>(copied.size()));
        },[image_base](uintptr_t data) {
            return free_string(image_base+kFree,reinterpret_cast<void*>(data));
        });
        {
            std::lock_guard lock(script_reload_receipt_mutex_);
            reload_receipt=last_completed_script_reload_;
            started_after=script_reload_attempts_started_.load();
            completed_after=script_reload_attempts_completed_.load();
        }
        LeaveCriticalSection(compilation_lock);
    } catch(...) {
        LeaveCriticalSection(compilation_lock);
        throw;
    }
    const bool receipt_current=completed_after==0 ? reload_receipt.is_null() :
        reload_receipt.is_object() && reload_receipt.value("completion_id",std::uint64_t{})==completed_after;
    const bool reload_context_consistent=started_before==started_after && completed_before==completed_after &&
        started_after==completed_after && receipt_current;
    return {{"status","available"},{"source","FAngelscriptManager::FormatDiagnostics"},
            {"process_id",GetCurrentProcessId()},{"text",std::move(text)},
            {"reload_attempt_started",started_after},{"reload_attempt_completed",completed_after},
            {"compiler_read_reload_context_consistent",reload_context_consistent},
            {"last_completed_reload",std::move(reload_receipt)}};
}
json GameAccess::request(const json& envelope) {
    if(envelope.at("op")=="status") return status();
    if(envelope.at("op")=="compile_diagnostics") return compile_diagnostics();
    if(envelope.at("op")=="movieplayer_status")return movieplayer_status(envelope.at("session_id").get<std::string>());
    if(envelope.at("op")=="movieplayer_finish")return request_movieplayer_finish(envelope);
    if(!enabled_) throw HandlerError("native_not_ready","Game hooks are not installed: "+startup_error_);
    const auto wait=request_wait(envelope);
    auto job=std::make_shared<Job>();job->request=envelope;auto future=job->result.get_future();
    {std::lock_guard lock(queue_mutex_);
     job->world_generation=world_generation_.load();job->script_generation=successful_script_reloads_.load();queue_.push_back(job);}
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
    movieplayer_jobs_.disconnect(session);
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
void GameAccess::pump() {
    if(!game_thread_.load()) game_thread_=GetCurrentThreadId();
    if(game_thread_.load()!=GetCurrentThreadId()) return;
    ++ticks_;
    last_tick_ms_=steady_ms();
    refresh_scripts();
    try {refresh_world();}
    catch(const HandlerError& error) {
        change_world({});
        std::lock_guard lock(queue_mutex_);
        world_metadata_["error"]={{"code",error.code()},{"message",error.what()}};
    }
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
            try {job->result.set_value(execute(job->request));}catch(...) {job->result.set_exception(std::current_exception());}
            job->state.finish();++completed_jobs_;
            {std::lock_guard lock(queue_mutex_);active_job_.reset();}
        }
    }
    refresh_scripts();
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
bool GameAccess::script_object(void* object) const {
    if(read<uint32_t>(reinterpret_cast<uintptr_t>(object)+8)&0x10) {
        const auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);
        if(!class_is(reinterpret_cast<void*>(klass),"/Script/AngelscriptCode.ASClass"))return false;
    }
    std::set<uintptr_t> seen;
    if(class_is(object,"/Script/AngelscriptCode.ASClass"))return true;
    if(class_is(object,"/Script/CoreUObject.Class")) {
        for(auto represented=reinterpret_cast<uintptr_t>(object);represented;represented=read<uintptr_t>(represented+0x40)) {
            if(!seen.insert(represented).second)throw HandlerError("invalid_object_class","The represented class hierarchy contains a cycle");
            if(class_is(reinterpret_cast<void*>(represented),"/Script/AngelscriptCode.ASClass"))return true;
        }
        seen.clear();
    }
    for(auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!seen.insert(klass).second)throw HandlerError("invalid_object_class","The class hierarchy contains a cycle");
        if(class_is(reinterpret_cast<void*>(klass),"/Script/AngelscriptCode.ASClass"))return true;
    }
    return class_is(object,"/Script/AngelscriptCode.ASFunction");
}
void GameAccess::refresh_scripts() {
    const auto generation=successful_script_reloads_.load();
    if(generation==script_generation_)return;
    std::lock_guard lock(queue_mutex_);
    const auto previous=script_generation_;script_generation_=generation;
    std::vector<std::string> affected;
    for(const auto& [id,handle]:handles_)
        if(handle.script_bound && handle.script_generation!=generation)affected.push_back(id);
    observations_.invalidate_script(affected,previous,generation,steady_ms(),ticks_.load());
    for(auto entry=queue_.begin();entry!=queue_.end();) {
        const auto& job=*entry;
        const auto references_script=[&](const auto& self,const json& value)->bool {
            if(value.is_object() && value.contains("handle") && value.at("handle").is_string()) {
                const auto found=handles_.find(value.at("handle").get<std::string>());
                if(found!=handles_.end() && found->second.script_bound)return true;
            }
            if(value.is_structured())for(const auto& child:value)if(self(self,child))return true;
            return false;
        };
        if(job->script_generation!=generation && references_script(references_script,job->request.at("params")) && job->state.cancel()) {
            ++cancelled_jobs_;
            job->result.set_exception(std::make_exception_ptr(HandlerError("script_reloaded","The script context changed before the request executed",
                {{"executed",false},{"may_have_executed",false},{"request_started",false},{"stage","queued_cancelled"},
                 {"previous_script_generation",job->script_generation},{"script_generation",generation}})));
            entry=queue_.erase(entry);
        } else ++entry;
    }
}
std::string GameAccess::handle_for(void* object,int index) {
    auto identity=validate_object(object);
    if(identity.index!=index)throw HandlerError("stale_handle","The object index changed");
    identity.bound_world=object_world(object);
    identity.world_generation=identity.bound_world?world_generation_.load():0;
    identity.script_bound=script_object(object);identity.script_generation=identity.script_bound?successful_script_reloads_.load():0;
    for(const auto& [id,existing]:handles_) {
        if(existing.world_generation==identity.world_generation && existing.bound_world==identity.bound_world
           && existing.script_bound==identity.script_bound && existing.script_generation==identity.script_generation
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
    if(read<uint32_t>(ptr+8)&0x18000)throw HandlerError("object_destroying","The object is being destroyed");
    const auto object_class=read<uintptr_t>(ptr+0x10);
    if(!object_class)throw HandlerError("invalid_object_class","The object has no class");
    const auto class_index=read<int32_t>(object_class+0xc);
    const auto class_item=slot_identity(class_index);
    if(class_index<0 || class_item.object!=reinterpret_cast<void*>(object_class)
       || (read<uint32_t>(object_class+8)&0x18000))
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
    if((read<uint32_t>(ptr+8)|read<uint32_t>(object_class+8))&0x18000)
        throw HandlerError("object_destroying","The object or its class is being destroyed");
    return {object,reinterpret_cast<void*>(object_class),index,object_fname,item.serial,class_index,class_fname,class_item.serial};
}
void GameAccess::validate_class(void* object_class) const {
    // Exact metaclass identity was confirmed by a read-only probe in the current game:
    // /Script/CoreUObject.Class (index 15, class Class), 30.10.2026.
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
    result["script_generation"]=reference.script_bound?json(reference.script_generation):json(nullptr);
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
        const auto bytes=g1r::enum_values::table_byte_size(count,capacity,data!=0);
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
        auto entries=g1r::enum_values::decode_entries(copied,count,capacity);
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
        const g1r::enum_values::Integer number=value.is_number_unsigned()
            ?g1r::enum_values::Integer(value.get<uint64_t>()):g1r::enum_values::Integer(value.get<int64_t>());
        const auto match=g1r::enum_values::find_label(number,table.entries,[this](uint64_t fname){return name(fname);});
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
        g1r::enum_values::integer_layout(underlying_type,size);
        return {underlying_type,size,reinterpret_cast<void*>(enum_ptr)};
    } catch(const std::invalid_argument& error) {
        throw HandlerError(error.what(),"Unsupported enum layout");
    }
}
json GameAccess::enum_property(uintptr_t address,const std::string& type,int size,uintptr_t field) {
    namespace ev=g1r::enum_values;
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
    if(h.script_bound && h.script_generation!=successful_script_reloads_.load())
        throw HandlerError("script_reloaded","The handle belongs to a previous script context; obtain a new handle",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},
             {"previous_script_generation",h.script_generation},{"script_generation",successful_script_reloads_.load()}});
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
        {"script_generation",reference.script_bound?json(reference.script_generation):json(nullptr)}};
}
json GameAccess::properties(void* object,std::size_t limit) {
    json items=json::array();auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);bool truncated=false;
    std::set<uintptr_t> visited_classes,visited_fields;
    for(;klass;klass=read<uintptr_t>(klass+0x40)) {
        if(!visited_classes.insert(klass).second)
            throw HandlerError("property_scan_cycle","Class chain contains a cycle");
        auto field=read<uintptr_t>(klass+0x50);auto class_size=read<int32_t>(klass+0x58);
        for(;field;field=read<uintptr_t>(field+0x18)) {
            if(items.size()>=limit){truncated=true;break;}
            if(!visited_fields.insert(field).second)
                throw HandlerError("property_scan_cycle","Property chain contains a cycle");
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
    return {{"items",items},{"truncated",truncated},{"validation","game_thread_memory_read"}};
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
        {"script_generation",successful_script_reloads_.load()},
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
            if(value.size()!=1 || !value.at("name").is_string())
                throw HandlerError("invalid_name_argument","Text FName requires only {name:string}");
            const auto& text=value.at("name").get_ref<const std::string&>();
            if(text.find('\0')!=std::string::npos)
                throw HandlerError("invalid_name_argument","Text FName cannot contain an embedded NUL");
            std::wstring encoded;
            if(!text.empty()) {
                const auto length=static_cast<int>(text.size());
                const auto units=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),length,nullptr,0);
                if(!units)throw HandlerError("invalid_name_argument","Text FName contains invalid UTF-8");
                encoded.resize(static_cast<size_t>(units));
                if(MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,text.data(),length,encoded.data(),units)!=units)
                    throw HandlerError("invalid_name_argument","Text FName could not be converted to UTF-16");
            }
            uint64_t packed{};
            if(!construct_name(base_+kNameConstruct,&packed,reinterpret_cast<const char16_t*>(encoded.c_str())))
                throw HandlerError("name_constructor_fault","The FName constructor raised a native exception");
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
    result.layout={read<int32_t>(result.structure+0x58),read<int32_t>(result.structure+0x5c),result.super!=0,{}};
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
        {"script_generation",(handles_.at(target_handle).script_bound || class_is(reinterpret_cast<void*>(function),"/Script/AngelscriptCode.ASFunction"))?json(successful_script_reloads_.load()):json(nullptr)},
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
json GameAccess::functions(void* object,std::size_t limit,const std::string& query,const std::string& target_handle) {
    json items=json::array();auto klass=read<uintptr_t>(reinterpret_cast<uintptr_t>(object)+0x10);
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
            if(items.size()>=limit){truncated=true;break;}
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
            }catch(const HandlerError& error){item["signature_status"]=error.code();}
            items.push_back(std::move(item));
            if(parameters_truncated)scan_truncated=true;
        }
        if(truncated || scan_truncated)break;
    }
    return {{"items",items},{"truncated",truncated || scan_truncated},{"scan_truncated",scan_truncated},
            {"scan_complete",!truncated && !scan_truncated}};
}
json GameAccess::invoke(void* object,const std::string& function_name,const json& arguments,
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
    const auto script_reference=[&](const auto& self,const json& value)->bool {
        if(value.is_object() && value.contains("handle") && value.at("handle").is_string()) {
            const auto found=handles_.find(value.at("handle").get<std::string>());
            if(found!=handles_.end() && found->second.script_bound)return true;
        }
        if(value.is_structured())for(const auto& child:value)if(self(self,child))return true;
        return false;
    };
    const bool script_call=handles_.at(target_handle).script_bound || class_is(reinterpret_cast<void*>(function),"/Script/AngelscriptCode.ASFunction")
        || script_reference(script_reference,arguments);
    const auto call_script_generation=successful_script_reloads_.load();
    if(active_job_ && active_job_->script_generation!=successful_script_reloads_.load()
       && script_call)
        throw HandlerError("script_reloaded","The script context changed after the call was queued",
            {{"executed",false},{"may_have_executed",false},{"request_started",false},
             {"previous_script_generation",active_job_->script_generation},{"script_generation",successful_script_reloads_.load()}});
    if(expected_token) {
        if(expected_contract.at("function").at("object")!=function || expected_contract.at("owner").at("object")!=function_owner)
            throw HandlerError("signature_token_mismatch","The token belongs to a different implementation of the same function name");
        require_same_function_contract(expected_contract,capture_function_contract(object,function_owner,function,target_handle));
    }
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
                         g1r::enum_values::Integer value;std::optional<Handle> enum_identity;};
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
    as_exception_capture::State script_errors;
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
        const bool confirmed=!string_event_started || (string_event_completed && !script_errors.exception_count && container_return_cleanup_confirmed);
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
        if(script_errors.exception_count && contains_string_output) {
            string_ledger.mark_uncertain();string_quarantine_cause=4;
        }
        if(string_event_completed && (contains_string_input || contains_string_array_input) && !string_cleanup_done && !string_ledger.uncertain()) {
            try{validate_string_inputs();}catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;}
        }
        if(!script_errors.exception_count && string_event_completed && has_string_return && !string_cleanup_done && !string_return_registered && !string_ledger.uncertain()) {
            StringHeader header{};
            try {
                if(!copy_memory(&header,buffer+return_offset,sizeof(header)))throw HandlerError("invalid_string_storage","The FString result is unavailable");
                string_ledger.add_return(header);string_return_registered=true;
            }catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=4;}
        }
        if(!script_errors.exception_count && string_event_completed && contains_string_output && !string_cleanup_done && !string_outputs_registered && !string_ledger.uncertain()) {
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
    if(script_call && call_script_generation!=successful_script_reloads_.load())
        throw HandlerError("script_reloaded","The script context changed before ProcessEvent",
            {{"executed",false},{"may_have_executed",false},{"request_started",false},
             {"previous_script_generation",call_script_generation},{"script_generation",successful_script_reloads_.load()}});
    string_event_started=true;
    native_exception::Diagnostic native_fault{};
    bool event_returned{};
    {
        as_exception_capture::Scope capture(script_errors);
        event_returned=call_event(reinterpret_cast<void*>(process_event_),object,reinterpret_cast<void*>(function),buffer,&native_fault);
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
    if(script_errors.exception_count) {
        json details={{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
            {"process_event_returned",true},{"script_completion_confirmed",false},
            {"script_errors",as_exception_capture::describe(script_errors)}};
        if(return_type=="StrProperty" || contains_string_output || contains_owned_containers) {
            if(return_type=="StrProperty" || contains_container_return)details["return_cleanup_confirmed"]=false;
            if(contains_array_output)details["array_output_cleanup_confirmed"]=false;
            if(contains_string_output){details["string_output_cleanup_confirmed"]=false;string_ledger.mark_uncertain();string_quarantine_cause=4;}
            details["cleanup_confirmed"]=false;
        }
        if(contains_string_input) {
            try {validate_string_inputs();}
            catch(const HandlerError& error) {string_ledger.mark_uncertain();string_quarantine_cause=3;details["cleanup_error"]=error.code();}
            catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;details["cleanup_error"]="input_validation_failed";}
            const auto cleanup=release_strings();
            details["input_cleanup_confirmed"]=cleanup.confirmed;
            details["cleanup_confirmed"]=details.value("cleanup_confirmed",true) && cleanup.confirmed;
            details["cleanup_attempted"]=details.value("cleanup_attempted",false) || cleanup.attempted;
        }
        if(contains_object_array) {
            try {validate_array_inputs();}
            catch(const HandlerError& error) {array_ledger.mark_uncertain();array_quarantine_cause=3;details["cleanup_error"]=error.code();}
            catch(...) {array_ledger.mark_uncertain();array_quarantine_cause=3;details["cleanup_error"]="input_validation_failed";}
            const auto cleanup=release_arrays();
            details["array_input_cleanup_confirmed"]=cleanup.confirmed;
            details["cleanup_confirmed"]=details.value("cleanup_confirmed",true) && cleanup.confirmed;
            details["cleanup_attempted"]=details.value("cleanup_attempted",false) || cleanup.attempted;
        }
        if(contains_string_output)release_mixed_strings(details);
        throw HandlerError("angelscript_exception","An AngelScript exception was recorded during ProcessEvent",std::move(details));
    }
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
            json response={{"function",function_name},{"return_type",return_type},{"result",result},{"executed",true},
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
                {"executed",true},{"thread_id",GetCurrentThreadId()},{"cleanup_confirmed",true}});
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
    json response={{"function",function_name},{"return_type",return_type},{"result",result},{"executed",true},{"thread_id",GetCurrentThreadId()}};
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
        if(contains_owned_containers && string_event_completed && !script_errors.exception_count)release_containers();
        if(error.code()=="angelscript_exception")throw;
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
            if(script_errors.exception_count) {
                details["script_errors"]=as_exception_capture::describe(script_errors);
                details["script_completion_confirmed"]=false;
            }
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
        if(!script_errors.exception_count && string_event_completed && !string_cleanup_done && return_type=="StrProperty" && !string_return_registered && !string_ledger.uncertain()) {
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
        if(script_errors.exception_count) {
            details["script_errors"]=as_exception_capture::describe(script_errors);
            details["script_completion_confirmed"]=false;
            if(return_type=="StrProperty")details["return_cleanup_confirmed"]=false;
        }
        if(string_event_started || !cleanup.confirmed)
            throw HandlerError("game_result_error",string_event_completed?"The call executed; FString processing or cleanup is incomplete":
                string_event_started?"The call effect is unknown; FString ownership is unconfirmed":"The call did not execute; FString ownership is unconfirmed",std::move(details));
        throw HandlerError(error.code(),error.what(),std::move(details));
    } catch(...) {
        if(contains_owned_containers && string_event_completed && !script_errors.exception_count)release_containers();
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
            if(script_errors.exception_count) {
                details["process_event_returned"]=string_event_completed;details["script_completion_confirmed"]=false;
                details["script_errors"]={{"source","observed_logging_entry"},{"exception_count",script_errors.exception_count},
                    {"full_capture",false},{"messages",json::array()},{"stack_captured",false}};
                throw HandlerError("angelscript_exception","An AngelScript exception was recorded; the complete report could not be prepared",std::move(details));
            }
            throw HandlerError("game_result_error","Object array processing is incomplete; do not repeat the request",std::move(details));
        }
        if(contains_string_output) {
            json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},
                {"do_not_retry",true},{"cause_code","result_processing_failed"}};
            release_mixed_strings(details);record_container_cleanup(details);
            if(script_errors.exception_count) {
                details["process_event_returned"]=string_event_completed;details["script_completion_confirmed"]=false;
                details["script_errors"]={{"exception_count",script_errors.exception_count},{"full_capture",false}};
                throw HandlerError("angelscript_exception","An AngelScript exception was recorded; the complete report could not be prepared",std::move(details));
            }
            throw HandlerError("game_result_error","FString output processing or cleanup is incomplete; do not repeat the request",std::move(details));
        }
        if(!contains_string_input) {
            if(contains_owned_containers) {
                json details={{"executed",string_event_completed},{"may_have_executed",string_event_started},{"do_not_retry",true},{"cause_code","result_processing_failed"}};
                record_container_cleanup(details);
                if(script_errors.exception_count) {
                    details["process_event_returned"]=string_event_completed;details["script_completion_confirmed"]=false;
                    details["script_errors"]={{"exception_count",script_errors.exception_count},{"full_capture",false}};
                    throw HandlerError("angelscript_exception","An AngelScript exception was recorded; the complete report could not be prepared",std::move(details));
                }
                throw HandlerError("game_result_error","Object container processing is incomplete; do not repeat the request",std::move(details));
            }
            if(script_errors.exception_count)
                throw HandlerError("angelscript_exception","An AngelScript exception was recorded; the complete report could not be prepared",
                    {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"process_event_returned",string_event_completed},
                     {"script_completion_confirmed",false},{"script_errors",{{"source","observed_logging_entry"},
                        {"exception_count",script_errors.exception_count},{"full_capture",false},{"messages",json::array()},{"stack_captured",false}}}});
            throw;
        }
        if(string_event_completed && !string_cleanup_done && !string_ledger.uncertain()) {
            try{validate_string_inputs();}
            catch(...) {string_ledger.mark_uncertain();string_quarantine_cause=3;}
        }
        if(!script_errors.exception_count && string_event_completed && !string_cleanup_done && return_type=="StrProperty" && !string_return_registered) {
            string_ledger.mark_uncertain();string_quarantine_cause=4;
        }
        const auto cleanup=release_strings();
        if(script_errors.exception_count) {
            json details=
                {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"process_event_returned",string_event_completed},
                 {"script_completion_confirmed",false},{"script_errors",{{"source","observed_logging_entry"},
                    {"exception_count",script_errors.exception_count},{"full_capture",false},{"messages",json::array()},{"stack_captured",false}}},
                 {"input_cleanup_confirmed",cleanup.confirmed},{"cleanup_confirmed",cleanup.confirmed && !contains_owned_containers},
                 {"return_cleanup_confirmed",return_type!="StrProperty" && !contains_container_return}};
            record_container_cleanup(details);
            throw HandlerError("angelscript_exception","An AngelScript exception was recorded; the complete report could not be prepared",
                std::move(details));
        }
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
json GameAccess::execute(const json& request) {
    auto op=request.at("op").get<std::string>();auto params=request.at("params");
    auto session=request.at("session_id").get<std::string>();
    if(session!=handle_session_){observations_.disconnect(handle_session_);handles_.clear();signature_contracts_.clear();handle_session_=session;}
    const auto now=static_cast<uint64_t>(std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
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
    if(op!="objects" && op!="properties" && op!="functions" && op!="invoke" && op!="read_array" && op!="read_set" && op!="read_map" && op!="read_struct")throw HandlerError("unknown_operation","The operation is unsupported");
    if(op=="objects") {
        auto query=params.value("query",std::string{}),class_query=params.value("class_query",std::string{});const auto limit=limit_of(params);
        if(params.contains("exact_path") && !params.at("exact_path").is_string())
            throw HandlerError("invalid_query","exact_path requires a string");
        const auto exact_path=params.value("exact_path",std::string{});
        if(!object_query::valid_exact_path(exact_path))
            throw HandlerError("invalid_query","exact_path must contain no NUL");
        auto count=read<int32_t>(base_+kObjects+0x14);if(count<0)throw HandlerError("invalid_registry","Invalid object registry");
        json items=json::array();int scanned=0,skipped=0;
        for(int index=0;index<count;++index){++scanned;
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
        return {{"items",items},{"total_slots",count},{"scanned_slots",scanned},{"skipped",skipped},{"truncated",scanned<count},{"thread_id",GetCurrentThreadId()}};
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
            auto result=properties(object,limit);
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
        return functions(object,limit_of(params),query,params.at("handle").get<std::string>());
    }
    if(op=="invoke")return invoke(object,params.at("function").get<std::string>(),params.value("arguments",json::object()),
                                  params.at("handle").get<std::string>(),params.contains("expected_signature_token")?&params.at("expected_signature_token"):nullptr);
    throw HandlerError("unknown_operation","The operation is unsupported");
}
}
