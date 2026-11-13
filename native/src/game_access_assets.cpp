#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/asset_path.hpp"
#include "sn2_mcp/function_contract.hpp"
#include "sn2_mcp/game_memory.hpp"
#include "sn2_mcp/native_exception.hpp"
#include "sn2_mcp/process_identity.hpp"
#include <cstring>
#include <set>

namespace sn2_mcp {
using json=nlohmann::json;
namespace {
using game_memory::read;
constexpr auto library_path="/Script/Engine.Default__KismetSystemLibrary";
constexpr auto library_class_path="/Script/Engine.KismetSystemLibrary";
constexpr std::uint64_t direction_mask=0x80|0x100|0x400|0x2|0x8000000;

json identity_json(const ObjectSerialIdentity& value) {
    return {{"object",value.object},{"object_class",value.object_class},
        {"index",value.index},{"name",value.name},{"serial",value.serial},
        {"class_index",value.class_index},{"class_name",value.class_name},
        {"class_serial",value.class_serial},{"serial_coverage",serial_coverage(value)}};
}

// Only the actual engine call crosses SEH; no C++ owned objects live here.
bool call_asset_event(void* entry,void* object,void* function,void* parameters,
                      native_exception::Diagnostic* fault) noexcept {
    __try {
        reinterpret_cast<void(__fastcall*)(void*,void*,void*)>(entry)(object,function,parameters);
        return true;
    } __except(native_exception::capture(GetExceptionInformation(),fault)) {return false;}
}
}

json GameAccess::capture_asset_load_contract(void* library,std::uintptr_t function,bool class_asset) {
    const auto target=validate_object(library);
    validate_class(target.object_class);
    const auto library_address=reinterpret_cast<std::uintptr_t>(library);
    const auto owner=reinterpret_cast<std::uintptr_t>(target.object_class);
    const auto callable=validate_object(reinterpret_cast<void*>(function));
    const auto function_name=class_asset?"LoadClassAsset_Blocking":"LoadAsset_Blocking";
    if(object_path(library)!=library_path || object_path(target.object_class)!=library_class_path
       || !(read<std::uint32_t>(library_address+8)&0x10)
       || !class_is(reinterpret_cast<void*>(function),"/Script/CoreUObject.Function")
       || object_path(reinterpret_cast<void*>(function))!=std::string(library_class_path)+"."+function_name
       || read<std::uintptr_t>(function+0x20)!=owner)
        throw HandlerError("unsupported_asset_load_contract","The loader is not the exact KismetSystemLibrary CDO and UFunction");
    const auto owner_identity=validate_object(target.object_class);
    const auto flags=read<std::uint32_t>(function+0xb0);
    const auto count=read<std::uint8_t>(function+0xb4);
    const auto bytes=read<std::uint16_t>(function+0xb6);
    const auto return_offset=read<std::uint16_t>(function+0xb8);
    if((flags&0x2400)!=0x2400 || count!=2 || bytes!=asset_path::parameter_extent
       || return_offset!=asset_path::return_offset)
        throw HandlerError("unsupported_asset_load_contract","The blocking loader must be a static native function with a 48-byte parameter frame");

    const auto head=read<std::uintptr_t>(function+0x50);
    auto fields=capture_function_field_chain(head,[&](std::uintptr_t field) {
        const auto field_class=read<std::uintptr_t>(field+8);
        const auto field_name=read<std::uint64_t>(field+0x20);
        const auto type_name=read<std::uint64_t>(field_class);
        return json{{"pointer",field},{"next",read<std::uintptr_t>(field+0x18)},
            {"owner",read<std::uintptr_t>(field+0x10)},{"field_class",field_class},
            {"type_name",type_name},{"type",name(type_name)},{"name_value",field_name},
            {"name",name(field_name)},{"flags",read<std::uint64_t>(field+0x38)},
            {"offset",read<std::int32_t>(field+0x44)},{"size",read<std::int32_t>(field+0x34)},
            {"array_dim",read<std::int32_t>(field+0x30)}};
    });
    json input=nullptr,returned=nullptr;
    for(auto& field:fields) {
        const auto field_flags=field.at("flags").get<std::uint64_t>();
        if(!(field_flags&0x80))continue;
        if(field.at("array_dim")!=1)
            throw HandlerError("unsupported_asset_load_contract","The soft-reference loader requires scalar parameters");
        const bool is_return=(field_flags&0x400)!=0;
        if((field_flags&direction_mask)!=(is_return?std::uint64_t{0x580}:std::uint64_t{0x80}))
            throw HandlerError("unsupported_asset_load_contract","The soft-reference loader parameter direction is unsupported");
        const auto expected_type=is_return?(class_asset?"ClassProperty":"ObjectProperty")
            :(class_asset?"SoftClassProperty":"SoftObjectProperty");
        if(field.at("type")!=expected_type || field.at("name")!=(is_return?"ReturnValue":class_asset?"AssetClass":"Asset")
           || field.at("size")!=(is_return?8:40) || field.at("offset")!=(is_return?40:0))
            throw HandlerError("unsupported_asset_load_contract","The loader's reflected type, name, size or offset differs from the verified soft-reference layout");
        const auto pointer=field.at("pointer").get<std::uintptr_t>();
        const auto declared=read<std::uintptr_t>(pointer+0x70);
        validate_class(reinterpret_cast<void*>(declared));
        const auto expected_declared=class_asset?"/Script/CoreUObject.Class":"/Script/CoreUObject.Object";
        if(object_path(reinterpret_cast<void*>(declared))!=expected_declared)
            throw HandlerError("unsupported_asset_load_contract","The loader's declared UObject or UClass type is unsupported");
        field["declared"]=identity_json(serial_identity(validate_object(reinterpret_cast<void*>(declared))));
        field["declared_path"]=object_path(reinterpret_cast<void*>(declared));
        if(class_asset) {
            const auto meta=read<std::uintptr_t>(pointer+0x78);
            validate_class(reinterpret_cast<void*>(meta));
            if(object_path(reinterpret_cast<void*>(meta))!="/Script/CoreUObject.Object")
                throw HandlerError("unsupported_asset_load_contract","The loader's soft-class metaclass must be CoreUObject.Object");
            field["meta"]=identity_json(serial_identity(validate_object(reinterpret_cast<void*>(meta))));
            field["meta_path"]=object_path(reinterpret_cast<void*>(meta));
        }
        auto& destination=is_return?returned:input;
        if(!destination.is_null())throw HandlerError("unsupported_asset_load_contract","The loader has duplicate input or return parameters");
        destination=field;
    }
    if(input.is_null() || returned.is_null())
        throw HandlerError("unsupported_asset_load_contract","The loader's soft-reference input and pointer return are required");
    if(!same_serial_identity(serial_identity(target),serial_identity(validate_object(library)))
       || !same_serial_identity(serial_identity(owner_identity),serial_identity(validate_object(target.object_class)))
       || !same_serial_identity(serial_identity(callable),serial_identity(validate_object(reinterpret_cast<void*>(function))))
       || read<std::uintptr_t>(function+0x50)!=head || read<std::uint32_t>(function+0xb0)!=flags
       || read<std::uint8_t>(function+0xb4)!=count || read<std::uint16_t>(function+0xb6)!=bytes
       || read<std::uint16_t>(function+0xb8)!=return_offset)
        throw HandlerError("signature_changed","The blocking loader identity or header changed during inspection");
    return {{"library",identity_json(serial_identity(target))},{"owner",identity_json(serial_identity(owner_identity))},
        {"function",identity_json(serial_identity(callable))},{"function_name",function_name},
        {"function_flags",flags},{"parameter_count",count},{"parameter_size",bytes},
        {"return_offset",return_offset},{"head",head},{"fields",fields},{"input",input},{"return",returned}};
}

json GameAccess::load_asset(const json& params,bool class_asset) {
    const auto names_before=name_constructions_started_.load();
    bool event_started=false,event_returned=false;
    std::string stage="validate_path";
    json invocation_identity=nullptr;
    const auto world_before=world_generation_.load();
    const auto session_before=handle_session_;
    auto effects=[&]() {
        return json{{"executed",event_returned},{"may_have_executed",event_started && !event_returned},
            {"process_event_started",event_started},{"process_event_returned",event_returned},
            {"do_not_retry",event_started},{"name_pool_may_have_changed",name_constructions_started_.load()!=names_before},
            {"package_load_may_have_occurred",event_started},{"constructors_may_have_run",event_started}};
    };
    try {
        if(GetCurrentThreadId()!=game_thread_.load())
            throw HandlerError("inspection_wrong_thread","Asset loading requires the confirmed game Tick thread");
        require_runtime_operation("invoke");
        if(!process_event_)
            throw HandlerError("process_event_unavailable","The executable ProcessEvent binding is unavailable");
        if(!params.is_object() || !params.contains("asset_path") || !params.at("asset_path").is_string())
            throw HandlerError("invalid_asset_path","A canonical asset_path string is required");
        const auto requested=params.at("asset_path").get<std::string>();
        const auto path=asset_path::parse(requested);
        stage="resolve_library";
        const auto count=read<std::int32_t>(base_+bindings_.object_array.rva+0x14);
        if(count<0)throw HandlerError("invalid_registry","The object registry count is invalid");
        void* library{};
        for(std::int32_t index=0;index<count;++index) {
            const auto object=slot(index);
            if(!object)continue;
            bool matches=false;
            try {matches=object_name(object)=="Default__KismetSystemLibrary" && object_path(object)==library_path;}
            catch(const HandlerError&) {continue;}
            if(!matches)continue;
            validate_object(object);
            if(library && library!=object)
                throw HandlerError("asset_loader_identity_mismatch","The KismetSystemLibrary CDO path is not unique");
            library=object;
        }
        if(!library)throw HandlerError("asset_loader_unavailable","The KismetSystemLibrary CDO is not present");
        const auto library_identity=validate_object(library);
        const auto owner=reinterpret_cast<std::uintptr_t>(library_identity.object_class);
        std::uintptr_t function{};
        std::set<std::uintptr_t> visited;
        const auto function_name=class_asset?"LoadClassAsset_Blocking":"LoadAsset_Blocking";
        for(auto field=read<std::uintptr_t>(owner+0x48);field;field=read<std::uintptr_t>(field+0x28)) {
            if(!visited.insert(field).second)
                throw HandlerError("unsupported_asset_load_contract","The KismetSystemLibrary UField chain contains a cycle");
            if(object_name(reinterpret_cast<void*>(field))!=function_name)continue;
            if(function && function!=field)
                throw HandlerError("asset_loader_identity_mismatch","The blocking loader function name is not unique");
            function=field;
        }
        if(!function)throw HandlerError("asset_loader_unavailable","The blocking reflected asset loader is unavailable");
        stage="verify_contract";
        const auto contract=capture_asset_load_contract(library,function,class_asset);
        if(capture_asset_load_contract(library,function,class_asset)!=contract)
            throw HandlerError("signature_changed","The blocking loader contract changed during inspection");
        invocation_identity={{"session_id",session_before},{"world_generation",world_before},
            {"process_id",GetCurrentProcessId()},{"process_creation_filetime",sn2::loader::creation_filetime(GetCurrentProcess())},
            {"library",contract.at("library")},{"function",contract.at("function")},
            {"abi",{{"soft_reference_size",40},{"package_name_offset",8},{"asset_name_offset",16},
                {"subpath_offset",24},{"parameter_size",48},{"return_offset",40}}}};
        stage="prepare_soft_reference";
        const auto package_name=encode_name_text({{"name",path.package_name}});
        const auto asset_name=encode_name_text({{"name",path.asset_name}});
        auto frame=asset_path::parameter_frame(package_name,asset_name);
        if(capture_asset_load_contract(library,function,class_asset)!=contract)
            throw HandlerError("signature_changed","The blocking loader contract changed after FName preparation");
        if(handle_session_!=session_before || world_generation_.load()!=world_before)
            throw HandlerError("world_changed","The session or world changed before asset loading");
        stage="process_event";
        native_exception::Diagnostic fault{};
        event_started=true;++process_events_started_;
        event_returned=call_asset_event(reinterpret_cast<void*>(process_event_),library,
            reinterpret_cast<void*>(function),frame.data(),&fault);
        if(event_returned)++process_events_returned_;
        if(!event_returned)
            throw HandlerError("game_call_fault","Asset loading raised a native exception; its package and constructor effects are unknown",
                {{"native_exception",native_exception::describe(fault)}});
        stage="verify_result";
        if(capture_asset_load_contract(library,function,class_asset)!=contract)
            throw HandlerError("signature_changed","The blocking loader contract changed after the call");
        std::uint64_t cached{},returned{};
        std::uintptr_t subpath{};std::int32_t subpath_count{},subpath_capacity{};
        std::memcpy(&cached,frame.data(),sizeof(cached));
        std::uint64_t package_after{},asset_after{};
        std::memcpy(&package_after,frame.data()+8,sizeof(package_after));
        std::memcpy(&asset_after,frame.data()+16,sizeof(asset_after));
        std::memcpy(&subpath,frame.data()+24,sizeof(subpath));
        std::memcpy(&subpath_count,frame.data()+32,sizeof(subpath_count));
        std::memcpy(&subpath_capacity,frame.data()+36,sizeof(subpath_capacity));
        if(package_after!=package_name || asset_after!=asset_name || subpath || subpath_count || subpath_capacity)
            throw HandlerError("asset_path_lifetime_unknown","The top-level soft-reference path storage changed after loading");
        std::memcpy(&returned,frame.data()+asset_path::return_offset,sizeof(returned));
        const auto& output=contract.at("return");
        const auto declared=output.at("declared").at("object").get<std::uintptr_t>();
        auto descriptor=class_asset?class_descriptor(reinterpret_cast<void*>(returned),reinterpret_cast<void*>(declared),
                reinterpret_cast<void*>(output.at("meta").at("object").get<std::uintptr_t>()))
            :object_descriptor(reinterpret_cast<void*>(returned),reinterpret_cast<void*>(declared));
        if(returned) {
            const auto loaded=validate_object(reinterpret_cast<void*>(returned));
            invocation_identity["loaded"]=identity_json(serial_identity(loaded));
            const auto actual=object_path(reinterpret_cast<void*>(returned));
            invocation_identity["requested_path"]=requested;
            invocation_identity["actual_path"]=actual;
            invocation_identity["exact_path_match"]=actual==requested;
        }
        refresh_world();
        if(handle_session_!=session_before || world_generation_.load()!=world_before)
            throw HandlerError("world_changed","The session or world changed while loading the asset");
        invocation_identity["soft_reference_cache"]=cached;
        auto effect=effects();effect["do_not_retry"]=false;
        return {{"stage",returned?"loaded":"not_found"},{"asset_path",requested},
            {"kind",class_asset?"class":"asset"},{"value",descriptor},{"effects",effect},
            {"identity",invocation_identity},{"ownership",{{"parameter_buffer","bridge"},
                {"soft_reference_subpath","empty; no owned allocation"},{"loaded_object","engine"},
                {"handle","observation; does not root the UObject"}}}};
    } catch(const HandlerError& error) {
        auto details=error.details();details.update(effects());details["stage"]=stage;
        details["identity"]=invocation_identity;
        details["ownership"]={{"parameter_buffer","bridge"},{"loaded_object","engine"},
            {"soft_reference_subpath","empty at preparation; no automatic retry or native destruction"}};
        throw HandlerError(error.code(),error.what(),std::move(details));
    } catch(const std::exception& error) {
        auto details=effects();details["stage"]=stage;details["identity"]=invocation_identity;
        throw HandlerError("asset_load_error",error.what(),std::move(details));
    }
}
} // namespace sn2_mcp
