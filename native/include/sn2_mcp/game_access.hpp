#pragma once

#include <Windows.h>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <future>
#include <memory>
#include <map>
#include <mutex>
#include <string>
#include <span>
#include <vector>
#include <unordered_map>
#include <nlohmann/json.hpp>
#include "sn2_mcp/observations.hpp"
#include "sn2_mcp/enum_values.hpp"
#include "sn2_mcp/object_serial.hpp"
#include "sn2_mcp/job_state.hpp"
#include "sn2_mcp/runtime_bindings.hpp"
#include "sn2_mcp/runtime_evidence.hpp"
#include "sn2_mcp/vector_layout.hpp"
#include "sn2_mcp/transform_layout.hpp"
#include "sn2_mcp/linear_color_layout.hpp"
#include "sn2_mcp/blueprint_mod_record.hpp"

namespace sn2_mcp {
namespace scalar_struct_values {struct Snapshot;}
struct ArrayHeader;
struct StringHeader;
class GameAccess {
public:
    bool start();
    nlohmann::json request(const nlohmann::json& envelope);
    void pump();
    nlohmann::json status() const;
    void disconnect(const std::string& session);

private:
    uintptr_t ensure_image_base();
    void validate_runtime_layout();
    void require_runtime_operation(const std::string& operation) const;
    struct Job {
        nlohmann::json request;
        std::promise<nlohmann::json> result;
        JobState state;
        std::uint64_t world_generation{};
    };
    struct Handle {
        void* object{};
        void* object_class{};
        int index{};
        uint64_t name{};
        int32_t serial{};
        int32_t class_index{};
        uint64_t class_name{};
        int32_t class_serial{};
        void* bound_world{};
        std::uint64_t world_generation{};
    };
    struct SlotIdentity {void* object{};int32_t serial{};};
    static ObjectSerialIdentity serial_identity(const Handle& value);
    using TickFn = void(__fastcall*)(void*, float, bool);
    using ProcessEventFn = void(__fastcall*)(void*, void*, void*);
    TickFn original_tick_{};
    ProcessEventFn process_event_{};
    RuntimeBindings bindings_;
    RuntimeEvidence evidence_;
    mutable std::atomic<std::uint64_t> name_constructions_started_{0};
    std::atomic<std::uint64_t> process_events_started_{0};
    std::atomic<std::uint64_t> process_events_returned_{0};
    void observe_result(const std::string& operation,const nlohmann::json& result);
    std::atomic<bool> reflection_validated_{false};
    nlohmann::json reflection_validation_=nullptr;
    std::string reflection_error_;
    mutable std::mutex bootstrap_mutex_;
    uintptr_t base_{};
    std::atomic<bool> enabled_{false};
    std::atomic<DWORD> game_thread_{0};
    std::atomic<uint64_t> ticks_{0};
    std::atomic<uint64_t> last_tick_ms_{0};
    std::atomic<uint64_t> completed_jobs_{0};
    std::atomic<uint64_t> cancelled_jobs_{0};
    // Retained until process exit; a new MCP session does not clear quarantine.
    std::atomic<bool> string_inputs_quarantined_{false};
    std::atomic<std::size_t> string_inputs_retained_count_{0};
    std::atomic<std::size_t> string_inputs_retained_bytes_{0};
    std::atomic<uint32_t> string_inputs_quarantine_cause_{0};
    // Changing MCP sessions does not clear quarantine of the owned array buffer.
    std::atomic<bool> object_array_inputs_quarantined_{false};
    std::atomic<std::size_t> object_array_inputs_retained_count_{0};
    std::atomic<std::size_t> object_array_inputs_retained_bytes_{0};
    std::atomic<uint32_t> object_array_inputs_quarantine_cause_{0};
    std::string startup_error_;
    mutable std::mutex queue_mutex_;
    std::deque<std::shared_ptr<Job>> queue_;
    std::shared_ptr<Job> active_job_;
    std::unordered_map<std::string, Handle> handles_;
    std::map<std::string, nlohmann::json> signature_contracts_;
    uint64_t next_signature_token_{};
    uint64_t next_handle_{0};
    std::string handle_session_;
    std::map<std::string, BlueprintModRecord> blueprint_mods_;
    nlohmann::json blueprint_mod_enable(const nlohmann::json& params);
    nlohmann::json blueprint_mod_status(const nlohmann::json& params);
    nlohmann::json blueprint_mod_disable(const nlohmann::json& params);
    void pump_mods();
    nlohmann::json blueprint_mod_activate(BlueprintModRecord& record);
    nlohmann::json blueprint_mod_describe(BlueprintModRecord& record);
    void blueprint_mod_observe(BlueprintModRecord& record);
    nlohmann::json blueprint_mod_destroy(BlueprintModRecord& record,const std::string& reason);
    nlohmann::json blueprint_mod_call(BlueprintModRecord& record,void* object,
        const std::string& function,const nlohmann::json& arguments,const std::string& stage);
    Handle game_engine_;
    Handle game_viewport_;
    Handle current_world_;
    std::uintptr_t engine_viewport_field_{};
    std::uintptr_t viewport_world_field_{};
    std::atomic<std::uint64_t> world_generation_{0};
    nlohmann::json world_metadata_{{"source","GameEngine.GameViewport.World"},{"state","unavailable"},{"generation",0},{"current",nullptr}};
    void refresh_world();
    void change_world(const Handle& world);
    void* object_world(void* object);
    std::uintptr_t object_field(void* object,const std::string& property_name) const;
    bool class_is(void* object,const std::string& class_path) const;
    bool session_cleanup_pending_{};
    Observations observations_;
    uint64_t next_snapshot_{};

