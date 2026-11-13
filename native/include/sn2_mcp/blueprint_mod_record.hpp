#pragma once

#include <sn2_mcp/object_serial.hpp>
#include <nlohmann/json.hpp>
#include <cstdint>
#include <string>

namespace sn2_mcp {

// Only used for the exact returned actor during the synchronous Finish call.
// The engine can allocate its first weak-reference serial during construction;
// ordinary handles, background observations and reconnects remain strict.
inline bool finish_allocated_actor_serial(const ObjectSerialIdentity& before,
                                         const ObjectSerialIdentity& after,
                                         bool same_confirmed_world) noexcept {
    return same_confirmed_world && before.serial==0 && after.serial>0
        && valid_observed_serial(before.class_serial)
        && before.object==after.object && before.object_class==after.object_class
        && before.index==after.index && before.name==after.name
        && before.class_index==after.class_index && before.class_name==after.class_name
        && before.class_serial==after.class_serial;
}

// Engine-owned actor identity survives MCP reconnects. Public handles are
// regenerated from this identity; revision strings are correlation labels.
struct BlueprintModRecord {
    std::string mod_id;
    std::string entry_class_path;
    std::string package_revision;
    std::string source_revision;
    std::string actor_class_path;
    std::string actor_package_revision;
    std::string actor_source_revision;
    nlohmann::json spawn_transform;
    bool desired_enabled{};
    bool actor_retained{};
    bool actor_cleanup_verified{};
    bool actor_identity_unresolved{};
    ObjectSerialIdentity actor;
    std::uint64_t actor_world_generation{};
    std::uint64_t attempted_world_generation{};
    std::uint64_t activation_attempts{};
    bool load_unknown{};
    bool spawn_unknown{};
    bool finish_unknown{};
    bool destroy_unknown{};
    bool destroy_attempted{};
    bool destroy_requested{};
    bool destroy_observation_attempted{};
    std::string state{"disabled"};
    nlohmann::json receipts=nlohmann::json::array();
    nlohmann::json last_error=nullptr;
    nlohmann::json actor_observation=nullptr;

    bool unresolved_operation() const noexcept {
        return load_unknown || spawn_unknown || finish_unknown || destroy_unknown || actor_identity_unresolved;
    }

    bool may_activate(std::uint64_t generation,bool explicit_request=false) const noexcept {
        if(!desired_enabled || !generation || actor_retained || unresolved_operation())return false;
        return explicit_request || !activation_attempts || attempted_world_generation!=generation;
    }

    void begin_activation(std::uint64_t generation) {
        attempted_world_generation=generation;
        ++activation_attempts;
        state="loading_class";
        last_error=nullptr;
        destroy_attempted=false;
        destroy_requested=false;
        destroy_observation_attempted=false;
    }

    void retain_actor(const ObjectSerialIdentity& identity,std::uint64_t generation) {
        actor=identity;
        actor_retained=true;
        actor_cleanup_verified=false;
        actor_identity_unresolved=false;
        actor_world_generation=generation;
        actor_class_path=entry_class_path;
        actor_package_revision=package_revision;
        actor_source_revision=source_revision;
        state="actor_deferred";
    }

    // Only an independent identity/destroying observation can call this.
    // The original receipts and unknown Blueprint side effects remain visible.
    void confirm_actor_ended(const std::string& ended_state) {
        actor_retained=false;
        destroy_unknown=false;
        finish_unknown=false;
        state=ended_state;
    }
};

} // namespace sn2_mcp
