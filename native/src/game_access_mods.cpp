#include <sn2_mcp/game_access.hpp>
#include <sn2_mcp/blueprint_mod_record.hpp>
#include <sn2_mcp/asset_path.hpp>
#include <sn2_mcp/game_memory.hpp>
#include <sn2_mcp/transport.hpp>
#include <sn2_mcp/transform_arguments.hpp>
#include <set>

namespace sn2_mcp {
using json=nlohmann::json;
using game_memory::read;

namespace {
json neutral_transform() {
    return {{"Rotation",{{"X",0},{"Y",0},{"Z",0},{"W",1}}},
            {"Translation",{{"X",0},{"Y",0},{"Z",0}}},
            {"Scale3D",{{"X",1},{"Y",1},{"Z",1}}}};
}
void validate_transform_value(const json& value) {
    transform_arguments::validate_keys(value,{"Rotation","Translation","Scale3D"},
        "spawn_transform requires only Rotation, Translation and Scale3D");
    transform_arguments::validate_keys(value.at("Rotation"),{"X","Y","Z","W"},
        "spawn_transform Rotation requires X, Y, Z and W");
    for(const auto* key:{"X","Y","Z","W"})double_argument(value.at("Rotation").at(key));
    for(const auto* part:{"Translation","Scale3D"}) {
        transform_arguments::validate_keys(value.at(part),{"X","Y","Z"},
            "spawn_transform vectors require X, Y and Z");
        for(const auto* key:{"X","Y","Z"})double_argument(value.at(part).at(key));
    }
}
std::string required_text(const json& params,const char* key) {
    if(!params.is_object() || !params.contains(key) || !params.at(key).is_string())
        throw HandlerError("invalid_blueprint_mod_params",std::string(key)+" must be a nonempty string");
    auto value=params.at(key).get<std::string>();
    if(value.empty() || value.find('\0')!=std::string::npos)
        throw HandlerError("invalid_blueprint_mod_params",std::string(key)+" must be nonempty and contain no NUL");
    return value;
}
json identity_json(const ObjectSerialIdentity& actor) {
    return {{"index",actor.index},{"serial",actor.serial},{"name",actor.name},
            {"class_index",actor.class_index},{"class_serial",actor.class_serial},
            {"class_name",actor.class_name},{"serial_coverage",serial_coverage(actor)}};
}
}

json GameAccess::blueprint_mod_call(BlueprintModRecord& record,void* object,
                                  const std::string& function,const json& arguments,
                                  const std::string& stage) {
    const auto started_before=process_events_started_.load();
    const auto returned_before=process_events_returned_.load();
    auto effects=[&]() {
        const bool started=process_events_started_.load()!=started_before;
        const bool returned=process_events_returned_.load()!=returned_before;
        return json{{"executed",returned},{"may_have_executed",started && !returned},
                    {"process_event_started",started},{"process_event_returned",returned},
                    {"do_not_retry",started}};
    };
    try {
        const auto identity=validate_object(object);
        auto response=invoke(object,function,arguments,handle_for(object,identity.index),nullptr);
        auto receipt=json{{"stage",stage},{"function",function},{"tick",ticks_.load()},
                          {"world_generation",world_generation_.load()},{"outcome","returned"},
                          {"effects",effects()},{"response",response}};
        record.receipts.push_back(std::move(receipt));
        return response;
    } catch(const HandlerError& error) {
        auto details=error.details();details.update(effects());
        record.receipts.push_back({{"stage",stage},{"function",function},{"tick",ticks_.load()},
            {"world_generation",world_generation_.load()},{"outcome","error"},
            {"code",error.code()},{"message",error.what()},{"effects",details}});
        throw HandlerError(error.code(),error.what(),std::move(details));
    } catch(const std::exception& error) {
        auto details=effects();
        record.receipts.push_back({{"stage",stage},{"function",function},{"tick",ticks_.load()},
            {"world_generation",world_generation_.load()},{"outcome","error"},
            {"code","blueprint_mod_call_error"},{"message",error.what()},{"effects",details}});
        throw HandlerError("blueprint_mod_call_error",error.what(),std::move(details));
    }
}

void GameAccess::blueprint_mod_observe(BlueprintModRecord& record) {
    if(!record.actor_retained)return;
    auto observation=json{{"tick",ticks_.load()},{"identity",identity_json(record.actor)},
                          {"actor_world_generation",record.actor_world_generation},
                          {"selected_world_generation",world_generation_.load()},
                          {"authored_effect_observed",false},{"end_play_observed",false}};
    auto ended=[&](const char* state,const char* reason) {
        observation["state"]=state;observation["method"]=reason;
        record.confirm_actor_ended(state);
        record.actor_observation=observation;
        record.receipts.push_back({{"stage","actor_end_observation"},{"tick",ticks_.load()},
                                  {"observation",observation}});
    };
    try {
        if(read<std::int32_t>(base_+bindings_.object_array.rva+0x14)<0)
            throw HandlerError("invalid_registry","The object registry count is invalid");
        const auto item=slot_identity(record.actor.index);
        const auto klass=slot_identity(record.actor.class_index);
        if(item.object==reinterpret_cast<void*>(record.actor.object) && item.serial!=record.actor.serial
           && (record.actor_identity_unresolved || record.actor.serial==0)) {
            record.actor_identity_unresolved=true;
            observation["state"]="identity_unresolved";
            observation["method"]="serial_changed_during_unresolved_finish";
            observation["observed_serial"]=item.serial;
            record.actor_observation=std::move(observation);return;
        }
        if(item.object!=reinterpret_cast<void*>(record.actor.object) || item.serial!=record.actor.serial
           || klass.object!=reinterpret_cast<void*>(record.actor.object_class) || klass.serial!=record.actor.class_serial) {
            ended("actor_invalidated","registry_slot_identity");return;
        }
        const auto current=serial_identity(validate_object(item.object));
        if(!same_serial_identity(record.actor,current)) {
            ended("actor_invalidated","validated_object_identity");return;
        }
        observation["state"]="alive";
        observation["method"]="validated_object_identity";
        if(record.destroy_requested && !record.destroy_observation_attempted) {
            record.destroy_observation_attempted=true;
            auto result=blueprint_mod_call(record,item.object,"IsActorBeingDestroyed",json::object(),"destroy_observation");
            if(!result.contains("result") || !result.at("result").is_boolean())
                throw HandlerError("unsupported_destroy_observation","IsActorBeingDestroyed did not return a boolean");
            if(result.at("result").get<bool>()) {
                ended("destroying_observed","IsActorBeingDestroyed");return;
            }
            record.state="destroy_pending";
        }
        record.actor_observation=std::move(observation);
    } catch(const HandlerError& error) {
        if(error.code()=="object_destroying") {
            ended("destroying_observed","UObject_destroying_flags");return;
        }
        observation["state"]="unknown";
        observation["cause_code"]=error.code();observation["message"]=error.what();
        record.actor_observation=std::move(observation);
    } catch(const std::exception& error) {
        observation["state"]="unknown";observation["message"]=error.what();
        record.actor_observation=std::move(observation);
    }
}

json GameAccess::blueprint_mod_describe(BlueprintModRecord& record) {
    json actor=nullptr;
    if(record.actor_retained) {
        actor={{"identity",identity_json(record.actor)},{"world_generation",record.actor_world_generation},
               {"entry_class_path",record.actor_class_path},{"package_revision",record.actor_package_revision},
               {"source_revision",record.actor_source_revision},{"revision_evidence","activation_correlation_labels"}};
        if(record.actor_world_generation==world_generation_.load()) {
            try {
                const auto object=reinterpret_cast<void*>(record.actor.object);
                if(same_serial_identity(record.actor,serial_identity(validate_object(object))))
                    actor["object"]=object_info(object,record.actor.index);
            } catch(const HandlerError& error) {
                actor["handle_error"]={{"code",error.code()},{"message",error.what()}};
            }
        }
    }
    return {{"mod_id",record.mod_id},{"entry_class_path",record.entry_class_path},
        {"package_revision",record.package_revision},{"source_revision",record.source_revision},
        {"revision_evidence","caller_correlation_labels"},{"desired_enabled",record.desired_enabled},
        {"state",record.state},{"actor",actor},{"actor_observation",record.actor_observation},
        {"world_generation",world_generation_.load()},{"attempted_world_generation",record.attempted_world_generation},
        {"activation_attempts",record.activation_attempts},{"spawn_transform",record.spawn_transform},
        {"unknown_operations",{{"load",record.load_unknown},{"spawn",record.spawn_unknown},
            {"finish",record.finish_unknown},{"destroy",record.destroy_unknown},
            {"actor_identity",record.actor_identity_unresolved}}},
        {"receipts",record.receipts},{"last_error",record.last_error},
        {"effect_observed",false},{"effect_evidence_source","independent_authored_effect_check_required"},
        {"package_unloaded",false},{"residency","loaded_classes_and_packages_remain_until_process_exit"},
        {"disconnect_behavior","engine_owned_actor_retained"},{"thread_id",GetCurrentThreadId()}};
}

json GameAccess::blueprint_mod_destroy(BlueprintModRecord& record,const std::string& reason) {
    blueprint_mod_observe(record);
    if(!record.actor_retained)return blueprint_mod_describe(record);
    if(record.actor_identity_unresolved) {
        record.state="actor_identity_unresolved";
        return blueprint_mod_describe(record);
    }
    if(!record.actor_cleanup_verified) {
        record.state="spawn_identity_unverified";
        return blueprint_mod_describe(record);
    }
    if(record.destroy_attempted) {
        blueprint_mod_observe(record);
        return blueprint_mod_describe(record);
    }
    try {
        record.destroy_attempted=true;
        const auto object=reinterpret_cast<void*>(record.actor.object);
        if(!same_serial_identity(record.actor,serial_identity(validate_object(object))))
            throw HandlerError("stale_mod_actor","The retained mod actor identity changed");
        const auto response=blueprint_mod_call(record,object,"K2_DestroyActor",json::object(),"destroy_actor");
        record.destroy_requested=true;
        record.state="destroy_pending";
        record.receipts.push_back({{"stage","destroy_request"},{"reason",reason},{"tick",ticks_.load()}});
        blueprint_mod_observe(record);
    } catch(const HandlerError& error) {
        const auto& effects=error.details();
        const bool started=effects.value("process_event_started",false);
        record.destroy_requested=started;
        record.destroy_unknown=started;
        record.state=started?"destroy_unknown":"destroy_failed";
        record.last_error={{"code",error.code()},{"message",error.what()},{"effects",effects}};
        blueprint_mod_observe(record);
    }
    return blueprint_mod_describe(record);
}

json GameAccess::blueprint_mod_activate(BlueprintModRecord& record) {
    record.begin_activation(world_generation_.load());
    const auto generation=world_generation_.load();
    const auto world=current_world_;
    std::string stage="load_class";
    bool spawn_identity_unresolved=false;
    bool finish_completed=false;
    bool finish_returned=false;
    json stage_effects={{"executed",false},{"may_have_executed",false},
                       {"process_event_started",false},{"process_event_returned",false},{"do_not_retry",false}};
    try {
        if(!world.object || !generation)
            throw HandlerError("world_unavailable","A confirmed selected world is required for a Blueprint entry actor");
        const auto loaded=load_asset({{"asset_path",record.entry_class_path}},true);
        if(loaded.contains("effects"))stage_effects=loaded.at("effects");
        record.receipts.push_back({{"stage","load_class"},{"tick",ticks_.load()},
                                  {"world_generation",generation},{"response",loaded}});
        if(!loaded.contains("value") || loaded.at("value").is_null())
            throw HandlerError("blueprint_mod_class_not_found","The requested cooked entry class was not loaded");
        auto actor_class=resolve(loaded.at("value").at("handle").get<std::string>());
        validate_class(actor_class);
        std::set<std::uintptr_t> parents;
        bool actor_type=false;
        for(auto parent=reinterpret_cast<std::uintptr_t>(actor_class);parent;parent=read<std::uintptr_t>(parent+0x40)) {
            if(!parents.insert(parent).second)
                throw HandlerError("invalid_object_class","The represented class hierarchy contains a cycle");
            validate_class(reinterpret_cast<void*>(parent));
            if(object_path(reinterpret_cast<void*>(parent))=="/Script/Engine.Actor") {actor_type=true;break;}
        }
        if(!actor_type)throw HandlerError("blueprint_mod_entry_not_actor","The entry class must derive from Engine.Actor");
        stage="validate_world";
        refresh_world();
        if(generation!=world_generation_.load() || !current_world_.object
           || !same_serial_identity(serial_identity(world),serial_identity(current_world_)))
            throw HandlerError("world_changed","The selected world changed during blocking class loading");
        if(!same_serial_identity(serial_identity(world),serial_identity(validate_object(world.object))))
            throw HandlerError("world_changed","The selected world identity changed before actor spawning");

        void* library{};
        const auto count=read<std::int32_t>(base_+bindings_.object_array.rva+0x14);
        if(count<0)throw HandlerError("invalid_registry","The object registry count is invalid");
        for(std::int32_t index=0;index<count;++index) {
            auto candidate=slot(index);if(!candidate)continue;
            bool matches=false;
            try {matches=object_name(candidate)=="Default__GameplayStatics"
                    && object_path(candidate)=="/Script/Engine.Default__GameplayStatics";}
            catch(const HandlerError&) {continue;}
            if(!matches)continue;
            validate_object(candidate);
            if(library && candidate!=library)
                throw HandlerError("blueprint_mod_spawn_library_ambiguous","The GameplayStatics CDO path is not unique");
            library=candidate;
        }
        if(!library)throw HandlerError("blueprint_mod_spawn_library_unavailable","The GameplayStatics CDO was not found");
        stage="spawn_actor";
        stage_effects={{"executed",false},{"may_have_executed",false},
                      {"process_event_started",false},{"process_event_returned",false},{"do_not_retry",false}};
        auto spawned=blueprint_mod_call(record,library,"BeginDeferredActorSpawnFromClass",
            {{"WorldContextObject",{{"handle",handle_for(world.object,world.index)}}},
             {"ActorClass",{{"handle",handle_for(actor_class,validate_object(actor_class).index)}}},
             {"SpawnTransform",record.spawn_transform},{"CollisionHandlingOverride",1},
             {"Owner",nullptr},{"TransformScaleMethod",0}},stage);
        stage_effects={{"executed",true},{"may_have_executed",false},
                      {"process_event_started",true},{"process_event_returned",true},{"do_not_retry",true}};
        if(!spawned.contains("result")) {
            spawn_identity_unresolved=true;
            throw HandlerError("blueprint_mod_spawn_result_unknown","Deferred spawn returned no result receipt");
        }
        if(spawned.at("result").is_null())
            throw HandlerError("blueprint_mod_spawn_rejected","Deferred spawn returned no actor");
        spawn_identity_unresolved=true;
        if(!spawned.at("result").is_object() || !spawned.at("result").contains("handle")) {
            record.spawn_unknown=true;
            throw HandlerError("blueprint_mod_spawn_result_unknown","Deferred spawn did not return a usable actor identity");
        }
        auto actor=resolve(spawned.at("result").at("handle").get<std::string>());
        const auto identity=validate_object(actor);
        record.retain_actor(serial_identity(identity),generation);
        spawn_identity_unresolved=false;
        if(!class_is(actor,"/Script/Engine.Actor") || !is_compatible_object(actor,actor_class))
            throw HandlerError("blueprint_mod_spawn_type_mismatch","The returned actor is incompatible with the loaded entry class");
        if(object_world(actor)!=world.object)
            throw HandlerError("blueprint_mod_spawn_world_mismatch","The returned actor does not belong to the selected world");
        record.actor_cleanup_verified=true;
        stage="validate_spawn_world";
        refresh_world();
        if(generation!=world_generation_.load() || !current_world_.object
           || !same_serial_identity(serial_identity(world),serial_identity(current_world_)))
            throw HandlerError("world_changed","The selected world changed during deferred actor spawning");
        stage="finish_actor";
        stage_effects={{"executed",false},{"may_have_executed",false},
                      {"process_event_started",false},{"process_event_returned",false},{"do_not_retry",false}};
        const auto finished=blueprint_mod_call(record,library,"FinishSpawningActor",
            {{"Actor",{{"handle",handle_for(actor,identity.index)}}},
             {"SpawnTransform",record.spawn_transform},{"TransformScaleMethod",0}},stage);
        stage_effects={{"executed",true},{"may_have_executed",false},
                      {"process_event_started",true},{"process_event_returned",true},{"do_not_retry",true}};
        finish_returned=true;
        if(!finished.contains("result") || !finished.at("result").is_object()
           || !finished.at("result").contains("handle"))
            throw HandlerError("blueprint_mod_finish_result_unknown","FinishSpawningActor did not return the retained actor");
        auto completed=resolve(finished.at("result").at("handle").get<std::string>());
        const auto completed_identity=serial_identity(validate_object(completed));
        if(!same_serial_identity(record.actor,completed_identity)) {
            refresh_world();
            const bool same_world=generation==world_generation_.load() && current_world_.object
                && same_serial_identity(serial_identity(world),serial_identity(current_world_))
                && object_world(completed)==world.object;
            if(finish_allocated_actor_serial(record.actor,completed_identity,same_world)) {
                record.receipts.push_back({{"stage","finish_actor_serial_established"},{"tick",ticks_.load()},
                    {"before",identity_json(record.actor)},{"after",identity_json(completed_identity)},
                    {"same_object_pointer",true},{"same_class_pointer",true},{"same_confirmed_world",true},
                    {"criterion","synchronous_Finish_return_same_full_structure_actor_serial_zero_to_positive_exact_class_identity"},
                    {"scope","owned_actor_Finish_only; ordinary_handles_and_background_observations_remain_strict"},
                    {"continuity_limit","initial_zero_serial_does_not_independently_prove_object_lifetime"}});
                record.actor=completed_identity;
            } else {
                record.actor_identity_unresolved=true;
                auto details=stage_effects;
                details["spawn_identity"]=identity_json(record.actor);
                details["finish_identity"]=identity_json(completed_identity);
                details["same_object_pointer"]=record.actor.object==completed_identity.object;
                details["same_class_pointer"]=record.actor.object_class==completed_identity.object_class;
                throw HandlerError("blueprint_mod_finish_identity_mismatch","FinishSpawningActor returned a different actor identity",details);
            }
        }
        record.state="actor_spawned";
        finish_completed=true;
        record.receipts.push_back({{"stage","actor_spawned"},{"tick",ticks_.load()},
            {"actor",identity_json(record.actor)},{"revision_evidence","caller_correlation_labels"},
            {"package_revision",record.package_revision},{"source_revision",record.source_revision},
            {"begin_play_observed",false},{"authored_effect_observed",false}});
        refresh_world();
        blueprint_mod_observe(record);
        if(record.actor_retained && record.actor_world_generation!=world_generation_.load())
            blueprint_mod_destroy(record,"world_changed_during_finish");
    } catch(const HandlerError& error) {
        auto effects=error.details();
        if(stage_effects.value("process_event_returned",false))effects.update(stage_effects);
        effects["result_validated"]=false;
        const bool started=effects.value("process_event_started",false);
        if(stage=="load_class")record.load_unknown=started && !stage_effects.value("process_event_returned",false);
        if(stage=="spawn_actor" && ((!record.actor_retained && (started || spawn_identity_unresolved))
           || (record.actor_retained && !record.actor_cleanup_verified)))record.spawn_unknown=true;
        if(stage=="finish_actor" && !finish_completed)record.finish_unknown=started || finish_returned;
        if(stage=="finish_actor" && !finish_completed && record.actor_retained && (started || finish_returned))
            record.actor_identity_unresolved=true;
        record.last_error={{"stage",stage},{"code",error.code()},{"message",error.what()},{"effects",effects}};
        record.state=record.unresolved_operation()?stage+"_unknown":"activation_failed";
        record.receipts.push_back({{"stage","activation_error"},{"tick",ticks_.load()},{"error",record.last_error}});
        if(record.actor_retained) {
            const auto activation_error=record.last_error;
            blueprint_mod_destroy(record,"failed_activation_cleanup");
            record.last_error=activation_error;
        }
    } catch(const std::exception& error) {
        if(stage=="spawn_actor" && spawn_identity_unresolved)record.spawn_unknown=true;
        if(stage=="finish_actor" && finish_returned && !finish_completed)record.finish_unknown=true;
        if(stage=="finish_actor" && finish_returned && !finish_completed && record.actor_retained)
            record.actor_identity_unresolved=true;
        record.last_error={{"stage",stage},{"code","blueprint_mod_activation_error"},{"message",error.what()},{"effects",stage_effects}};
        record.state=record.unresolved_operation()?stage+"_unknown":"activation_failed";
        record.receipts.push_back({{"stage","activation_error"},{"tick",ticks_.load()},{"error",record.last_error}});
        if(record.actor_retained)blueprint_mod_destroy(record,"failed_activation_cleanup");
    }
    return blueprint_mod_describe(record);
}

json GameAccess::blueprint_mod_enable(const json& params) {
    const auto id=required_text(params,"mod_id");
    const auto path=required_text(params,"entry_class_path");
    asset_path::parse(path);
    const auto revision=required_text(params,"package_revision");
    if(params.contains("source_revision") && !params.at("source_revision").is_string())
        throw HandlerError("invalid_blueprint_mod_params","source_revision must be a string");
    if(params.contains("spawn_transform") && !params.at("spawn_transform").is_object())
        throw HandlerError("invalid_blueprint_mod_params","spawn_transform must be a typed Transform object");
    const auto transform=params.value("spawn_transform",neutral_transform());
    validate_transform_value(transform);
    auto& record=blueprint_mods_[id];
    blueprint_mod_observe(record);
    if(!record.entry_class_path.empty() && record.entry_class_path!=path
       && (record.actor_retained || record.unresolved_operation()))
        throw HandlerError("blueprint_mod_actor_retained","Disable and confirm the existing entry actor before changing its class path",
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false},{"mod",blueprint_mod_describe(record)}});
    record.mod_id=id;record.entry_class_path=path;record.package_revision=revision;
    record.source_revision=params.value("source_revision",std::string{});
    if(!record.actor_retained)record.spawn_transform=transform;
    record.desired_enabled=true;
    if(!record.destroy_requested && !record.destroy_unknown)record.destroy_attempted=false;
    if(record.actor_retained && record.actor_world_generation!=world_generation_.load())
        blueprint_mod_destroy(record,"selected_world_changed");
    if(record.may_activate(world_generation_.load(),true))return blueprint_mod_activate(record);
    if(!record.actor_retained && !record.unresolved_operation() && !current_world_.object)record.state="waiting_for_world";
    return blueprint_mod_describe(record);
}