    nlohmann::json execute(const nlohmann::json& request);
    std::string name(uint64_t fname) const;
    std::string object_name(void* object) const;
    std::string object_path(void* object) const;
    std::string handle_for(void* object, int index);
    Handle validate_object(void* object) const;
    void validate_class(void* object_class) const;
    void validate_development_service_call(void* object, void* function_owner,
        const std::string& function_name, std::uintptr_t function, const unsigned char* parameters) const;
    nlohmann::json function_execution_metadata(std::uintptr_t function) const;
    bool is_compatible_object(void* object, void* expected_class) const;
    bool is_compatible_class(void* represented_class, void* property_class, void* meta_class) const;
    nlohmann::json object_descriptor(void* object, void* expected_class);
    nlohmann::json class_descriptor(void* object, void* property_class, void* meta_class);
    nlohmann::json property_value(uintptr_t address, const std::string& type, int size, uintptr_t field);
    nlohmann::json enum_property(uintptr_t address, const std::string& type, int size, uintptr_t field);
    nlohmann::json enum_metadata(void* object, const nlohmann::json& value, const std::string& underlying_type);
    nlohmann::json read_array(const nlohmann::json& params);
    nlohmann::json read_set(const nlohmann::json& params);
    nlohmann::json read_map(const nlohmann::json& params);
    nlohmann::json read_container(const nlohmann::json& params,bool map);
#if defined(SN2_MCP_DEVELOPMENT_PROBES)
    nlohmann::json development_sparse_fixture(const nlohmann::json& params);
    nlohmann::json development_uint64_fixture(const nlohmann::json& params);
    nlohmann::json development_array_fixture(const nlohmann::json& params);
    nlohmann::json development_owned_output_fixture(const nlohmann::json& params);
#endif
    nlohmann::json capture_object_set_return(std::uintptr_t field,int size,int dim) const;
    nlohmann::json read_object_set_return(std::uintptr_t field,std::uintptr_t address,const nlohmann::json& metadata);
    nlohmann::json capture_object_array_return(std::uintptr_t field,int size,int dim,bool output=false) const;
    nlohmann::json read_object_array_return(std::uintptr_t field,std::uintptr_t address,const nlohmann::json& metadata);
    nlohmann::json capture_scalar_array_return(std::uintptr_t field,int size,int dim,bool output=false) const;
    nlohmann::json read_scalar_array_return(std::uintptr_t field,std::uintptr_t address,const nlohmann::json& metadata);
    std::string read_copied_string(const StringHeader& header) const;
    nlohmann::json read_struct(void* object, const std::string& handle, const std::string& property_name);
    nlohmann::json load_asset(const nlohmann::json& params, bool class_asset=false);
    nlohmann::json capture_asset_load_contract(void* library, std::uintptr_t function, bool class_asset);
    nlohmann::json capture_read_value(std::uintptr_t field,int size,int dim) const;
    nlohmann::json capture_pod_struct_parameter(std::uintptr_t field,int size,int dim) const;
    static nlohmann::json pod_struct_metadata(const nlohmann::json& captured);
    std::vector<std::byte> encode_pod_struct(const nlohmann::json& captured,const nlohmann::json& value) const;
    nlohmann::json decode_pod_struct(const nlohmann::json& captured,std::span<const std::byte> copied);
    std::uint64_t encode_name_text(const nlohmann::json& value) const;
    nlohmann::json decode_read_value(const nlohmann::json& metadata,std::span<const std::byte> copied,
                                    std::uintptr_t live_address,std::vector<std::pair<std::uintptr_t,ArrayHeader>>& copied_headers);
    vector_layout::Snapshot capture_vector_parameter(std::uintptr_t field,int size,int dim) const;
    transform_layout::Snapshot capture_transform_parameter(std::uintptr_t field,int size,int dim) const;
    nlohmann::json capture_object_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const;
    nlohmann::json capture_enum_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const;
    nlohmann::json capture_scalar_array_parameter(std::uintptr_t field,int size,int dim,std::uint64_t flags) const;
    linear_color_layout::Snapshot capture_linear_color_parameter(std::uintptr_t field,int size,int dim) const;
    scalar_struct_values::Snapshot capture_scalar_struct_parameter(std::uintptr_t field,int size,int dim) const;
    static nlohmann::json scalar_struct_snapshot_json(const scalar_struct_values::Snapshot& value,bool raw=true);
    scalar_struct_values::Snapshot capture_gameplay_tag_parameter(std::uintptr_t field,int size,int dim) const;
    struct EnumLayout {std::string underlying_type;int size;void* object;};
    struct EnumTable {Handle identity;std::string path;std::vector<sn2_mcp::enum_values::Entry> entries;};
    EnumLayout enum_layout(const std::string& type, int size, uintptr_t field) const;
    EnumTable enum_table(void* object) const;
    void* resolve(const std::string& id);
    void* slot(int index) const;
    SlotIdentity slot_identity(int index) const;
    nlohmann::json object_info(void* object, int index);
    nlohmann::json properties(void* object, std::size_t limit, std::size_t offset);
    nlohmann::json selected_properties(void* object, const nlohmann::json& names);
    nlohmann::json snapshot(const nlohmann::json& targets);
    nlohmann::json capture_function_contract(void* object, uintptr_t owner, uintptr_t function, const std::string& target_handle);
    nlohmann::json functions(void* object, std::size_t limit, std::size_t offset, const std::string& function_query, const std::string& target_handle);
    nlohmann::json invoke_impl(void* object,const std::string& function,const nlohmann::json& arguments,
        const std::string& target_handle,const nlohmann::json* expected_token);
    nlohmann::json invoke(void* object, const std::string& function_name, const nlohmann::json& arguments,
                          const std::string& target_handle, const nlohmann::json* expected_token);
    static void __fastcall hooked_tick(void* engine, float delta_seconds, bool idle_mode);
    static GameAccess* active_;
};
}
