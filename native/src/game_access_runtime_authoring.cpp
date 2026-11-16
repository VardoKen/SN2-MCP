#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/asset_path.hpp"
#include "sn2_mcp/game_memory.hpp"
#include "sn2_mcp/native_exception.hpp"
#include "sn2_mcp/runtime_authoring_bindings.hpp"
#include "sn2_mcp/runtime_blueprint_compiler.hpp"
#include "sn2_mcp/runtime_archive.hpp"
#include "sn2_mcp/runtime_package_writer.hpp"
#include "sn2_mcp/runtime_iostore_writer.hpp"
#include "sn2_mcp/runtime_pak_writer.hpp"
#include "sn2_mcp/runtime_authoring_lifecycle.hpp"
#include <array>
#include <charconv>
#include <cmath>
#include <cstring>
#include <functional>
#include <filesystem>
#include <fstream>
#include <map>
#include <set>
#include <limits>
#include <string>
#include <vector>

namespace sn2_mcp {
using json=nlohmann::json;
namespace {
namespace rb=runtime_blueprint;
template<class Value>void write(std::uintptr_t address,const Value& value) {
    static_assert(std::is_trivially_copyable_v<Value>);
    if(!game_memory::copy(reinterpret_cast<void*>(address),&value,sizeof(value)))
        throw HandlerError("runtime_authoring_memory_changed","The owned authoring value could not be written");
}
struct alignas(16) ConstructParameters {std::array<std::byte,0x80> bytes{};};
static_assert(sizeof(ConstructParameters)==0x80 && alignof(ConstructParameters)==16);
struct FunctionLinkInfo {void*(__fastcall* create)();const char* name;};
static_assert(sizeof(FunctionLinkInfo)==16);
thread_local void* linking_function{};
void* __fastcall existing_function(){return linking_function;}
// Engine exceptions are captured in frames containing only trivially owned
// state. Keep STL containers in the game-thread caller.
bool call_construct(std::uintptr_t ctor,std::uintptr_t construct,void* klass,void* outer,
    std::uint64_t name,std::uint32_t flags,void** result,native_exception::Diagnostic* fault) noexcept {
    ConstructParameters parameters{};
    __try {
        reinterpret_cast<void*(__fastcall*)(void*,void*)>(ctor)(&parameters,klass);
        std::memcpy(parameters.bytes.data()+8,&outer,8);
        std::memcpy(parameters.bytes.data()+0x10,&name,8);
        std::memcpy(parameters.bytes.data()+0x18,&flags,4);
        *result=reinterpret_cast<void*(__fastcall*)(const void*)>(construct)(&parameters);
        return true;
    }__except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_field(std::uintptr_t entry,std::uint64_t type,std::uint64_t owner,
    std::uint64_t name,void** result,native_exception::Diagnostic* fault) noexcept {
    __try {
        *result=reinterpret_cast<void*(__fastcall*)(const std::uint64_t*,const std::uint64_t*,
            const std::uint64_t*,std::uint32_t)>(entry)(&type,&owner,&name,0);
        return true;
    }__except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_object_method(std::uintptr_t entry,void* object,native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*)>(entry)(object);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_set_super(std::uintptr_t entry,void* object,void* parent,native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*,void*)>(entry)(object,parent);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_static_link(std::uintptr_t entry,void* object,native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*,bool)>(entry)(object,true);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_collect_references(std::uintptr_t entry,void* object,native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*,void*)>(entry)(object,
        reinterpret_cast<void*>(reinterpret_cast<std::uintptr_t>(object)+0x90));return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_serialize(std::uintptr_t object,void* archive,native_exception::Diagnostic* fault) noexcept {
    __try {
        const auto table=*reinterpret_cast<std::uintptr_t*>(object);
        reinterpret_cast<void(__fastcall*)(void*,void*)>(*reinterpret_cast<std::uintptr_t*>(table+0xc8))(
            reinterpret_cast<void*>(object),archive);return true;
    }__except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_serialize_defaults(std::uintptr_t entry,std::uintptr_t klass,std::uintptr_t object,void* archive,
    native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*,void*,void*)>(entry)(reinterpret_cast<void*>(klass),reinterpret_cast<void*>(object),archive);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_default_object(std::uintptr_t entry,void* klass,void** result,native_exception::Diagnostic* fault) noexcept {
    __try {*result=reinterpret_cast<void*(__fastcall*)(void*)>(entry)(klass);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_archetype(std::uintptr_t entry,std::uintptr_t object,std::uintptr_t* result,
native_exception::Diagnostic* fault) noexcept {
__try {*result=reinterpret_cast<std::uintptr_t(__fastcall*)(const void*)>(entry)(reinterpret_cast<const void*>(object));return true;}
__except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
struct ObjectDependencyArray {std::uintptr_t data{};std::int32_t count{},capacity{};};
static_assert(sizeof(ObjectDependencyArray)==16);
bool call_preload_dependencies(std::uintptr_t object,ObjectDependencyArray* result,
native_exception::Diagnostic* fault) noexcept {
__try {
    const auto table=*reinterpret_cast<std::uintptr_t*>(object);
    // Exact UObject/UClass/BPGC slot 40 in the source-correlated Shipping
    // tables: void GetPreloadDependencies(TArray<UObject*>&).
    const auto method=*reinterpret_cast<std::uintptr_t*>(table+0x140);
    reinterpret_cast<void(__fastcall*)(void*,ObjectDependencyArray*)>(method)(reinterpret_cast<void*>(object),result);
    return true;
}__except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_root(std::uintptr_t entry,std::uintptr_t item,native_exception::Diagnostic* fault,bool* changed=nullptr) noexcept {
    __try {const auto result=reinterpret_cast<bool(__fastcall*)(void*,std::uint32_t)>(entry)(reinterpret_cast<void*>(item),0x40000000);if(changed)*changed=result;return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_link_function(std::uintptr_t entry,void* klass,void* function,const char* name,
    native_exception::Diagnostic* fault) noexcept {
    const FunctionLinkInfo info{existing_function,name};
    linking_function=function;
    __try {reinterpret_cast<void(__fastcall*)(void*,const FunctionLinkInfo*,std::uint32_t)>(entry)(klass,&info,1);linking_function=nullptr;return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){linking_function=nullptr;return false;}
}
bool call_reallocate(std::uintptr_t entry,void* existing,std::size_t count,void** result,
    native_exception::Diagnostic* fault) noexcept {
    __try {*result=reinterpret_cast<void*(__fastcall*)(void*,std::size_t,std::uint32_t)>(entry)(existing,count,16);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
bool call_free(std::uintptr_t entry,void* allocation,native_exception::Diagnostic* fault) noexcept {
    __try {reinterpret_cast<void(__fastcall*)(void*)>(entry)(allocation);return true;}
    __except(native_exception::capture(GetExceptionInformation(),fault)){return false;}
}
struct RuntimeAuthoredRecord {
    std::uintptr_t module{},klass{},package{},default_object{};
    std::int32_t class_index{},class_serial{};
    std::string asset_path;
    json specification;
    std::vector<std::uintptr_t> rooted_items;
    runtime_authoring_lifecycle::RootLedger root_ledger;
    struct MemberDefault {
        std::uintptr_t field{};
        std::int32_t offset{},size{};
        std::string name,kind;
        json value;
    };
    std::vector<MemberDefault> scalar_defaults;
    ObjectSerialIdentity class_identity;
};
std::map<std::string,RuntimeAuthoredRecord> authored_records;
std::uint64_t next_authored_revision{};
std::string new_revision_id() {
    FILETIME created{},exited{},kernel{},user{};
    if(!GetProcessTimes(GetCurrentProcess(),&created,&exited,&kernel,&user))
        throw HandlerError("runtime_authoring_process_identity_unavailable","The current process creation identity is unavailable");
    const auto creation=(std::uint64_t(created.dwHighDateTime)<<32)|created.dwLowDateTime;
    return "revision-"+std::to_string(GetCurrentProcessId())+"-"+std::to_string(creation)+"-"+std::to_string(++next_authored_revision);
}
template<class Resolve>
auto authored_record(const json& params,std::uintptr_t module,Resolve&& resolve) {
    auto found=authored_records.end();
    if(params.contains("revision_id")) {
        if(!params.at("revision_id").is_string() || params.at("revision_id").get<std::string>().empty())
            throw HandlerError("invalid_runtime_authoring_revision","revision_id must be a nonempty string");
        found=authored_records.find(params.at("revision_id").get<std::string>());
    }else if(params.contains("class_handle") && params.at("class_handle").is_string()) {
        const auto pointer=reinterpret_cast<std::uintptr_t>(resolve(params.at("class_handle").get<std::string>()));
        for(auto candidate=authored_records.begin();candidate!=authored_records.end();++candidate)
            if(candidate->second.module==module && candidate->second.klass==pointer
               && !candidate->second.root_ledger.retirement_requested())found=candidate;
    }else throw HandlerError("invalid_runtime_authoring_revision","revision_id or class_handle is required");
    if(found==authored_records.end() || found->second.module!=module)
        throw HandlerError("runtime_authored_class_unavailable","The selected revision has no runtime authoring record");
    return found;
}
template<class Observe>
void refresh_active_roots(RuntimeAuthoredRecord& record,Observe&& observe) {
    if(record.root_ledger.retirement_requested())return;
    std::vector<runtime_authoring_lifecycle::RootIdentityObservation> refresh;
    for(const auto& root:record.root_ledger.roots()) {
        auto current=observe(root);
        if(!current.confirmed_root_set)
            throw HandlerError("runtime_authoring_root_changed","An active owned authoring root is no longer retained");
        refresh.push_back(current);
    }
    if(!record.root_ledger.refresh_owned_identities(refresh))
        throw HandlerError("runtime_authoring_root_identity_changed","Rooted object identities changed before the requested operation");
    for(const auto& root:record.root_ledger.roots())if(root.identity.object==record.klass) {
        record.class_identity=root.identity;record.class_serial=root.identity.serial;
    }
}
std::string property_kind(const rb::Type& type) {
    if(type.array)return "ArrayProperty";
    static const std::map<std::string,std::string> kinds{{"bool","BoolProperty"},{"int","IntProperty"},
        {"int64","Int64Property"},{"float","FloatProperty"},{"double","DoubleProperty"},
        {"string","StrProperty"},{"name","NameProperty"},{"text","TextProperty"},
        {"object","ObjectProperty"},{"class","ClassProperty"},{"struct","StructProperty"},{"enum","EnumProperty"}};
    const auto found=kinds.find(type.kind);
    if(found==kinds.end())throw HandlerError("runtime_authoring_type_unavailable","The authoring type has no established property factory: "+type.kind);
    return found->second;
}
std::string bytecode_hex(std::span<const std::uint8_t> bytes) {
    constexpr char digits[]="0123456789abcdef";
    std::string result;result.reserve(bytes.size()*2);
    for(const auto value:bytes){result.push_back(digits[value>>4]);result.push_back(digits[value&15]);}
    return result;
}
// Source-correlated CreatePackage in CL131439 returns a UObject pointer in RAX.
// Keep SEH confined to the engine call; C++ owned buffers remain in the caller.
bool call_create_package(std::uintptr_t entry,const wchar_t* name,void** result,
                         native_exception::Diagnostic* fault) noexcept {
    __try {
        *result=reinterpret_cast<void*(__fastcall*)(const wchar_t*)>(entry)(name);
        return true;
    } __except(native_exception::capture(GetExceptionInformation(),fault)) {return false;}
}

std::vector<wchar_t> package_name_utf16(const std::string& name) {
    if(name.empty())return std::vector<wchar_t>{L'\0'};
    if(name.size()>static_cast<std::size_t>(std::numeric_limits<int>::max()))
        throw HandlerError("invalid_package_name","The package name exceeds the Windows conversion extent");
    const int count=MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,name.data(),
                                      static_cast<int>(name.size()),nullptr,0);
    if(count<=0)throw HandlerError("invalid_package_name","The package name requires valid UTF-8");
    std::vector<wchar_t> result(static_cast<std::size_t>(count)+1);
    if(MultiByteToWideChar(CP_UTF8,MB_ERR_INVALID_CHARS,name.data(),
                          static_cast<int>(name.size()),result.data(),count)!=count)
        throw HandlerError("invalid_package_name","The package name could not be converted to UTF-16");
    return result;
}
}

json GameAccess::runtime_create_package(const json& params) {
    if(!params.contains("package_name") || !params.at("package_name").is_string())
        throw HandlerError("invalid_package_name","package_name must be a canonical Unreal package name");
    const auto package_name=params.at("package_name").get<std::string>();
    // Reuse the established canonical asset-path validation, with a neutral
    // synthetic asset suffix. This validates the package rather than guessing
    // an arbitrary name length or allowing CreatePackage to normalize input.
    const auto parsed=asset_path::parse(package_name+".RuntimeAuthoringAsset");
    if(parsed.package_name!=package_name)
        throw HandlerError("invalid_package_name","The package name is not canonical");
    if(!bindings_.create_package.available())
        throw HandlerError("package_construction_unavailable","CreatePackage was not discovered in this executable");
    const auto text=package_name_utf16(package_name);
    void* package{};
    native_exception::Diagnostic fault{};
    if(!call_create_package(base_+bindings_.create_package.rva,text.data(),&package,&fault))
        throw HandlerError("package_construction_exception","CreatePackage raised a native exception",
            {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
             {"package_name",package_name},{"native_exception",native_exception::describe(fault)}});
    if(!package)
        throw HandlerError("package_construction_failed","CreatePackage returned no package",
            {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
             {"package_name",package_name}});
    const auto identity=validate_object(package);
    if(!class_is(package,"/Script/CoreUObject.Package") || object_path(package)!=package_name)
        throw HandlerError("package_construction_identity_mismatch","The returned package has an unexpected class or path",
            {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
             {"package_name",package_name}});
    bindings_.create_package.runtime_validated=true;
    auto descriptor=object_info(package,identity.index);
    return {{"source","runtime_package_construction"},{"package_name",package_name},
            {"package",std::move(descriptor)},{"persisted",false},
            {"thread_id",GetCurrentThreadId()},
            {"effects",{{"executed",true},{"may_have_executed",true},{"do_not_retry",false}}}};
}

json GameAccess::runtime_compile_blueprint(const json& params) {
    const auto& asset=params.contains("asset")?params.at("asset"):params;
    const auto make_name=[&](const std::string& text){return encode_name_text(json{{"name",text}});};
    if(!asset.is_object())throw HandlerError("invalid_spec","asset must contain a schema 1 Blueprint specification");
    const auto parsed=asset_path::parse(asset.value("path",std::string{}));
    if(asset.value("kind",std::string{})!="blueprint")
        throw HandlerError("unsupported_asset_kind","Runtime compilation requires a Blueprint asset");
    const auto find_object=[&](const std::string& path)->void* {
        const auto count=game_memory::read<std::int32_t>(base_+bindings_.object_array.rva+0x14);
        for(int index=0;index<count;++index) {
            auto* object=slot(index);if(!object)continue;
            try{if(object_path(object)==path){validate_object(object);return object;}}catch(const HandlerError&){}
        }
        throw HandlerError("runtime_authoring_object_unavailable","The required reflected object is not loaded: "+path);
    };
    const auto find_class=[&](const std::string& path)->void* {
        auto* klass=find_object(path);validate_class(klass);return klass;
    };
    const auto find_field=[&](std::uintptr_t owner,const std::string& field_name)->std::uintptr_t {
        std::set<std::uintptr_t> classes,fields;
        for(auto klass=owner;klass;klass=game_memory::read<std::uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)throw HandlerError("property_scan_cycle","The authoring class hierarchy contains a cycle");
            for(auto field=game_memory::read<std::uintptr_t>(klass+0x50);field;field=game_memory::read<std::uintptr_t>(field+0x18)) {
                if(!fields.insert(field).second)throw HandlerError("property_scan_cycle","The authoring field chain contains a cycle");
                if(name(game_memory::read<std::uint64_t>(field+0x20))==field_name)return field;
            }
        }
        throw HandlerError("runtime_authoring_property_unavailable","The reflected property does not exist: "+field_name);
    };
    std::map<std::string,std::uintptr_t> symbols;
    std::map<std::string,Handle> referenced_objects;
    std::map<std::string,std::pair<std::uintptr_t,Handle>> referenced_fields;
    const auto remember=[&](const std::string& symbol,void* object) {
        const auto value=validate_object(object);symbols[symbol]=reinterpret_cast<std::uintptr_t>(object);
        referenced_objects[symbol]=value;
    };
    std::function<rb::Type(std::uintptr_t)> reflected_type;
    reflected_type=[&](std::uintptr_t field)->rb::Type {
        const auto kind=game_memory::field_type(field,[&](auto value){return name(value);});
        rb::Type type;
        if(kind=="ArrayProperty") {type=reflected_type(game_memory::read<std::uintptr_t>(field+0x78));type.array=true;return type;}
        static const std::map<std::string,std::string> scalar{{"BoolProperty","bool"},{"IntProperty","int"},
            {"Int64Property","int64"},{"FloatProperty","float"},{"DoubleProperty","double"},
            {"StrProperty","string"},{"NameProperty","name"},{"TextProperty","text"},
            {"ObjectProperty","object"},{"ClassProperty","class"},{"StructProperty","struct"},{"EnumProperty","enum"}};
        const auto found=scalar.find(kind);
        if(found==scalar.end())throw HandlerError("runtime_authoring_type_unavailable","The reflected property type is unsupported: "+kind);
        type.kind=found->second;
        if(kind=="ObjectProperty" || kind=="ClassProperty" || kind=="StructProperty" || kind=="EnumProperty") {
            const auto dependency=game_memory::read<std::uintptr_t>(field+(kind=="ClassProperty"?0x78:kind=="EnumProperty"?0x78:0x70));
            type.path=object_path(reinterpret_cast<void*>(dependency));
        }
        return type;
    };
    rb::Resolver resolver;
    resolver.function=[&](const std::string& owner_path,const std::string& function_name) {
        auto* owner=find_class(owner_path);std::set<std::uintptr_t> classes,functions;
        std::uintptr_t function{};
        for(auto klass=reinterpret_cast<std::uintptr_t>(owner);klass && !function;klass=game_memory::read<std::uintptr_t>(klass+0x40)) {
            if(!classes.insert(klass).second)throw HandlerError("function_scan_cycle","The reflected function hierarchy contains a cycle");
            for(auto child=game_memory::read<std::uintptr_t>(klass+0x48);child;child=game_memory::read<std::uintptr_t>(child+0x28)) {
                if(!functions.insert(child).second)throw HandlerError("function_scan_cycle","The reflected function chain contains a cycle");
                if(object_name(reinterpret_cast<void*>(child))==function_name){function=child;break;}
            }
        }
        if(!function)throw HandlerError("runtime_authoring_function_unavailable","The reflected function is not loaded: "+owner_path+"."+function_name);
        rb::FunctionSignature signature;
        signature.symbol="external_function:"+object_path(reinterpret_cast<void*>(function));
        signature.name=function_name;signature.owner_path=object_path(reinterpret_cast<void*>(game_memory::read<std::uintptr_t>(function+0x20)));
        signature.flags=game_memory::read<std::uint32_t>(function+0xb0);signature.pure=(signature.flags&0x10000000)!=0;
        signature.editor_only=(signature.flags&0x20000000)!=0;
        remember(signature.symbol,reinterpret_cast<void*>(function));
        auto* actual_owner=find_class(signature.owner_path);
        if(signature.flags&0x2000) {
            auto* defaults=reinterpret_cast<void*>(game_memory::read<std::uintptr_t>(reinterpret_cast<std::uintptr_t>(actual_owner)+0x110));
            if(!defaults)throw HandlerError("runtime_authoring_default_object_unavailable","A static function requires its real loaded default object");
            signature.default_object_symbol="external_object:"+object_path(defaults);remember(signature.default_object_symbol,defaults);
        }
        std::set<std::uintptr_t> fields;
        for(auto field=game_memory::read<std::uintptr_t>(function+0x50);field;field=game_memory::read<std::uintptr_t>(field+0x18)) {
            if(!fields.insert(field).second)throw HandlerError("property_scan_cycle","The function parameter chain contains a cycle");
            const auto flags=game_memory::read<std::uint64_t>(field+0x38);if(!(flags&0x80))break;
            rb::Parameter parameter;parameter.name=name(game_memory::read<std::uint64_t>(field+0x20));
            parameter.symbol=signature.symbol+"/parameter:"+parameter.name;parameter.type=reflected_type(field);
            parameter.direction=(flags&0x400)?rb::Direction::return_value:
                ((flags&0x100) && !(flags&2))?((flags&0x8000000)?rb::Direction::input_output:rb::Direction::output):rb::Direction::input;
            parameter.const_reference=(flags&2) && (flags&(0x100|0x8000000));
            // LatentInfo is the actual runtime continuation contract. This
            // resolver does not inspect editor metadata; missing metadata
            // does not establish a synchronous runtime contract.
            if(parameter.type.kind=="struct" && parameter.type.path=="/Script/Engine.LatentActionInfo")
                signature.latent=true;
            parameter.default_to_self=parameter.name=="WorldContextObject";
            parameter.wildcard=parameter.type.array && signature.owner_path=="/Script/Engine.KismetArrayLibrary";
            symbols[parameter.symbol]=field;referenced_fields[parameter.symbol]={field,validate_object(reinterpret_cast<void*>(function))};
            signature.parameters.push_back(std::move(parameter));
        }
        return signature;
    };
    resolver.structure=[&](const std::string& path) {
        auto* object=find_object(path);const auto pointer=reinterpret_cast<std::uintptr_t>(object);
        if(!class_is(object,"/Script/CoreUObject.ScriptStruct"))
            throw HandlerError("runtime_authoring_struct_unavailable","The declared structure must be a UScriptStruct");
        rb::StructSignature signature;signature.symbol="external_struct:"+path;
        signature.size=game_memory::read<std::int32_t>(pointer+0x58);remember(signature.symbol,object);
        std::set<std::uintptr_t> fields;
        for(auto field=game_memory::read<std::uintptr_t>(pointer+0x70);field;field=game_memory::read<std::uintptr_t>(field+0x48)) {
            if(!fields.insert(field).second)throw HandlerError("property_scan_cycle","The structure PropertyLink chain contains a cycle");
            const auto flags=game_memory::read<std::uint64_t>(field+0x38);
            if(flags&(0x2000ull|0x800000000ull))continue;
            const auto member_name=name(game_memory::read<std::uint64_t>(field+0x20));
            const auto symbol=signature.symbol+"/member:"+member_name;symbols[symbol]=field;
            referenced_fields[symbol]={field,validate_object(object)};
            signature.members.push_back({member_name,symbol,reflected_type(field),game_memory::read<std::int32_t>(field+0x30)});
        }
        return signature;
    };
    resolver.object=[&](const std::string& path,const rb::Type& type) {
        void* object{};if(auto found=handles_.find(path);found!=handles_.end())object=resolve(path);else object=find_object(path);
        if(type.kind=="class") {
            validate_class(object);
            if(!is_compatible_class(object,find_class("/Script/CoreUObject.Class"),find_class(type.path)))
                throw HandlerError("incompatible_class_argument",
                    "The authored class literal does not match the pin MetaClass",
                    {{"literal",path},{"meta_class",type.path},{"compiled",false},{"executed",false},
                     {"may_have_executed",false},{"preserved_previous",true}});
        }
        else if(!type.path.empty() && !is_compatible_object(object,find_class(type.path)))
            throw HandlerError("incompatible_object_argument","The authored object literal does not match the pin class");
        const auto symbol="external_object:"+object_path(object);remember(symbol,object);return symbol;
    };
    resolver.compatible=[&](const rb::Type& from,const rb::Type& to) {
        if(from==to)return true;
        if(from.array!=to.array || from.kind!=to.kind || (from.kind!="object" && from.kind!="class"))return false;
        auto* child=find_class(from.path);auto* parent=find_class(to.path);
        std::set<std::uintptr_t> seen;
        for(auto klass=reinterpret_cast<std::uintptr_t>(child);klass;klass=game_memory::read<std::uintptr_t>(klass+0x40)) {
            if(!seen.insert(klass).second)throw HandlerError("class_scan_cycle","The declared class hierarchy contains a cycle");
            if(klass==reinterpret_cast<std::uintptr_t>(parent))return true;
        }
        return false;
    };
    rb::Program program;
    try{program=rb::compile_blueprint(asset,resolver);}
    catch(const rb::CompilerError& error){throw HandlerError(error.code,error.what(),
        {{"node",error.node.empty()?json(nullptr):json(error.node)},{"pin",error.pin.empty()?json(nullptr):json(error.pin)},
         {"compiled",false},{"executed",false},{"may_have_executed",false},{"preserved_previous",true}});}
    const auto bindings=runtime_authoring::discover(base_);
    const auto entry=[&](const std::string& name){return bindings.address(base_,name);};
    for(const auto* binding:{"construct_parameters","construct_object","field_try_construct","static_link",
        "class_set_super","function_bind","blueprint_class_bind","link_functions","set_root_flags","clear_root_flags","create_default_object",
        "collect_bytecode_references","mark_as_reachable"})entry(binding);
    if(std::any_of(program.functions.begin(),program.functions.end(),[](const auto& function){return function.event_override;}))
        entry("reinitialize_struct_base_chain");
    if(!bindings_.realloc.available())throw HandlerError("runtime_authoring_allocator_unavailable","The game allocator is unavailable");
    auto* parent=find_class(program.parent_path);
    auto* meta_class=find_class("/Script/Engine.BlueprintGeneratedClass");
    auto* function_class=find_class("/Script/CoreUObject.Function");
    // Unreal declaration identity is comparison FName plus number, rather
    // than spelling. Check the actual target names before constructing any
    // Package/UClass/UFunction, including unsupported inherited masking.
    std::map<std::uint64_t,std::string> inherited_names;
    std::set<std::uintptr_t> parent_classes,parent_fields,parent_functions;
    for(auto current=reinterpret_cast<std::uintptr_t>(parent);current;
        current=game_memory::read<std::uintptr_t>(current+0x40)) {
        if(!parent_classes.insert(current).second)
            throw HandlerError("class_scan_cycle","The authoring parent class hierarchy contains a cycle");
        const auto owner=object_path(reinterpret_cast<void*>(current));
        for(auto field=game_memory::read<std::uintptr_t>(current+0x50);field;
            field=game_memory::read<std::uintptr_t>(field+0x18)) {
            if(!parent_fields.insert(field).second)
                throw HandlerError("property_scan_cycle","The inherited field chain contains a cycle");
            const auto raw=game_memory::read<std::uint64_t>(field+0x20);
            inherited_names.emplace(raw,owner+"."+name(raw));
        }
        for(auto child=game_memory::read<std::uintptr_t>(current+0x48);child;
            child=game_memory::read<std::uintptr_t>(child+0x28)) {
            if(!parent_functions.insert(child).second)
                throw HandlerError("function_scan_cycle","The inherited function chain contains a cycle");
            auto* object=reinterpret_cast<void*>(child);validate_object(object);
            if(class_is(object,"/Script/CoreUObject.Function"))
                inherited_names.emplace(game_memory::read<std::uint64_t>(child+0x18),object_path(object));
        }
    }
    std::map<std::uint64_t,std::string> declarations;
    const auto declaration_name=[&](const std::string& text,const std::string& kind) {
        const auto raw=make_name(text);
        const auto [found,inserted]=declarations.emplace(raw,text);
        if(!inserted)
            throw HandlerError("runtime_authoring_name_collision",
                "Authored declarations have the same Unreal FName",
                {{"name",text},{"conflicts_with",found->second},{"kind",kind},{"compiled",false},
                 {"executed",false},{"may_have_executed",false},{"preserved_previous",true}});
        if(const auto inherited=inherited_names.find(raw);inherited!=inherited_names.end()
           && kind!="event_override")
            throw HandlerError("runtime_authoring_inherited_name_collision",
                "An authored declaration masks an inherited field or function",
                {{"name",text},{"inherited",inherited->second},{"kind",kind},{"compiled",false},
                 {"executed",false},{"may_have_executed",false},{"preserved_previous",true}});
    };
    for(const auto& member:program.members)declaration_name(member.name,"member");
    for(const auto& function:program.functions) {
        declaration_name(function.name,function.event_override?"event_override":"function");
        if(function.event_override) {
            const auto super=symbols.find(function.super_function_symbol);
            if(super==symbols.end() || !super->second)
                throw HandlerError("runtime_authoring_event_super_unavailable","The reflected event has no bound inherited function");
            const auto expected=resolver.function(program.parent_path,function.name);
            if(expected.symbol!=function.super_function_symbol || !(expected.flags&0x08000000u))
                throw HandlerError("runtime_authoring_event_signature_changed","The inherited Blueprint event changed before construction");
        }
        std::map<std::uint64_t,std::string> property_names;
        for(const auto& property:function.properties) {
            const auto [found,inserted]=property_names.emplace(make_name(property.name),property.name);
            if(!inserted)
                throw HandlerError("runtime_authoring_parameter_name_collision",
                    "Function parameters or locals have the same Unreal FName",
                    {{"function",function.name},{"pin",property.name},{"conflicts_with",found->second},
                     {"compiled",false},{"executed",false},{"may_have_executed",false},{"preserved_previous",true}});
        }
    }
    const auto generated_name=parsed.asset_name+"_C",generated_path=parsed.package_name+"."+generated_name;
    const auto count=game_memory::read<std::int32_t>(base_+bindings_.object_array.rva+0x14);
    for(int index=0;index<count;++index) {
        auto* object=slot(index);if(!object)continue;
        try{if(object_path(object)==generated_path)throw HandlerError("runtime_authoring_class_exists",
            "The generated class already exists; publish a separate revision path to preserve the active class");}
        catch(const HandlerError& error){if(error.code()=="runtime_authoring_class_exists")throw;}
    }
    // Factories/linkers are preflighted before the first game mutation. Missing
    // support must preserve a previous authored revision.
    const auto preflight_type=[&](const auto& self,const rb::Type& type)->void {
        property_kind(type);
        if(type.kind=="enum")throw HandlerError("runtime_authoring_enum_construction_pending","Enum property construction requires its exact underlying-field binding");
        if(type.kind=="text")throw HandlerError("runtime_authoring_text_construction_pending","FText property construction requires typed runtime default/literal support");
        if(type.kind=="object" || type.kind=="class")find_class(type.path);
        if(type.kind=="struct")find_object(type.path);
        if(type.array){auto inner=type;inner.array=false;self(self,inner);}
    };
    for(const auto& property:program.members)preflight_type(preflight_type,property.type);
    for(const auto& function:program.functions)for(const auto& property:function.properties)preflight_type(preflight_type,property.type);
    // Validate scalar member defaults before creating any game objects. Complex
    // values require the separately established typed import adapter.
    std::map<std::string,json> member_defaults;
    for(const auto& member:program.members)if(!member.default_literal.empty()) {
        if(member.type.array || (member.type.kind!="bool" && member.type.kind!="int" && member.type.kind!="int64"
           && member.type.kind!="float" && member.type.kind!="double" && member.type.kind!="string" && member.type.kind!="name"))
            throw HandlerError("runtime_authoring_default_type_pending","The member default requires typed runtime import: "+member.name);
        const auto& literal=member.default_literal;
        if(member.type.kind=="string" || member.type.kind=="name")member_defaults[member.symbol]=literal;
        else if(member.type.kind=="bool") {
            if(literal=="true" || literal=="True" || literal=="1")member_defaults[member.symbol]=true;
            else if(literal=="false" || literal=="False" || literal=="0")member_defaults[member.symbol]=false;
            else throw HandlerError("invalid_variable_default","The Boolean default is invalid",{{"pin",member.name},{"executed",false},{"may_have_executed",false}});
        }else {
            json value;
            try{value=json::parse(literal);}catch(const json::exception&){throw HandlerError("invalid_variable_default","The numeric default is invalid",{{"pin",member.name},{"executed",false},{"may_have_executed",false}});}
            if(!value.is_number())throw HandlerError("invalid_variable_default","The member default must be numeric");
            if((member.type.kind=="int" || member.type.kind=="int64") && !value.is_number_integer())
                throw HandlerError("invalid_variable_default","An integer default cannot be fractional");
            if((member.type.kind=="int" || member.type.kind=="int64") && value.is_number_unsigned()
               && value.get<std::uint64_t>()>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
                throw HandlerError("invalid_variable_default","The integer default exceeds its signed reflected width");
            if(member.type.kind=="int") {
                const auto number=value.get<std::int64_t>();
                if(number<std::numeric_limits<std::int32_t>::min() || number>std::numeric_limits<std::int32_t>::max())
                    throw HandlerError("invalid_variable_default","The int default exceeds its reflected width");
            }
            if((member.type.kind=="float" || member.type.kind=="double")
               && (!std::isfinite(value.get<double>())
                   || (member.type.kind=="float" && !std::isfinite(value.get<float>()))))
                throw HandlerError("invalid_variable_default","The real default exceeds its finite reflected range");
            member_defaults[member.symbol]=value;
        }
    }
    native_exception::Diagnostic fault{};void* klass{};std::vector<std::uintptr_t> rooted_items;
    bool mutated{};std::string stage="package_creation";
    const auto failed=[&](const std::string& operation) {
        throw HandlerError("runtime_authoring_native_exception",operation+" raised a native exception",
            {{"stage",stage},{"native_exception",native_exception::describe(fault)}});
    };
    const auto item_for=[&](void* object) {
        const auto identity=validate_object(object);const auto chunks=game_memory::read<std::uintptr_t>(base_+bindings_.object_array.rva);
        const auto chunk=game_memory::read<std::uintptr_t>(chunks+static_cast<std::size_t>(identity.index>>16)*8);
        const auto item=chunk+static_cast<std::size_t>(identity.index&65535)*24;
        if(game_memory::read<std::uintptr_t>(item)!=reinterpret_cast<std::uintptr_t>(object))
            throw HandlerError("runtime_authoring_identity_changed","The authoring registry item changed");
        return item;
    };
    const auto root_object=[&](void* object) {
        const auto item=item_for(object);
        if(!(game_memory::read<std::uint32_t>(item+8)&0x40000000)) {
bool changed{};
if(!call_root(entry("set_root_flags"),item,&fault,&changed))failed("SetRootFlags");
if(changed)rooted_items.push_back(item);
if(!(game_memory::read<std::uint32_t>(item+8)&0x40000000))
throw HandlerError("runtime_authoring_root_changed","The authoring object did not retain RootSet after acquisition");
        }
    };
    try {
        mutated=true;
        const auto package_result=runtime_create_package({{"package_name",parsed.package_name}});
        auto* package=resolve(package_result.at("package").at("handle").get<std::string>());root_object(package);
        const auto construct=[&](void* type,void* outer,const std::string& name_text) {
            void* object{};
            if(!call_construct(entry("construct_parameters"),entry("construct_object"),type,outer,make_name(name_text),3,&object,&fault))failed("StaticConstructObject_Internal");
            if(!object)throw HandlerError("runtime_authoring_construction_failed","The engine object constructor returned null");
            validate_object(object);return object;
        };
        stage="class_construction";klass=construct(meta_class,package,generated_name);root_object(klass);
        const auto class_address=reinterpret_cast<std::uintptr_t>(klass),parent_address=reinterpret_cast<std::uintptr_t>(parent);
        if(!call_set_super(entry("class_set_super"),klass,parent,&fault))failed("UClass::SetSuperStruct");
        // Generated classes inherit eligible class flags, never native/intrinsic
        // status. Bind obtains the actual parent constructor and static hooks.
        constexpr std::uint32_t inherit=0x00000008|0x00000010|0x00000002|0x00000004|0x00000400|0x40000000|
            0x00000100|0x00000200|0x00010000|0x00800000|0x02000000|0x00200000|0x08000000|0x00000040|
            0x00008000|0x00020000|0x00001000|0x00002000;
        write(class_address+0xd4,(game_memory::read<std::uint32_t>(parent_address+0xd4)&inherit)|0x20040000u);
        write(class_address+0xe0,game_memory::read<std::uintptr_t>(parent_address+0xe0));
        const auto virtual_bind=game_memory::read<std::uintptr_t>(game_memory::read<std::uintptr_t>(class_address)+0x2b8);
        if(virtual_bind!=entry("blueprint_class_bind"))throw HandlerError("runtime_authoring_class_layout_changed","The new generated class has an unexpected Bind method");
        if(!call_object_method(virtual_bind,klass,&fault))failed("UBlueprintGeneratedClass::Bind");
        std::vector<std::pair<const rb::CompiledFunction*,void*>> generated_functions;
        std::map<std::string,std::pair<std::uintptr_t,void*>> owned_fields;
        const auto create_property=[&](const auto& self,const rb::PropertyDefinition& definition,void* owner)->std::uintptr_t {
            const auto type_name=make_name(property_kind(definition.type)),field_name=make_name(definition.name);
            void* field{};const auto owner_address=reinterpret_cast<std::uintptr_t>(owner);
            validate_object(owner);
            const auto owner_variant=owner_address | 1ull;
            if(!call_field(entry("field_try_construct"),type_name,owner_variant,field_name,&field,&fault))failed("FField::TryConstruct");
            if(!field)throw HandlerError("runtime_authoring_field_construction_failed","The requested property factory returned null: "+property_kind(definition.type));
            const auto pointer=reinterpret_cast<std::uintptr_t>(field);
            if(game_memory::read<std::uint64_t>(pointer+0x10)!=owner_variant || game_memory::read<std::uint64_t>(pointer+0x20)!=field_name)
                throw HandlerError("runtime_authoring_field_identity_changed","The new property owner or name is unexpected");
            std::uint64_t flags{};
            if(definition.role==rb::PropertyRole::member)flags=4;
            else if(definition.role==rb::PropertyRole::input)flags=0x80|(definition.const_reference?0x100|2|0x8000000:0);
            else if(definition.role==rb::PropertyRole::output)flags=0x80|0x100;
    // Factory-computed flags describe destruction, hashing and POD layout.
    // Preserve them while adding the authored declaration's semantic flags.
    write(pointer+0x38,game_memory::read<std::uint64_t>(pointer+0x38)|flags);
    if(!definition.type.array && definition.type.kind=="object")write(pointer+0x70,reinterpret_cast<std::uintptr_t>(find_class(definition.type.path)));
    if(!definition.type.array && definition.type.kind=="class") {
                write(pointer+0x70,reinterpret_cast<std::uintptr_t>(find_class("/Script/CoreUObject.Class")));
                write(pointer+0x78,reinterpret_cast<std::uintptr_t>(find_class(definition.type.path)));
            }
    if(!definition.type.array && definition.type.kind=="struct")write(pointer+0x70,reinterpret_cast<std::uintptr_t>(find_object(definition.type.path)));
            if(definition.type.array) {
                auto inner=definition;inner.type.array=false;inner.role=rb::PropertyRole::local;inner.symbol=definition.symbol+"/inner";
                // Array inner is owned by the FArrayProperty, not the UObject.
                const auto inner_type=make_name(property_kind(inner.type));void* inner_field{};
                if(!call_field(entry("field_try_construct"),inner_type,pointer,field_name,&inner_field,&fault))failed("FField::TryConstruct array inner");
                if(!inner_field)throw HandlerError("runtime_authoring_field_construction_failed","The array inner factory returned null");
                const auto inner_pointer=reinterpret_cast<std::uintptr_t>(inner_field);
        if(game_memory::read<std::uintptr_t>(inner_pointer+0x10)!=pointer
           || game_memory::read<std::uint64_t>(inner_pointer+0x20)!=field_name)
            throw HandlerError("runtime_authoring_field_identity_changed","The array inner owner or name is unexpected");
                if(inner.type.kind=="object")write(inner_pointer+0x70,reinterpret_cast<std::uintptr_t>(find_class(inner.type.path)));
                if(inner.type.kind=="struct")write(inner_pointer+0x70,reinterpret_cast<std::uintptr_t>(find_object(inner.type.path)));
                if(inner.type.kind=="class") {
                    write(inner_pointer+0x70,reinterpret_cast<std::uintptr_t>(find_class("/Script/CoreUObject.Class")));
                    write(inner_pointer+0x78,reinterpret_cast<std::uintptr_t>(find_class(inner.type.path)));
                }
                write(pointer+0x78,inner_pointer);
        symbols[inner.symbol]=inner_pointer;owned_fields[inner.symbol]={inner_pointer,owner};
            }
            symbols[definition.symbol]=pointer;owned_fields[definition.symbol]={pointer,owner};return pointer;
        };
        const auto add_properties=[&](const std::vector<rb::PropertyDefinition>& definitions,void* owner) {
            const auto address=reinterpret_cast<std::uintptr_t>(owner);
            // AddCppProperty prepends; reverse creation preserves the declared
            // Parm/OutParm/local order consumed by UFunction::Link.
            for(auto index=definitions.size();index>0;--index) {
                const auto field=create_property(create_property,definitions[index-1],owner);
                write(field+0x18,game_memory::read<std::uintptr_t>(address+0x50));
                write(address+0x50,field);
            }
        };
        stage="property_construction";add_properties(program.members,klass);
        for(const auto& function:program.functions) {
            stage="function_construction";auto* object=construct(function_class,klass,function.name);
            const auto address=reinterpret_cast<std::uintptr_t>(object);
            write(address+0xb0,function.flags);
        if(function.event_override) {
            const auto super=symbols.at(function.super_function_symbol);
            validate_object(reinterpret_cast<void*>(super));
            // UStruct::SetSuperStruct assignment plus the same base-chain
            // operation used by source-correlated UClass::SetSuperStruct.
            write(address+0x40,super);
            if(!call_object_method(entry("reinitialize_struct_base_chain"),reinterpret_cast<void*>(address+0x30),&fault))
                failed("FStructBaseChain::ReinitializeBaseChainArray");
        }
            write(address+0xba,std::uint32_t{});write(address+0xc0,std::uintptr_t{});
            write(address+0xc8,std::uintptr_t{});write(address+0xd0,std::int32_t{});write(address+0xd8,std::uintptr_t{});
            add_properties(function.properties,object);
            if(!call_static_link(entry("static_link"),object,&fault))failed("UFunction::StaticLink");
        if(function.event_override) {
            const auto super=symbols.at(function.super_function_symbol);
            if(game_memory::read<std::uint8_t>(address+0xb4)!=game_memory::read<std::uint8_t>(super+0xb4)
               || game_memory::read<std::uint16_t>(address+0xb6)!=game_memory::read<std::uint16_t>(super+0xb6))
                throw HandlerError("runtime_authoring_event_layout_mismatch","Generated event parameter extent differs from its reflected parent");
            auto own=game_memory::read<std::uintptr_t>(address+0x50);
            auto inherited=game_memory::read<std::uintptr_t>(super+0x50);
            for(;inherited && (game_memory::read<std::uint64_t>(inherited+0x38)&0x80);
                inherited=game_memory::read<std::uintptr_t>(inherited+0x18)) {
                if(!own || game_memory::read<std::uint64_t>(own+0x20)!=game_memory::read<std::uint64_t>(inherited+0x20)
                   || reflected_type(own)!=reflected_type(inherited)
                   || game_memory::read<std::int32_t>(own+0x44)!=game_memory::read<std::int32_t>(inherited+0x44))
                    throw HandlerError("runtime_authoring_event_layout_mismatch","Generated event parameters differ from the exact reflected parent");
                own=game_memory::read<std::uintptr_t>(own+0x18);
            }
        }
            if(!call_object_method(entry("function_bind"),object,&fault))failed("UFunction::Bind");
            if(!call_link_function(entry("link_functions"),klass,object,function.name.c_str(),&fault))failed("UClass::CreateLinkAndAddChildFunctionsToMap");
            symbols[function.symbol]=address;generated_functions.emplace_back(&function,object);
        }
        stage="class_link";if(!call_static_link(entry("static_link"),klass,&fault))failed("UClass::StaticLink");
        json function_results=json::array();
        for(const auto& [function,object]:generated_functions) {
            stage="script_binding";
            const auto bound=rb::bind_script(*function,[&](const rb::Relocation& relocation) {
                rb::BoundSymbol symbol;
                if(relocation.kind==rb::RelocationKind::script_name) {
                if(!relocation.symbol.starts_with("name:"))
                    throw HandlerError("runtime_authoring_name_symbol_invalid","The script name relocation has no name prefix");
                const auto value=make_name(relocation.symbol.substr(5));
                // This target's FName is the verified non-case-preserving
                // comparison/number pair. FScriptName still occupies 12 bytes;
                // NameToScriptName leaves DisplayIndex zero in this ABI.
                symbol.comparison_index=static_cast<std::uint32_t>(value);symbol.display_index=0;
                    symbol.number=static_cast<std::uint32_t>(value>>32);symbol.identity="name:"+name(value);return symbol;
                }
                const auto found=symbols.find(relocation.symbol);
                if(found==symbols.end())throw HandlerError("runtime_authoring_symbol_unavailable","The bytecode reference was not resolved: "+relocation.symbol);
                symbol.pointer=found->second;
                if(const auto owned=owned_fields.find(relocation.symbol);owned!=owned_fields.end()) {
                    const auto identity=validate_object(owned->second.second);
                    symbol.identity="field:"+std::to_string(symbol.pointer)+":owner:"+std::to_string(identity.index)+":"+std::to_string(identity.serial);
                }else if(const auto external_field=referenced_fields.find(relocation.symbol);external_field!=referenced_fields.end()) {
                    const auto actual=validate_object(external_field->second.second.object);
                    if(!same_serial_identity(serial_identity(actual),serial_identity(external_field->second.second))
                       || game_memory::read<std::uintptr_t>(symbol.pointer+0x10)!=(reinterpret_cast<std::uintptr_t>(actual.object)|1ull))
                        throw HandlerError("runtime_authoring_reference_changed","A reflected field reference changed before publication");
                    symbol.identity="field:"+std::to_string(symbol.pointer)+":owner:"+object_path(actual.object)+":"+std::to_string(actual.index)+":"+std::to_string(actual.serial);
                }else if(const auto reference=referenced_objects.find(relocation.symbol);reference!=referenced_objects.end()) {
                    const auto actual=validate_object(reference->second.object);
                    if(!same_serial_identity(serial_identity(actual),serial_identity(reference->second)))
                        throw HandlerError("runtime_authoring_reference_changed","A reflected bytecode reference changed before publication");
                    symbol.identity=object_path(actual.object)+":"+std::to_string(actual.index)+":"+std::to_string(actual.serial);
                }else {
                    const auto identity=validate_object(reinterpret_cast<void*>(symbol.pointer));
                    symbol.identity=object_path(identity.object)+":"+std::to_string(identity.index)+":"+std::to_string(identity.serial);
                }
                return symbol;
            });
            if(bound.bytecode.empty() || bound.bytecode.size()>static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
                throw HandlerError("runtime_authoring_bytecode_invalid","The generated script extent is invalid");
            void* script{};
            if(!call_reallocate(base_+bindings_.realloc.rva,nullptr,bound.bytecode.size(),&script,&fault))failed("game script allocation");
            if(!script || !game_memory::copy(script,bound.bytecode.data(),bound.bytecode.size()))
                throw HandlerError("runtime_authoring_script_publication_failed","The generated game script buffer could not be copied");
            const auto address=reinterpret_cast<std::uintptr_t>(object);
            write(address+0x60,reinterpret_cast<std::uintptr_t>(script));
            write(address+0x68,static_cast<std::int32_t>(bound.bytecode.size()));write(address+0x6c,static_cast<std::int32_t>(bound.bytecode.size()));
            if(!call_collect_references(entry("collect_bytecode_references"),object,&fault))failed("UStruct::CollectBytecodeReferencedObjects");
            const auto references=game_memory::read<std::uintptr_t>(address+0x90);
            const auto reference_count=game_memory::read<std::int32_t>(address+0x98),reference_capacity=game_memory::read<std::int32_t>(address+0x9c);
            if(reference_count<0 || reference_capacity<reference_count || (reference_count && !references))
                throw HandlerError("runtime_authoring_reference_collection_failed","The engine reference array has an invalid extent");
            for(std::int32_t index=0;index<reference_count;++index) {
                auto* reference=reinterpret_cast<void*>(game_memory::read<std::uintptr_t>(references+static_cast<std::size_t>(index)*8));
                if(reference && !call_object_method(entry("mark_as_reachable"),reference,&fault))failed("UE::GC::MarkAsReachable");
            }
            std::vector<std::uint8_t> observed(bound.bytecode.size());
        const bool copied=game_memory::copy(observed.data(),script,observed.size());
        const auto vm_entry=game_memory::read<std::uintptr_t>(address+0xd8);
        if(!copied || observed!=bound.bytecode || !vm_entry)
            throw HandlerError("runtime_authoring_script_publication_failed","The generated script or bound VM entry differs after publication",
                {{"function",function->name},{"function_pointer",address},{"flags",game_memory::read<std::uint32_t>(address+0xb0)},
                 {"vm_entry",vm_entry},{"bytecode_copied",copied},{"expected_bytecode",bytecode_hex(bound.bytecode)},
                 {"observed_bytecode",copied?json(bytecode_hex(observed)):json(nullptr)}});
            const auto identity=validate_object(object);
            function_results.push_back({{"function",object_info(object,identity.index)},
                {"flags",game_memory::read<std::uint32_t>(address+0xb0)},{"parameter_count",game_memory::read<std::uint8_t>(address+0xb4)},
                {"parameter_bytes",game_memory::read<std::uint16_t>(address+0xb6)},{"script",{{"byte_count",observed.size()},
                {"bytecode_hex",bytecode_hex(observed)},{"vm_entry",game_memory::read<std::uintptr_t>(address+0xd8)},
                {"references",bound.referenced_identities},{"gc_reference_count",reference_count}}}});
        }
        stage="default_object";void* defaults{};
        const auto cdo_method=game_memory::read<std::uintptr_t>(game_memory::read<std::uintptr_t>(class_address)+0x3f8);
        if(cdo_method!=entry("create_default_object"))throw HandlerError("runtime_authoring_class_layout_changed","The generated class has an unexpected default-object constructor");
        if(!call_default_object(cdo_method,klass,&defaults,&fault))failed("UClass::CreateDefaultObject");
        if(!defaults || game_memory::read<std::uintptr_t>(class_address+0x110)!=reinterpret_cast<std::uintptr_t>(defaults))
            throw HandlerError("runtime_authoring_default_object_failed","The generated class did not retain its real default object");
        const auto class_identity=validate_object(klass),default_identity=validate_object(defaults);
        if(default_identity.object_class!=klass)throw HandlerError("runtime_authoring_default_object_failed","The generated default object has the wrong class");
        stage="member_defaults";
        json default_values=json::object();
        for(const auto& member:program.members)if(const auto found=member_defaults.find(member.symbol);found!=member_defaults.end()) {
            const auto field=symbols.at(member.symbol);
            const auto offset=game_memory::read<std::int32_t>(field+0x44),size=game_memory::read<std::int32_t>(field+0x34);
            const auto class_size=game_memory::read<std::int32_t>(class_address+0x58);
            if(offset<0 || size<1 || offset>class_size-size)
                throw HandlerError("runtime_authoring_default_layout_invalid","The member default exceeds the generated CDO extent");
            const auto value_address=reinterpret_cast<std::uintptr_t>(defaults)+offset;
            const auto& value=found->second;
            if(member.type.kind=="bool") {
                const auto byte_offset=game_memory::read<std::uint8_t>(field+0x71),mask=game_memory::read<std::uint8_t>(field+0x73);
                auto observed=game_memory::read<std::uint8_t>(value_address+byte_offset);
                observed=value.get<bool>()?static_cast<std::uint8_t>(observed|mask):static_cast<std::uint8_t>(observed&~mask);
                write(value_address+byte_offset,observed);
            }else if(member.type.kind=="int")write(value_address,value.get<std::int32_t>());
            else if(member.type.kind=="int64")write(value_address,value.get<std::int64_t>());
            else if(member.type.kind=="float")write(value_address,value.get<float>());
            else if(member.type.kind=="double")write(value_address,value.get<double>());
            else if(member.type.kind=="name")write(value_address,make_name(value.get<std::string>()));
            else if(member.type.kind=="string") {
                const auto text=package_name_utf16(value.get<std::string>());
                void* allocation{};
                if(!call_reallocate(base_+bindings_.realloc.rva,nullptr,text.size()*sizeof(wchar_t),&allocation,&fault))failed("FString default allocation");
                if(!allocation || !game_memory::copy(allocation,text.data(),text.size()*sizeof(wchar_t)))
                    throw HandlerError("runtime_authoring_default_copy_failed","The string default allocation could not be copied");
                const auto previous=game_memory::read<std::uintptr_t>(value_address);
                write(value_address,reinterpret_cast<std::uintptr_t>(allocation));
                write(value_address+8,static_cast<std::int32_t>(text.size()));write(value_address+12,static_cast<std::int32_t>(text.size()));
                if(previous && !call_free(base_+bindings_.free.rva,reinterpret_cast<void*>(previous),&fault))failed("FString previous default cleanup");
            }
            default_values[member.name]=property_value(value_address,property_kind(member.type),size,field);
        }
        RuntimeAuthoredRecord compiled_record{base_,class_address,reinterpret_cast<std::uintptr_t>(package),
            reinterpret_cast<std::uintptr_t>(defaults),class_identity.index,class_identity.serial,
            parsed.package_name+"."+parsed.asset_name,asset,rooted_items,{}};
        compiled_record.root_ledger.reserve(rooted_items.size());
        for(const auto item:rooted_items) {
            auto* rooted=reinterpret_cast<void*>(game_memory::read<std::uintptr_t>(item));
            const auto identity=serial_identity(validate_object(rooted));
            if(item_for(rooted)!=item || !(game_memory::read<std::uint32_t>(item+8)&0x40000000u))
                throw HandlerError("runtime_authoring_root_changed","A newly added authoring root changed before publication");
            compiled_record.root_ledger.add_owned_root({item,identity});
        }
        // Persistence describes this compiled revision. Capture all authored
        // scalar defaults, including implicit defaults, before exposing CDO.
        for(const auto& member:program.members) {
            if(member.type.array || member.type.kind=="struct")continue;
            const auto field=symbols.at(member.symbol);
            const auto offset=game_memory::read<std::int32_t>(field+0x44),size=game_memory::read<std::int32_t>(field+0x34);
            const auto class_size=game_memory::read<std::int32_t>(class_address+0x58);
            if(offset<0 || size<1 || offset>class_size-size)
                throw HandlerError("runtime_authoring_default_layout_invalid","The compiled member default exceeds the CDO extent");
            const auto kind=property_kind(member.type);
            compiled_record.scalar_defaults.push_back({field,offset,size,member.name,kind,
                property_value(reinterpret_cast<std::uintptr_t>(defaults)+offset,kind,size,field)});
        }
        compiled_record.class_identity=serial_identity(class_identity);
        const auto revision_id=new_revision_id();
        // A live class is published only after all properties/functions/scripts
        // and its CDO are attributable. Persistent package publication is a
        // separate operation and is never inferred from object creation.
        json response={{"source","runtime_blueprint_compilation"},{"revision_id",revision_id},{"compiled",true},{"runtime_published",true},
            {"package_published",false},{"persisted",false},{"asset_path",parsed.package_name+"."+parsed.asset_name},
            {"class",object_info(klass,class_identity.index)},{"default_object",object_info(defaults,default_identity.index)},
            {"functions",std::move(function_results)},{"member_defaults",std::move(default_values)},{"bindings",bindings.describe()},
            {"thread_id",GetCurrentThreadId()},{"retained_root_items",rooted_items},
            {"effects",{{"executed",true},{"may_have_executed",true},{"do_not_retry",false}}}};
        authored_records.emplace(revision_id,std::move(compiled_record));
        return response;
    }catch(const HandlerError& error) {
        for(auto it=rooted_items.rbegin();it!=rooted_items.rend();++it)call_root(entry("clear_root_flags"),*it,&fault);
        throw HandlerError(error.code(),error.what(),{{"compiled",false},{"runtime_published",false},{"persisted",false},
            {"stage",stage},{"executed",mutated},{"may_have_executed",mutated},{"do_not_retry",mutated},
            {"class_pointer",reinterpret_cast<std::uintptr_t>(klass)},{"detail",error.details()}});
    }catch(const rb::CompilerError& error) {
        for(auto it=rooted_items.rbegin();it!=rooted_items.rend();++it)call_root(entry("clear_root_flags"),*it,&fault);
        throw HandlerError(error.code,error.what(),{{"node",error.node},{"pin",error.pin},{"stage",stage},
            {"compiled",false},{"executed",mutated},{"may_have_executed",mutated},{"do_not_retry",mutated}});
    }
}

json GameAccess::runtime_release_blueprint(const json& params) {
    namespace lifetime=runtime_authoring_lifecycle;
    const auto found=authored_record(params,base_,[&](const auto& handle){return resolve(handle);});
    auto& ledger=found->second.root_ledger;
    const auto item_for=[&](const ObjectSerialIdentity& identity) {
        const auto chunks=game_memory::read<std::uintptr_t>(base_+bindings_.object_array.rva);
        const auto chunk=game_memory::read<std::uintptr_t>(chunks+static_cast<std::size_t>(identity.index>>16)*8);
        return chunk+static_cast<std::size_t>(identity.index&65535)*24;
    };
    // Initial weak references can allocate a previously zero serial while
    // RootSet keeps the same UObject alive. Refresh only confirmed roots,
    // atomically, before starting retirement; preserve before/after evidence.
    refresh_active_roots(found->second,[&](const auto& root) {
            const auto current=serial_identity(validate_object(reinterpret_cast<void*>(root.identity.object)));
            if(item_for(current)!=root.item)
                throw HandlerError("runtime_authoring_root_identity_changed","The rooted object registry item changed");
            return lifetime::RootIdentityObservation{root.item,current,(game_memory::read<std::uint32_t>(root.item+8)&0x40000000u)!=0};
    });
    if(!ledger.retirement_requested()
       && !same_serial_identity(found->second.class_identity,
           serial_identity(validate_object(reinterpret_cast<void*>(found->second.klass)))))
        throw HandlerError("runtime_authoring_class_identity_changed","The active authored class identity changed before retirement");
    const auto observer=[&](const lifetime::OwnedRoot& root)->lifetime::RootObservation {
        try {
            const auto current=serial_identity(validate_object(reinterpret_cast<void*>(root.identity.object)));
            if(!same_serial_identity(current,root.identity) || item_for(current)!=root.item)
                return {lifetime::ObservationState::identity_changed,false};
            return {lifetime::ObservationState::valid,(game_memory::read<std::uint32_t>(root.item+8)&0x40000000u)!=0};
        }catch(const HandlerError&){return {lifetime::ObservationState::unavailable,false};}
    };
    const bool reconcile=params.value("reconcile",false);
    native_exception::Diagnostic fault{};
    lifetime::ReleaseResult result;
    if(reconcile)result=ledger.reconcile(observer);
    else if(ledger.snapshot().state==lifetime::ReleaseState::released || ledger.snapshot().unknown_count)
        result=ledger.snapshot();
    else {
        const auto bindings=runtime_authoring::discover(base_);
        const auto clear=bindings.address(base_,"clear_root_flags");
        result=ledger.release(observer,[&](const auto& root){return call_root(clear,root.item,&fault);});
    }
    const char* state=result.state==lifetime::ReleaseState::released?"released":
        result.state==lifetime::ReleaseState::identity_changed?"identity_changed":
        result.state==lifetime::ReleaseState::observation_unavailable?"observation_unavailable":
        result.state==lifetime::ReleaseState::release_unknown?"release_unknown":"retained";
    json roots=json::array(),refreshes=json::array();
    for(const auto& root:ledger.roots())roots.push_back({{"item",root.item},{"object",root.identity.object},
        {"index",root.identity.index},{"serial",root.identity.serial},{"serial_coverage",serial_coverage(root.identity)},
        {"state",root.state==lifetime::RootState::released?"released":root.state==lifetime::RootState::unknown?"unknown":"retained"}});
    for(const auto& refresh:ledger.identity_refreshes())refreshes.push_back({{"item",refresh.item},
        {"before_serial",refresh.before.serial},{"after_serial",refresh.after.serial},
        {"before_class_serial",refresh.before.class_serial},{"after_class_serial",refresh.after.class_serial}});
    return {{"source","runtime_blueprint_retirement"},{"revision_id",found->first},{"state",state},{"asset_path",found->second.asset_path},
        {"retirement_requested",result.retirement_requested},{"released_count",result.released_count},
        {"retained_count",result.retained_count},{"unknown_count",result.unknown_count},{"failed_item",result.failed_item},
        {"roots",std::move(roots)},{"identity_refreshes",std::move(refreshes)},
        {"reconciled",reconcile},{"objects_destroyed",false},{"gc_observed",false},
        {"effects",{{"executed",result.attempted},{"may_have_executed",result.unknown_count!=0},{"do_not_retry",result.unknown_count!=0}}}};
}

json GameAccess::runtime_save_package(const json& params) {
    namespace rp=runtime_package;
    namespace ra=runtime_archive;
    if(!params.contains("output_directory") || !params.at("output_directory").is_string())
        throw HandlerError("invalid_runtime_package_request","output_directory is required");
    const auto record=authored_record(params,base_,[&](const auto& handle){return resolve(handle);});
    if(record->second.root_ledger.retirement_requested())
        throw HandlerError("runtime_authored_revision_retired","Saving requires an active retained compiled revision",
            {{"serialized",false},{"persisted",false},{"executed",false},{"may_have_executed",false}});
    refresh_active_roots(record->second,[&](const auto& root) {
        const auto current=serial_identity(validate_object(reinterpret_cast<void*>(root.identity.object)));
        const auto chunks=game_memory::read<std::uintptr_t>(base_+bindings_.object_array.rva);
        const auto chunk=game_memory::read<std::uintptr_t>(chunks+static_cast<std::size_t>(current.index>>16)*8);
        const auto item=chunk+static_cast<std::size_t>(current.index&65535)*24;
        if(item!=root.item)throw HandlerError("runtime_authoring_root_identity_changed","The rooted object registry item changed");
        return runtime_authoring_lifecycle::RootIdentityObservation{item,current,(game_memory::read<std::uint32_t>(item+8)&0x40000000u)!=0};
    });
    auto* klass=reinterpret_cast<void*>(record->second.klass);
    const auto class_address=reinterpret_cast<std::uintptr_t>(klass);
    const auto identity=validate_object(klass);
    if(!same_serial_identity(record->second.class_identity,serial_identity(identity)))
        throw HandlerError("runtime_authored_class_unavailable","The selected class has no current runtime authoring record");
    for(const auto& variable:record->second.specification.value("variables",json::array())) {
        if(variable.value("container",std::string{})=="array" || variable.value("type",std::string{})=="struct")
            throw HandlerError("runtime_package_compound_defaults_pending",
                "Saving compound authored member defaults requires a verified deep revision guard",
                {{"serialized",false},{"persisted",false},{"executed",false},{"may_have_executed",false},
                 {"preserved_previous",true}});
    }
    if(game_memory::read<std::uintptr_t>(class_address+0x110)!=record->second.default_object)
        throw HandlerError("runtime_authored_defaults_changed","The compiled class default object was replaced",
            {{"serialized",false},{"persisted",false},{"executed",false},{"may_have_executed",false},{"preserved_previous",true}});
    for(const auto& member:record->second.scalar_defaults) {
        const auto field=member.field;
        if(game_memory::read<std::int32_t>(field+0x44)!=member.offset
           || game_memory::read<std::int32_t>(field+0x34)!=member.size
           || name(game_memory::read<std::uint64_t>(field+0x20))!=member.name
           || game_memory::field_type(field,[&](auto raw){return name(raw);})!=member.kind
           || property_value(record->second.default_object+member.offset,member.kind,member.size,field)!=member.value)
            throw HandlerError("runtime_authored_defaults_changed",
                "An authored member default changed after compilation; compile the intended source revision again",
                {{"member",member.name},{"serialized",false},{"persisted",false},{"executed",false},
                 {"may_have_executed",false},{"preserved_previous",true}});
    }
    const auto original_package=object_path(reinterpret_cast<void*>(record->second.package));
    if(params.contains("package_name") && !params.at("package_name").is_string())
        throw HandlerError("invalid_runtime_package_request","package_name must be a string");
    const auto package_name=params.value("package_name",original_package);
    const auto validated_package=asset_path::parse(package_name+".RuntimePackageAsset");
    if(validated_package.package_name!=package_name)
        throw HandlerError("invalid_package_name","The output package name must be canonical");
    if(params.contains("raw_only") && !params.at("raw_only").is_boolean())
        throw HandlerError("invalid_runtime_package_request","raw_only must be a boolean");
    const bool raw_only=params.value("raw_only",false);
    const auto mount_end=package_name.find('/',1);
    const auto mount=package_name.substr(1,mount_end-1);
    const auto original_mount_end=original_package.find('/',1);
    if(mount!=original_package.substr(1,original_mount_end-1)
       || package_name.substr(package_name.find_last_of('/')+1)!=original_package.substr(original_package.find_last_of('/')+1))
        throw HandlerError("runtime_package_identity_mismatch","A saved candidate must retain its content mount and asset basename");
    if(params.contains("plugin_id") && !params.at("plugin_id").is_string())
        throw HandlerError("invalid_runtime_package_request","plugin_id must be a string");
    const auto plugin_id=params.value("plugin_id",mount);
    if(!raw_only && (plugin_id!=mount || plugin_id=="Game" || plugin_id=="Engine" || plugin_id=="Script"
       || plugin_id.empty() || plugin_id.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")!=std::string::npos))
        throw HandlerError("runtime_package_plugin_mismatch","A containerized authored package must belong to its exact content-plugin mount");
    const auto filename=package_name.substr(package_name.find_last_of('/')+1);
    if(params.contains("container_name") && !params.at("container_name").is_string())
        throw HandlerError("invalid_runtime_package_request","container_name must be a string");
    const auto container_name=params.value("container_name",filename);
    if(!raw_only && (container_name.empty() || container_name=="." || container_name==".."
       || container_name.find_first_not_of("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-.")!=std::string::npos))
        throw HandlerError("invalid_container_name","container_name must be a nonempty local filename stem");
    const auto output_text=params.at("output_directory").get<std::string>();
    if(output_text.empty() || output_text.find('\0')!=std::string::npos)
        throw HandlerError("invalid_output_directory","The output directory must be a nonempty filesystem path");
    const auto output_utf16=package_name_utf16(output_text);
    const std::filesystem::path output_directory(std::wstring(output_utf16.data()));
    if(!output_directory.is_absolute())throw HandlerError("invalid_output_directory","The output directory must be absolute");
    const auto path_text=[](const std::filesystem::path& path) {
        const auto value=path.u8string();
        return std::string(reinterpret_cast<const char*>(value.data()),value.size());
    };
    const auto uasset_path=output_directory/std::filesystem::u8path(filename+".uasset");
    const auto uexp_path=output_directory/std::filesystem::u8path(filename+".uexp");
    const auto pak_path=output_directory/std::filesystem::u8path(container_name+".pak");
    const auto utoc_path=output_directory/std::filesystem::u8path(container_name+".utoc");
    const auto ucas_path=output_directory/std::filesystem::u8path(container_name+".ucas");
    for(const auto& path:raw_only?std::vector<std::filesystem::path>{uasset_path,uexp_path}:
            std::vector<std::filesystem::path>{uasset_path,uexp_path,pak_path,utoc_path,ucas_path})
        if(std::filesystem::exists(path))throw HandlerError("runtime_package_output_exists","The selected output revision already has package files");
    const auto bindings=runtime_authoring::discover(base_);
    const auto entry=[&](const std::string& name){return bindings.address(base_,name);};
    const auto state_ctor=entry("archive_state_constructor"),state_dtor=entry("archive_state_destructor"),cdo_serializer=entry("serialize_default_object"),archetype_entry=entry("get_archetype");
    if(!bindings_.free.available())throw HandlerError("runtime_authoring_allocator_unavailable","The game allocator cleanup is unavailable");
    image_patterns::Image image(base_);
    // The full FArchive table comes from its source-correlated constructor;
    // FArchiveState's own table lacks the serialization methods.
    ra::Bindings archive_bindings{state_ctor,state_dtor,entry("full_archive_vtable"),0};
    rp::Package package;package.name=package_name;
    package.saved_engine={5,6,1,131439,{}};package.compatible_engine=package.saved_engine;
    package.flags=rp::cooked_flag|rp::filter_editor_only_flag;
    std::map<std::uint64_t,rp::NameRef> mapped_names;
    std::map<std::string,std::int32_t> base_names;
    const auto map_name=[&](std::uint64_t raw) {
        if(const auto found=mapped_names.find(raw);found!=mapped_names.end())return found->second;
        // Preserve the raw FName number separately; decoding only its comparison
        // part yields the package-local base name instead of a suffixed string.
        const auto base_name=name(static_cast<std::uint32_t>(raw));
        auto found=base_names.find(base_name);std::int32_t index{};
        if(found==base_names.end()) {
            if(package.names.size()>static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
                throw HandlerError("runtime_package_name_overflow","The package name table exceeds its wire width");
            index=static_cast<std::int32_t>(package.names.size());package.names.push_back(rp::make_name(base_name));base_names.emplace(base_name,index);
        }else index=found->second;
        const rp::NameRef result{index,static_cast<std::int32_t>(raw>>32)};mapped_names.emplace(raw,result);return result;
    };
    const auto make_name=[&](const std::string& text){return encode_name_text(json{{"name",text}});};
    map_name(make_name("None"));
    std::vector<std::uintptr_t> objects{class_address};
    std::vector<std::uintptr_t> registry_objects;
    std::set<std::uintptr_t> included{class_address};
    // All contained exports are harvested before serialization. References to
    // other packages become actual imports with recursively mapped Outer.
    const auto total=game_memory::read<std::int32_t>(base_+bindings_.object_array.rva+0x14);
    for(int index=0;index<total;++index) {
        auto* object=slot(index);if(!object)continue;
        try {
            const auto pointer=reinterpret_cast<std::uintptr_t>(object);
            validate_object(object);registry_objects.push_back(pointer);
            if(pointer==class_address)continue;
            const auto path=object_path(object);
            if(path.starts_with(original_package+".") && included.insert(pointer).second)objects.push_back(pointer);
        }catch(const HandlerError&){}
    }
    if(!included.contains(record->second.default_object))
        throw HandlerError("runtime_package_default_object_missing","The authored default object is absent from the package exports");
    std::map<std::uintptr_t,std::int32_t> object_indices;
    for(std::size_t index=0;index<objects.size();++index)object_indices[objects[index]]=static_cast<std::int32_t>(index+1);
    object_indices[record->second.package]=0;
    std::function<std::int32_t(std::uintptr_t)> map_object;
    std::set<std::uintptr_t> mapping;
    map_object=[&](std::uintptr_t object)->std::int32_t {
        if(!object)return 0;
        if(const auto found=object_indices.find(object);found!=object_indices.end())return found->second;
        if(!mapping.insert(object).second)throw HandlerError("runtime_package_import_cycle","A referenced object's Outer hierarchy contains a cycle");
        const auto actual=validate_object(reinterpret_cast<void*>(object));
        const auto klass_pointer=reinterpret_cast<std::uintptr_t>(actual.object_class);
        std::uintptr_t class_package=klass_pointer;std::set<std::uintptr_t> class_outers;
        while(const auto outer=game_memory::read<std::uintptr_t>(class_package+0x20)) {
            if(!class_outers.insert(class_package).second)throw HandlerError("runtime_package_import_cycle","The imported class Outer hierarchy contains a cycle");
            class_package=outer;
        }
        const auto outer=game_memory::read<std::uintptr_t>(object+0x20);
        const auto outer_index=map_object(outer);
        if(package.imports.size()>=static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
            throw HandlerError("runtime_package_import_overflow","The package imports exceed their wire width");
        const auto index=-static_cast<std::int32_t>(package.imports.size()+1);
        package.imports.push_back({map_name(game_memory::read<std::uint64_t>(class_package+0x18)),
            map_name(game_memory::read<std::uint64_t>(klass_pointer+0x18)),
            map_name(game_memory::read<std::uint64_t>(object+0x18)),outer_index,false});
        object_indices.emplace(object,index);mapping.erase(object);return index;
    };
    ra::Mappers mappers;
    mappers.name=map_name;mappers.object=map_object;
    mappers.field=[&](std::uintptr_t field) {
        ra::FieldPath result;std::set<std::uintptr_t> visited;
        for(auto current=field;current;) {
            if(!visited.insert(current).second)throw HandlerError("runtime_package_field_cycle","A field owner hierarchy contains a cycle");
            const auto flags=game_memory::read<std::uint64_t>(current+0x38);
            if(flags&0x800000000ull){result.filtered_editor_only=true;return result;}
            result.path.push_back(map_name(game_memory::read<std::uint64_t>(current+0x20)));
            const auto owner=game_memory::read<std::uintptr_t>(current+0x10);
            if(owner&1) {result.owner=map_object(owner&~std::uintptr_t{1});break;}
            current=owner;
        }
        return result;
    };
    mappers.indirect_object=[&](ra::IndirectObject kind,std::uintptr_t wrapper) {
        if(kind==ra::IndirectObject::weak_object_ptr) {
            const auto index=game_memory::read<std::int32_t>(wrapper),serial=game_memory::read<std::int32_t>(wrapper+4);
            if(index<0 || !serial)return std::uintptr_t{};
            const auto actual=slot_identity(index);
            return actual.object && actual.serial==serial?reinterpret_cast<std::uintptr_t>(actual.object):std::uintptr_t{};
        }
        const auto handle=game_memory::read<std::uintptr_t>(wrapper);
        if(!handle)return std::uintptr_t{};
        // Resolved FObjectPtr carries its actual aligned UObject address.
        // An encoded unresolved handle requires the target resolver, rather
        // than being treated as an object address by a format writer.
        if(handle&1)throw HandlerError("runtime_package_object_handle_unresolved","The serializer encountered an unresolved object handle");
        validate_object(reinterpret_cast<void*>(handle));return handle;
    };
    native_exception::Diagnostic fault{};
    bool output_attempted=false;
    std::vector<std::filesystem::path> completed_outputs;
    std::map<std::uintptr_t,std::uintptr_t> archetypes;
    if(game_memory::read<std::uintptr_t>(game_memory::read<std::uintptr_t>(class_address)+0x140)!=entry("blueprint_class_preload_dependencies")
       || game_memory::read<std::uintptr_t>(game_memory::read<std::uintptr_t>(reinterpret_cast<std::uintptr_t>(identity.object_class))+0x140)!=entry("class_preload_dependencies"))
        throw HandlerError("runtime_package_preload_binding_mismatch","The live class preload methods differ from their source-correlated Shipping bindings");
    std::vector<rp::Export> export_metadata;
    export_metadata.reserve(objects.size());
    const auto dependency=[&](std::vector<std::int32_t>& target,std::int32_t index,std::uintptr_t current) {
        if(!index || index==object_indices.at(current))return;
        const auto referenced=std::find_if(object_indices.begin(),object_indices.end(),[&](const auto& item){return item.second==index;});
        if(referenced==object_indices.end())throw HandlerError("runtime_package_dependency_unmapped","A preload dependency has no mapped runtime object");
        std::uintptr_t outermost=referenced->first;
        std::set<std::uintptr_t> visited;
        while(const auto outer=game_memory::read<std::uintptr_t>(outermost+0x20)) {
            if(!visited.insert(outermost).second)throw HandlerError("runtime_package_import_cycle","A dependency Outer hierarchy contains a cycle");
            outermost=outer;
        }
        // Stock SavePackage2 assumes CoreUObject constructors do not load
        // assets and excludes that package from preload dependency edges.
        if(object_path(reinterpret_cast<void*>(outermost))=="/Script/CoreUObject")return;
        if(std::find(target.begin(),target.end(),index)==target.end())target.push_back(index);
    };
    const auto nested_under=[&](std::uintptr_t object,std::uintptr_t outer) {
        std::set<std::uintptr_t> visited;
        for(auto parent=game_memory::read<std::uintptr_t>(object+0x20);parent;
            parent=game_memory::read<std::uintptr_t>(parent+0x20)) {
            if(parent==outer)return true;
            if(!visited.insert(parent).second)
                throw HandlerError("runtime_package_import_cycle","A subobject Outer hierarchy contains a cycle");
        }
        return false;
    };
    const auto nontransient_archetype=[&](std::uintptr_t object) {
        std::set<std::uintptr_t> visited;
        while(game_memory::read<std::uint32_t>(object+8)&0x40u) {
            if(!visited.insert(object).second)
                throw HandlerError("runtime_package_archetype_cycle","A transient subobject archetype chain contains a cycle");
            std::uintptr_t previous{};
            if(!call_archetype(archetype_entry,object,&previous,&fault))
                throw HandlerError("runtime_package_archetype_exception","The engine subobject archetype lookup raised a native exception",
                    {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},
                     {"fault",native_exception::describe(fault)}});
            if(!previous)throw HandlerError("runtime_package_archetype_missing","A transient subobject has no archetype");
            const auto actual=validate_object(reinterpret_cast<void*>(previous));
            if(game_memory::read<std::uint32_t>(reinterpret_cast<std::uintptr_t>(actual.object_class)+0xd4)&0x10000080u)
                break;
            object=previous;
        }
        return object;
    };
    for(const auto object:objects) {
        const auto actual=validate_object(reinterpret_cast<void*>(object));
        const auto object_class=reinterpret_cast<std::uintptr_t>(actual.object_class);
        std::uintptr_t archetype{};
        if(!call_archetype(archetype_entry,object,&archetype,&fault))
            throw HandlerError("runtime_package_archetype_exception","The engine archetype lookup raised a native exception",
                {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},{"fault",native_exception::describe(fault)}});
        if(!archetype)throw HandlerError("runtime_package_archetype_missing","The engine returned no archetype for a cooked export");
        validate_object(reinterpret_cast<void*>(archetype));
        const auto flags=game_memory::read<std::uint32_t>(object+8);
        const auto expected_class=(flags&0x10u)?game_memory::read<std::uintptr_t>(object_class+0x40):object_class;
        if(!expected_class || !class_is(reinterpret_cast<void*>(archetype),object_path(reinterpret_cast<void*>(expected_class))))
            throw HandlerError("runtime_package_archetype_class_mismatch","The export archetype does not match the required object/default-object class");
        archetypes.emplace(object,archetype);
        rp::Export export_record;export_record.name=map_name(actual.name);
        export_record.class_index=map_object(object_class);
        export_record.template_index=map_object(archetype);
        if(!export_record.template_index)throw HandlerError("runtime_package_archetype_unmapped","The cooked export archetype mapped to a null package index");
        export_record.outer_index=map_object(game_memory::read<std::uintptr_t>(object+0x20));
        export_record.object_flags=flags&~(0x80u|0x40u|0x10000000u);
        if(class_is(reinterpret_cast<void*>(object),"/Script/CoreUObject.Struct"))
            export_record.super_index=map_object(game_memory::read<std::uintptr_t>(object+0x40));
        export_record.is_asset=object==class_address;
        dependency(export_record.dependencies.serialization_before_create,export_record.class_index,object);
        dependency(export_record.dependencies.serialization_before_create,export_record.template_index,object);
        // SavePackage2 requires template subobjects before object creation,
        // separately from the virtual preload dependencies used for loading.
        for(const auto subobject:registry_objects)
            if((game_memory::read<std::uint32_t>(subobject+8)&(0x40000u|0x20u)) && nested_under(subobject,archetype))
                dependency(export_record.dependencies.serialization_before_create,
                    map_object(nontransient_archetype(subobject)),object);
        dependency(export_record.dependencies.create_before_create,export_record.super_index,object);
        dependency(export_record.dependencies.create_before_create,export_record.outer_index,object);
        if((flags&(0x10u|0x20u)) && export_record.outer_index)
            dependency(export_record.dependencies.serialization_before_serialization,export_record.outer_index,object);
        if(object==class_address)for(const auto subobject:objects) {
            if(!(game_memory::read<std::uint32_t>(subobject+8)&(0x40000u|0x20u))
               || !nested_under(subobject,record->second.default_object))continue;
            std::uintptr_t subobject_archetype{};
            if(!call_archetype(archetype_entry,subobject,&subobject_archetype,&fault))
                throw HandlerError("runtime_package_archetype_exception","The engine default-subobject archetype lookup raised a native exception",
                    {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},
                     {"fault",native_exception::describe(fault)}});
            if(!subobject_archetype)
                throw HandlerError("runtime_package_archetype_missing","A default subobject has no archetype");
            validate_object(reinterpret_cast<void*>(subobject_archetype));
            dependency(export_record.dependencies.serialization_before_serialization,
                map_object(nontransient_archetype(subobject_archetype)),object);
        }
        ObjectDependencyArray preload{};
        const auto method=game_memory::read<std::uintptr_t>(game_memory::read<std::uintptr_t>(object)+0x140);
        if(!image.contains(method-base_))throw HandlerError("runtime_package_preload_binding_unavailable","The object's preload dependency method is outside the game image");
        const bool preload_returned=call_preload_dependencies(object,&preload,&fault);
        try {
            if(!preload_returned)throw HandlerError("runtime_package_preload_exception","The engine preload dependency method raised a native exception",
                {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},{"fault",native_exception::describe(fault)}});
            if(preload.count<0 || preload.capacity<preload.count || (preload.capacity && !preload.data)
               || (preload.data && static_cast<std::size_t>(preload.capacity)>
                   (std::numeric_limits<std::uintptr_t>::max()-preload.data)/sizeof(std::uintptr_t)))
                throw HandlerError("runtime_package_preload_array_invalid","The engine returned an invalid object dependency array");
            for(std::int32_t index=0;index<preload.count;++index)
                dependency(export_record.dependencies.serialization_before_serialization,
                    map_object(game_memory::read<std::uintptr_t>(preload.data+static_cast<std::size_t>(index)*sizeof(std::uintptr_t))),object);
        }catch(...) {
            if(preload.data) {
                native_exception::Diagnostic cleanup_fault{};
                if(!call_free(base_+bindings_.free.rva,reinterpret_cast<void*>(preload.data),&cleanup_fault))
                    throw HandlerError("runtime_package_preload_cleanup_failed",
                        "The engine dependency-array cleanup failed after dependency collection failed",
                        {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},
                         {"fault",native_exception::describe(cleanup_fault)}});
            }
            throw;
        }
        if(preload.data && !call_free(base_+bindings_.free.rva,reinterpret_cast<void*>(preload.data),&fault))
            throw HandlerError("runtime_package_preload_cleanup_failed","The engine object dependency array cleanup raised a native exception",
                {{"executed",true},{"may_have_executed",true},{"persisted",false},{"do_not_retry",true},
                 {"fault",native_exception::describe(fault)}});
        export_metadata.push_back(std::move(export_record));
    }
    const auto previous_cooked=game_memory::read<std::uint8_t>(class_address+0xd0);
    write(class_address+0xd0,std::uint8_t{1});
    try {
        package.exports.reserve(objects.size());
        for(std::size_t object_index=0;object_index<objects.size();++object_index) {
            const auto object=objects[object_index];
            rp::Export export_record=std::move(export_metadata[object_index]);
            ra::Options options;options.filter_editor_only=true;options.unversioned_properties=false;
            options.serializing_defaults=object==record->second.default_object?1:0;
            const auto result=ra::serialize(object,[&](std::uintptr_t selected,void* archive) {
                const bool okay=selected==record->second.default_object?
                    call_serialize_defaults(cdo_serializer,class_address,selected,archive,&fault):call_serialize(selected,archive,&fault);
                if(!okay)throw ra::ArchiveError("runtime_serializer_native_exception","The engine serializer raised a native exception");
            },archive_bindings,mappers,options);
            if(result.ue4_version!=package.ue4_version || result.ue5_version!=package.ue5_version
               || result.licensee_version!=package.licensee_version)
                throw HandlerError("runtime_package_serializer_schema_mismatch",
                    "The live serializer file versions differ from the package writer's established schema",
                    {{"ue4_version",result.ue4_version},{"ue5_version",result.ue5_version},
                     {"licensee_version",result.licensee_version}});
            export_record.data=result.data;
            export_record.script_serialization_start=result.script_serialization_start;
            export_record.script_serialization_end=result.script_serialization_end;
            for(const auto& version:result.custom_versions) {
                auto found=std::find_if(package.custom_versions.begin(),package.custom_versions.end(),[&](const auto& existing){return existing.guid==version.guid;});
                if(found==package.custom_versions.end())package.custom_versions.push_back(version);
                else if(found->version!=version.version)throw HandlerError("runtime_package_custom_version_changed","Serializers disagree on a custom version");
            }
            for(const auto& reference:export_record.data.references) {
                if(reference.kind==rp::ReferenceKind::name || !reference.object)continue;
                // The class can refer to its own CDO, but requiring that CDO's
                // creation before class serialization would make a cycle.
                if(object==class_address && reference.object==object_indices.at(record->second.default_object))continue;
                dependency(export_record.dependencies.create_before_serialization,reference.object,object);
            }
            package.exports.push_back(std::move(export_record));
        }
        package.names_referenced_from_exports=static_cast<std::int32_t>(package.names.size());
        json serialized_custom_versions=json::array();
        for(const auto& version:package.custom_versions)
            serialized_custom_versions.push_back({{"guid",version.guid},{"version",version.version}});
        // PackageFileSummary writes no custom-version container for an
        // unversioned cooked file, while serializers retain their actual
        // registered versions. Loading restores FCurrentCustomVersions.
        if(package.unversioned_file)package.custom_versions.clear();
        const auto cooked=rp::write(package);
        // Validate the produced wire before any output-file publication.
        const auto roundtrip=rp::read(cooked.uasset,cooked.uexp);
        if(roundtrip.package.exports.size()!=package.exports.size())
            throw HandlerError("runtime_package_validation_failed","The emitted package export count differs after parsing");
        rp::IoStorePackage io_store;
        rp::PakArchive pak;
        std::string package_filename;
        if(!raw_only) {
            const auto relative_package=package_name.substr(mount_end+1);
            package_filename="../../../Subnautica2/Mods/"+plugin_id+"/Content/"+relative_package+".uasset";
            rp::IoStoreOptions options;options.container_name=container_name;options.package_filename=package_filename;
            io_store=rp::write_io_store(package,cooked,options);
            const json provenance{{"schema",1},{"kind","sn2_runtime_authored_package"},{"plugin_id",plugin_id},
                {"package_name",package_name},{"original_runtime_package",original_package},{"container_name",container_name},
                {"package_id",io_store.id},{"export_count",package.exports.size()}};
            const auto text=provenance.dump(2)+"\n";
            const rp::PakFile metadata{"MCPRuntime/"+container_name+".json",{text.begin(),text.end()}};
            const rp::PakOptions pak_options{"../../../Subnautica2/Mods/"+plugin_id+"/",0};
            pak=rp::write_pak(pak_options,std::span<const rp::PakFile>(&metadata,1));
            const auto inspected=rp::read_pak(pak.bytes);
            if(inspected.files.size()!=1 || inspected.files.front().bytes!=metadata.bytes)
                throw HandlerError("runtime_package_validation_failed","The emitted Pak provenance did not roundtrip");
        }
        std::filesystem::create_directories(output_directory);
        const auto save=[&](const std::filesystem::path& path,const std::vector<std::uint8_t>& bytes) {
            output_attempted=true;
            std::ofstream output(path,std::ios::binary|std::ios::trunc);
            if(!output || !output.write(reinterpret_cast<const char*>(bytes.data()),static_cast<std::streamsize>(bytes.size())))
                throw HandlerError("runtime_package_output_failed","The serialized package output could not be written");
            output.close();
            if(!output)throw HandlerError("runtime_package_output_failed","The serialized package output did not close successfully");
            completed_outputs.push_back(path);
        };
        save(uasset_path,cooked.uasset);save(uexp_path,cooked.uexp);
        if(!raw_only){save(pak_path,pak.bytes);save(utoc_path,io_store.utoc);save(ucas_path,io_store.ucas);}
        write(class_address+0xd0,previous_cooked);
        json exports=json::array();
        for(std::size_t index=0;index<objects.size();++index)exports.push_back({{"path",object_path(reinterpret_cast<void*>(objects[index]))},
            {"archetype",object_path(reinterpret_cast<void*>(archetypes.at(objects[index])))},
            {"class_index",package.exports[index].class_index},{"template_index",package.exports[index].template_index},
            {"super_index",package.exports[index].super_index},{"outer_index",package.exports[index].outer_index},
            {"bytes",package.exports[index].data.bytes.size()},{"references",package.exports[index].data.references.size()}});
        json outputs=json::array();
        if(!raw_only) {
            outputs.push_back({{"path",path_text(pak_path)},{"bytes",pak.bytes.size()},{"role","pak"}});
            outputs.push_back({{"path",path_text(utoc_path)},{"bytes",io_store.utoc.size()},{"role","utoc"}});
            outputs.push_back({{"path",path_text(ucas_path)},{"bytes",io_store.ucas.size()},{"role","ucas"}});
        }
        return {{"source","runtime_package_serialization"},{"serialized",true},{"persisted",true},
            {"package_name",package_name},{"original_package_name",original_package},{"plugin_id",plugin_id},
            {"package_filename",package_filename},{"outputs",std::move(outputs)},
            {"uasset_path",path_text(uasset_path)},{"uexp_path",path_text(uexp_path)},
            {"uasset_bytes",cooked.uasset.size()},{"uexp_bytes",cooked.uexp.size()},
            {"exports",std::move(exports)},{"name_count",package.names.size()},{"import_count",package.imports.size()},
            {"serialization_versions",{{"unversioned_file",package.unversioned_file},
                {"ue4_version",package.ue4_version},{"ue5_version",package.ue5_version},
                {"licensee_version",package.licensee_version},{"custom_versions",std::move(serialized_custom_versions)}}},
            {"fresh_load_verified",false},{"containerized",!raw_only},{"effects",{{"executed",true},{"may_have_executed",true},{"do_not_retry",false}}}};
    }catch(const ra::ArchiveError& error) {
        write(class_address+0xd0,previous_cooked);
        throw HandlerError(error.code,error.what(),{{"serialized",false},{"persisted",output_attempted},{"output_state",output_attempted?"partial_or_uncertain":"not_written"},
            {"executed",true},{"may_have_executed",true},{"do_not_retry",true}});
    }catch(const rp::PackageError& error) {
        write(class_address+0xd0,previous_cooked);
        throw HandlerError(error.code,error.what(),{{"serialized",false},{"persisted",output_attempted},{"output_state",output_attempted?"partial_or_uncertain":"not_written"},
            {"executed",true},{"may_have_executed",true},{"do_not_retry",true}});
    }catch(const HandlerError& error) {
        write(class_address+0xd0,previous_cooked);
        json completed=json::array();for(const auto& path:completed_outputs)completed.push_back(path_text(path));
        throw HandlerError(error.code(),error.what(),{{"serialized",false},{"persisted",output_attempted},
            {"output_state",output_attempted?"partial_or_uncertain":"not_written"},{"completed_outputs",std::move(completed)},
            {"detail",error.details()},{"executed",true},{"may_have_executed",true},{"do_not_retry",true}});
    }catch(const std::exception& error) {
        write(class_address+0xd0,previous_cooked);
        json completed=json::array();for(const auto& path:completed_outputs)completed.push_back(path_text(path));
        throw HandlerError("runtime_package_output_failed",error.what(),{{"serialized",false},{"persisted",output_attempted},
            {"output_state",output_attempted?"partial_or_uncertain":"not_written"},{"completed_outputs",std::move(completed)},
            {"executed",true},{"may_have_executed",true},{"do_not_retry",true}});
    }catch(...) {write(class_address+0xd0,previous_cooked);throw;}
}
} // namespace sn2_mcp