json GameAccess::blueprint_mod_status(const json& params) {
    if(!params.is_object())throw HandlerError("invalid_blueprint_mod_params","Blueprint mod status params must be an object");
    if(params.contains("mod_id")) {
        const auto id=required_text(params,"mod_id");
        const auto found=blueprint_mods_.find(id);
        if(found==blueprint_mods_.end())return {{"mod_id",id},{"state","not_tracked"},{"desired_enabled",false}};
        found->second.destroy_observation_attempted=false;
        blueprint_mod_observe(found->second);
        return blueprint_mod_describe(found->second);
    }
    auto items=json::array();
    for(auto& [id,record]:blueprint_mods_) {
        record.destroy_observation_attempted=false;
        blueprint_mod_observe(record);items.push_back(blueprint_mod_describe(record));
    }
    return {{"items",items},{"total_count",items.size()},{"thread_id",GetCurrentThreadId()}};
}

json GameAccess::blueprint_mod_disable(const json& params) {
    const auto id=required_text(params,"mod_id");
    const auto found=blueprint_mods_.find(id);
    if(found==blueprint_mods_.end())return {{"mod_id",id},{"state","not_tracked"},{"desired_enabled",false}};
    auto& record=found->second;
    record.desired_enabled=false;
    record.destroy_observation_attempted=false;
    if(!record.destroy_requested && !record.destroy_unknown)record.destroy_attempted=false;
    if(record.actor_retained)return blueprint_mod_destroy(record,"operator_disable");
    if(!record.unresolved_operation())record.state="disabled";
    return blueprint_mod_describe(record);
}

void GameAccess::pump_mods() {
    if(GetCurrentThreadId()!=game_thread_.load() || !reflection_validated_.load())return;
    for(auto& [id,record]:blueprint_mods_) {
        try {
            blueprint_mod_observe(record);
            if(record.actor_retained && record.actor_world_generation!=world_generation_.load())
                blueprint_mod_destroy(record,"selected_world_changed");
            if(record.may_activate(world_generation_.load()))blueprint_mod_activate(record);
        } catch(const std::exception& error) {
            record.last_error={{"stage","tick_observation"},{"message",error.what()}};
        }
    }
}

} // namespace sn2_mcp
