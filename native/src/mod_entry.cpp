#include <Windows.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <deque>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/process_identity.hpp"
#include "sn2_mcp/transport.hpp"
#include "sn2_mod/command.hpp"
#include "sn2_mod/spec.hpp"

namespace {
using json = nlohmann::json;
using Operation = sn2_mod::Operation;
HMODULE module{};

std::string utf8(const std::wstring& text) {
    if(text.empty())return {};
    const int bytes=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),
        static_cast<int>(text.size()),nullptr,0,nullptr,nullptr);
    if(!bytes)throw std::runtime_error("module_path_encoding_failed");
    std::string result(static_cast<std::size_t>(bytes),'\0');
    WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),
        result.data(),bytes,nullptr,nullptr);
    return result;
}

std::string module_path() {
    std::wstring path(32768,L'\0');
    const auto length=GetModuleFileNameW(module,path.data(),static_cast<DWORD>(path.size()));
    if(!length || length>=path.size())throw std::runtime_error("module_path_unavailable");
    path.resize(length);return utf8(path);
}

struct Pending {std::uint64_t id;Operation operation;};
struct Binding {
    std::string handle;
    json target;
    json getter_token;
    json setter_token;
    float baseline{};
    bool baseline_recorded{};
    bool effect_started{};
};

struct State {
    std::mutex mutex;
    HANDLE event{};
    bool worker_started{};
    bool worker_failed{};
    std::deque<Pending> pending;
    std::map<std::uint64_t,json> receipts;
    json snapshot;
    std::uint64_t request_sequence{};

    State() {
        const auto* specification=SN2ModSpecification();
        if(!specification || !specification->mod_id || !specification->source_revision
           || !*specification->mod_id || !*specification->source_revision
           || !std::isfinite(specification->opacity)
           || specification->opacity<0 || specification->opacity>1)
            throw std::runtime_error("invalid_mod_specification");
        event=CreateEventW(nullptr,FALSE,FALSE,nullptr);
        if(!event)throw std::runtime_error("mod_worker_event_failed");
        snapshot={{"kind","native_widget_opacity_mod"},{"command_abi",sn2_mod::CommandAbi},
            {"mod_id",specification->mod_id},{"source_revision",specification->source_revision},
            {"compiled_opacity",specification->opacity},{"module_path",module_path()},
            {"process_id",GetCurrentProcessId()},
            {"process_creation_filetime",std::to_string(sn2::loader::creation_filetime(GetCurrentProcess()))},
            {"state","loaded"},{"initialized",false},{"active",false},{"desired_state","disabled"},
            {"target",nullptr},{"baseline_opacity",nullptr},{"observed_opacity",nullptr},
            {"restoration_required",false},{"restoration_confirmed",nullptr},{"native_status",nullptr},{"last_error",nullptr},
            {"bootstrap_diagnostics",json::array()},
            {"activation_policy","maintain_compiled_opacity_after_fresh_getter"},
            {"maintenance_applications",0},{"last_maintenance",nullptr},
            {"module_resident",true},{"hook_resident",false},{"hot_unload_supported",false},
            {"iteration","controlled_process_restart"}};
    }
};

State& state() {
    // Neither the state nor the GameAccess/trampoline is destroyed while Tick
    // can enter the module. Windows reclaims them when the process exits.
    static State* value=new State();return *value;
}

void update(State& value,const json& fields) {
    std::lock_guard lock(value.mutex);value.snapshot.update(fields);
}

void receipt(State& value,std::uint64_t id,const json& fields) {
    if(!id)return;
    std::lock_guard lock(value.mutex);
    auto& current=value.receipts.at(id);
    const auto previous=current.value("state",std::string{});
    if(previous=="completed" || previous=="failed" || previous=="outcome_unknown"
       || previous=="cancelled" || previous=="superseded")return;
    current.update(fields);
}

json error_json(const std::exception& error) {
    const auto* handler=dynamic_cast<const sn2_mcp::HandlerError*>(&error);
    return {{"code",handler?handler->code():"mod_runtime_failed"},{"message",error.what()},
        {"effect",handler?handler->details():json{{"executed",false},{"may_have_executed",false}}}};
}

class Worker {
public:
    explicit Worker(State& value):state_(value),session_("native-mod-"+std::to_string(GetCurrentProcessId())+
        "-"+std::to_string(reinterpret_cast<std::uintptr_t>(module))) {}

    void run() {
        for(;;) {
            std::optional<Pending> next;
            {
                std::lock_guard lock(state_.mutex);
                if(!state_.pending.empty()){next=state_.pending.front();state_.pending.pop_front();}
            }
            if(next) {
                if(next->operation==Operation::start)start(next->id);
                else disable(next->id);
            }
            if(desired_active_ && !effect_unknown_)advance();
            if(game_)update(state_,{{"native_status",game_->status()}});
            WaitForSingleObject(state_.event,250);
        }
    }

private:
    json request(const char* operation,json parameters) {
        parameters["expected_process_id"]=GetCurrentProcessId();
        parameters["expected_creation_filetime"]=std::to_string(sn2::loader::creation_filetime(GetCurrentProcess()));
        parameters["expected_session_id"]=session_;
        return game_->request({{"op",operation},{"params",std::move(parameters)},
            {"id",++state_.request_sequence},{"session_id",session_},{"wait_timeout_ms",2500}});
    }

    float getter(const Binding& binding) {
        const auto result=request("invoke",{{"handle",binding.handle},{"function","GetRenderOpacity"},
            {"arguments",json::object()},{"expected_signature_token",binding.getter_token}});
        if(!result.value("executed",false) || !result.value("process_event_returned",false)
           || !result.at("result").is_number())throw std::runtime_error("mod_getter_not_confirmed");
        const auto observed=result.at("result").get<float>();
        if(!std::isfinite(observed))throw std::runtime_error("mod_getter_not_finite");
        return observed;
    }

    void setter(Binding& binding,float opacity) {
        // From admission onward, a timeout is an unknown effect until a getter
        // resolves it. It must not trigger an automatic setter retry.
        const bool previous_effect=binding.effect_started;
        binding.effect_started=true;
        setter_uncertain_=true;
        try {
            const auto result=request("invoke",{{"handle",binding.handle},{"function","SetRenderOpacity"},
                {"arguments",{{"InOpacity",opacity}}},{"expected_signature_token",binding.setter_token}});
            if(!result.value("executed",false) || !result.value("process_event_returned",false))
                throw sn2_mcp::HandlerError("mod_setter_not_confirmed","The modification setter outcome is not confirmed",
                    {{"executed",false},{"may_have_executed",true},{"do_not_retry",true}});
            setter_uncertain_=false;
        } catch(const sn2_mcp::HandlerError& error) {
            setter_uncertain_=error.details().value("may_have_executed",false);
            if(!setter_uncertain_ && !error.details().value("executed",false))
                binding.effect_started=previous_effect;
            throw;
        }
    }

    std::optional<Binding> select() {
        json selected=json::array();std::size_t offset{};
        for(;;) {
            const auto found=request("objects",{{"query","TextLabel"},{"class_query","TextBlock"},
                {"offset",offset},{"limit",200}});
            for(const auto& item:found.at("items")) {
                const auto path=item.value("path",std::string{});
                if(path.find("/Engine/Transient.")!=std::string::npos
                   && path.find("WBP_StartScreen_PressStart_C_")!=std::string::npos
                   && path.find(".ContinueBtn.")!=std::string::npos)selected.push_back(item);
            }
            if(!found.value("truncated",false))break;
            const auto next=found.at("next_offset").get<std::size_t>();
            if(next<=offset)throw std::runtime_error("mod_object_scan_did_not_advance");
            offset=next;
        }
        if(selected.empty())return std::nullopt;
        if(selected.size()!=1)throw std::runtime_error("mod_target_not_unique");
        Binding binding;binding.target=selected.at(0);binding.handle=binding.target.at("handle").get<std::string>();
        offset=0;
        for(;;) {
            const auto functions=request("functions",{{"handle",binding.handle},
                {"function_query","RenderOpacity"},{"offset",offset},{"limit",100}});
            for(const auto& item:functions.at("items")) {
                const auto name=item.value("name",std::string{});
                if(name!="GetRenderOpacity" && name!="SetRenderOpacity")continue;
                if(item.value("signature_status",std::string{})!="complete")
                    throw std::runtime_error("mod_widget_signature_unsupported");
                if(name=="GetRenderOpacity")binding.getter_token=item.at("signature_token");
                else binding.setter_token=item.at("signature_token");
            }
            if(!functions.value("truncated",false))break;
            const auto next=functions.at("next_offset").get<std::size_t>();
            if(next<=offset)throw std::runtime_error("mod_function_scan_did_not_advance");
            offset=next;
        }
        if(binding.getter_token.is_null() || binding.setter_token.is_null())
            throw std::runtime_error("mod_widget_signature_missing");
        binding.baseline=getter(binding);binding.baseline_recorded=true;
        return binding;
    }

    void start(std::uint64_t id) {
        if(effect_unknown_) {
            receipt(state_,id,{{"state","failed"},{"stage","prior_outcome_unknown"},
                {"error","Resolve or disable the prior modification effect before activation"}});
            return;
        }
        if(start_id_) {
            std::lock_guard lock(state_.mutex);
            auto& previous=state_.receipts.at(start_id_);
            if(previous.value("state",std::string{})=="pending")
                previous.update({{"state","superseded"},{"superseded_by",id}});
        }
        start_id_=id;desired_active_=true;reapply_requested_=binding_.has_value();
        receipt(state_,id,{{"state","pending"},{"stage","initialize"}});
        update(state_,{{"desired_state","active"},{"state","initializing"},{"last_error",nullptr},
            {"restoration_confirmed",nullptr}});
        if(!game_)game_=new sn2_mcp::GameAccess();
        if(!game_->start()) {
            desired_active_=false;
            update(state_,{{"state","faulted"},{"active",false},{"native_status",game_->status()}});
            receipt(state_,id,{{"state","failed"},{"stage","bootstrap"},{"native_status",game_->status()}});
        }
    }

    void advance() {
        try {
            const auto native=game_->status();update(state_,{{"native_status",native},
                {"hook_resident",native.value("hook_installed",false)}});
            if(!native.value("ready",false)) {
                if(!native.at("reflection").value("error",json(nullptr)).is_null())
                    throw std::runtime_error("mod_reflection_checkpoint_failed");
                update(state_,{{"state","waiting_runtime"},{"active",false}});return;
            }
            update(state_,{{"initialized",true}});
            if(!binding_) {
                binding_=select();
                if(!binding_) {
                    update(state_,{{"state","waiting_target"},{"active",false},{"target",nullptr}});
                    receipt(state_,start_id_,{{"state","pending"},{"stage","target_selection"}});return;
                }
                update(state_,{{"target",binding_->target},{"baseline_opacity",binding_->baseline}});
                update(state_,{{"restoration_required",true}});
                setter(*binding_,SN2ModSpecification()->opacity);
                reapply_requested_=false;
            } else if(reapply_requested_) {
                reapply_requested_=false;
                // A fresh explicit START admits one new application.
                (void)getter(*binding_);
                setter(*binding_,SN2ModSpecification()->opacity);
            }
            auto observed=getter(*binding_);
            if(std::fabs(observed-SN2ModSpecification()->opacity)>0.000001f) {
                const auto change=observed;
                const auto sequence=++maintenance_applications_;
                update(state_,{{"state","maintaining"},{"active",false},{"observed_opacity",change},
                    {"maintenance_applications",sequence},
                    {"last_maintenance",{{"sequence",sequence},{"state","applying"},
                        {"reason","fresh_getter_differs_from_compiled_opacity"},
                        {"observed_before",change},{"requested_opacity",SN2ModSpecification()->opacity},
                        {"target",binding_->target},{"observed_monotonic_ms",GetTickCount64()}}}});
                // This is a new policy action caused by a definite getter
                // result, not a replay of a setter with unknown outcome.
                setter(*binding_,SN2ModSpecification()->opacity);
                {
                    std::lock_guard lock(state_.mutex);
                    state_.snapshot["last_maintenance"].update({{"state","verifying"},
                        {"setter_returned",true},{"setter_effect",{{"executed",true},{"may_have_executed",false}}}});
                }
                observed=getter(*binding_);
                {
                    std::lock_guard lock(state_.mutex);
                    state_.snapshot["last_maintenance"].update({{"state","setter_completed"},
                        {"observed_after",observed},{"setter_returned",true},
                        {"desired_value_confirmed",std::fabs(observed-SN2ModSpecification()->opacity)<=0.000001f}});
                }
                if(std::fabs(observed-SN2ModSpecification()->opacity)>0.000001f) {
                    // The game may animate the widget between separate Tick
                    // calls. The next action requires another fresh getter.
                    update(state_,{{"state","maintaining"},{"active",false},{"observed_opacity",observed}});
                    return;
                }
            }
            update(state_,{{"state","active"},{"active",true},{"observed_opacity",observed},
                {"last_error",nullptr}});
            receipt(state_,start_id_,{{"state","completed"},{"stage","active"},
                {"effect",{{"executed",true},{"may_have_executed",false},{"getter_confirmed",true}}},
                {"target",binding_->target},{"baseline_opacity",binding_->baseline},{"observed_opacity",observed}});
        } catch(const sn2_mcp::HandlerError& error) {
            const auto code=error.code();
            if(code=="stale_handle" || code=="world_changed" || code=="object_destroying") {
                // The previous object cannot be written through an expired
                // handle. New widgets receive a fresh baseline and contracts.
                update(state_,{{"state","waiting_target"},{"active",false},{"last_error",error_json(error)},
                    {"previous_target",binding_?binding_->target:json(nullptr)},
                    {"previous_target_restoration","target_identity_expired"}});
                {
                    std::lock_guard lock(state_.mutex);
                    auto& maintenance=state_.snapshot["last_maintenance"];
                    if(maintenance.is_object()) {
                        const auto stage=maintenance.value("state",std::string{});
                        if(stage=="verifying")
                            maintenance.update({{"state","verification_failed"},{"reason","target_identity_expired"},
                                {"getter_error",error_json(error)},{"desired_value_confirmed",false}});
                        else if(stage=="applying")
                            maintenance.update({{"state","failed"},{"reason","target_identity_expired"},
                                {"error",error_json(error)}});
                    }
                }
                if(binding_ && binding_->effect_started)prior_restore_unconfirmed_=true;
                binding_.reset();return;
            }
            fail(error,error.details().value("may_have_executed",false));
        } catch(const std::exception& error) {fail(error,setter_uncertain_);}
    }

    void fail(const std::exception& error,bool uncertain) {
        effect_unknown_=uncertain;desired_active_=false;
        auto details=error_json(error);
        if(uncertain)details["effect"].update({{"may_have_executed",true},{"do_not_retry",true}});
        update(state_,{{"state",uncertain?"effect_unknown":"faulted"},{"active",false},
            {"last_error",details}});
        {
            std::lock_guard lock(state_.mutex);
            auto& maintenance=state_.snapshot["last_maintenance"];
            if(maintenance.is_object()) {
                const auto stage=maintenance.value("state",std::string{});
                if(stage=="applying")
                    maintenance.update({{"state",uncertain?"outcome_unknown":"failed"},{"error",details}});
                else if(stage=="verifying")
                    maintenance.update({{"state","verification_failed"},{"getter_error",details},
                        {"setter_returned",true},{"desired_value_confirmed",false}});
            }
        }
        receipt(state_,start_id_,{{"state",uncertain?"outcome_unknown":"failed"},
            {"stage","mod_effect"},{"error",details}});
    }

    void disable(std::uint64_t id) {
        desired_active_=false;
        if(start_id_) {
            std::lock_guard lock(state_.mutex);
            if(state_.receipts.at(start_id_).value("state",std::string{})=="pending")
                state_.receipts.at(start_id_).update({{"state","cancelled"},{"cancelled_by",id}});
        }
        receipt(state_,id,{{"state","pending"},{"stage","restore"}});
        update(state_,{{"desired_state","disabled"},{"state","disabling"},{"active",false}});
        try {
            bool restored=true;json observed=nullptr;
            const bool restore_required=binding_ && binding_->baseline_recorded && binding_->effect_started;
            if(restore_required) {
                const auto current=getter(*binding_);
                if(std::fabs(current-binding_->baseline)>0.000001f)setter(*binding_,binding_->baseline);
                observed=getter(*binding_);
                restored=std::fabs(observed.get<float>()-binding_->baseline)<=0.000001f;
                if(!restored)throw std::runtime_error("mod_restore_getter_mismatch");
            }
            if(prior_restore_unconfirmed_) {
                update(state_,{{"observed_opacity",observed},{"current_target_restoration_confirmed",restored}});
                throw std::runtime_error("mod_previous_target_restoration_unconfirmed");
            }
            effect_unknown_=false;
            update(state_,{{"state","disabled"},{"active",false},{"restoration_confirmed",restored},
                {"observed_opacity",observed},{"last_error",nullptr}});
            receipt(state_,id,{{"state","completed"},{"stage","disabled"},
                {"restoration_required",restore_required},{"restoration_confirmed",restored},{"observed_opacity",observed},
                {"module_resident",true},{"hook_resident",game_ && game_->status().value("hook_installed",false)}});
            binding_.reset();
        } catch(const std::exception& error) {
            auto details=error_json(error);
            effect_unknown_=setter_uncertain_ || details.at("effect").value("may_have_executed",false);
            if(effect_unknown_)details["effect"].update({{"may_have_executed",true},{"do_not_retry",true}});
            update(state_,{{"state",effect_unknown_?"restore_outcome_unknown":"disabled_unrestored"},
                {"restoration_confirmed",false},{"last_error",details}});
            receipt(state_,id,{{"state",effect_unknown_?"outcome_unknown":"failed"},
                {"stage","restore"},{"restoration_confirmed",false},{"error",details}});
        }
    }

    State& state_;
    std::string session_;
    sn2_mcp::GameAccess* game_{};
    std::optional<Binding> binding_;
    std::uint64_t start_id_{};
    std::uint64_t maintenance_applications_{};
    bool desired_active_{};
    bool effect_unknown_{};
    bool reapply_requested_{};
    bool setter_uncertain_{};
    bool prior_restore_unconfirmed_{};
};

DWORD WINAPI worker_entry(void* pointer) {
    auto& value=*static_cast<State*>(pointer);
    try {Worker(value).run();}
    catch(const std::exception& error) {
        update(value,{{"state","worker_failed"},{"active",false},{"last_error",error_json(error)}});
        std::lock_guard lock(value.mutex);
        value.worker_failed=true;
        for(auto& [id,command]:value.receipts) {
            (void)id;
            if(command.value("state",std::string{})=="accepted" || command.value("state",std::string{})=="pending")
                command.update({{"state","outcome_unknown"},{"stage","worker_failed"},{"error",error_json(error)}});
        }
    }
    return 0;
}

json dispatch(const sn2_mod::Command& command) {
    auto& value=state();const auto operation=static_cast<Operation>(command.operation);
    std::lock_guard lock(value.mutex);
    if(operation!=Operation::status) {
        if(!command.request_id)throw std::invalid_argument("request_id_required");
        if(const auto previous=value.receipts.find(command.request_id);previous!=value.receipts.end()) {
            if(previous->second.at("operation")!=command.operation)
                throw std::invalid_argument("request_id_operation_conflict");
        } else {
            const auto* name=operation==Operation::start?"start":"disable";
            value.receipts.emplace(command.request_id,json{{"request_id",command.request_id},
                {"operation",command.operation},{"command",name},{"state","accepted"},
                {"process_id",GetCurrentProcessId()},
                {"process_creation_filetime",value.snapshot.at("process_creation_filetime")}});
            if(value.worker_failed) {
                value.receipts.at(command.request_id).update({{"state","failed"},{"stage","worker_failed"}});
                return {{"command",value.receipts.at(command.request_id)},{"request_id",command.request_id},
                    {"command_found",true},{"status",value.snapshot}};
            }
            if(!value.worker_started) {
                const auto thread=CreateThread(nullptr,0,worker_entry,&value,0,nullptr);
                if(!thread) {
                    value.receipts.at(command.request_id).update({{"state","failed"},{"stage","worker_creation"}});
                    throw std::runtime_error("mod_worker_create_failed");
                }
                CloseHandle(thread);value.worker_started=true;
            }
            value.pending.push_back({command.request_id,operation});SetEvent(value.event);
        }
    }
    const auto found=value.receipts.find(command.request_id);
    return {{"command",found==value.receipts.end()?json(nullptr):found->second},
        {"request_id",command.request_id},{"command_found",found!=value.receipts.end()},
        {"status",value.snapshot}};
}

bool readable_copy(void* destination,const void* source,std::size_t bytes) {
    SIZE_T copied{};
    return ReadProcessMemory(GetCurrentProcess(),source,destination,bytes,&copied) && copied==bytes;
}
bool writable_copy(void* destination,const void* source,std::size_t bytes) {
    SIZE_T copied{};
    return WriteProcessMemory(GetCurrentProcess(),destination,source,bytes,&copied) && copied==bytes;
}
} // namespace

extern "C" void SN2ModDiagnostic(const char* text) noexcept {
    try {
        auto& value=state();
        std::lock_guard lock(value.mutex);
        value.snapshot["bootstrap_diagnostics"].push_back({{"message",text?text:""},
            {"thread_id",GetCurrentThreadId()},{"monotonic_ms",GetTickCount64()}});
    } catch(...) {
        // Diagnostic allocation failure does not change hook/effect state.
    }
}

extern "C" __declspec(dllexport) DWORD WINAPI SN2ModCommand(void* pointer) {
    sn2_mod::Command command;
    if(!pointer || !readable_copy(&command,pointer,sizeof(command)))
        return static_cast<DWORD>(sn2_mod::Result::invalid_request);
    command.response_size=0;command.response_required=0;
    auto result=sn2_mod::Result::ok;json response;bool response_valid{};
    try {
        if(!command.response_address || command.response_capacity>std::numeric_limits<std::size_t>::max()
           || command.response_address>std::numeric_limits<std::uintptr_t>::max()-command.response_capacity)
            throw std::invalid_argument("invalid_response_allocation");
        const auto header=reinterpret_cast<std::uintptr_t>(pointer);
        if(command.response_address<header+sizeof(command)
           && command.response_address+command.response_capacity>header)
            throw std::invalid_argument("response_overlaps_command");
        if(command.response_capacity) {
            MEMORY_BASIC_INFORMATION allocation{};
            const auto address=static_cast<std::uintptr_t>(command.response_address);
            if(!VirtualQuery(reinterpret_cast<const void*>(address),&allocation,sizeof(allocation))
               || allocation.State!=MEM_COMMIT || (allocation.Protect&(PAGE_GUARD|PAGE_NOACCESS))
               || !(allocation.Protect&(PAGE_READWRITE|PAGE_WRITECOPY|PAGE_EXECUTE_READWRITE|PAGE_EXECUTE_WRITECOPY))
               || address-reinterpret_cast<std::uintptr_t>(allocation.BaseAddress)>allocation.RegionSize
               || command.response_capacity>allocation.RegionSize-(address-reinterpret_cast<std::uintptr_t>(allocation.BaseAddress)))
                throw std::invalid_argument("response_allocation_not_writable");
        }
        response_valid=true;
        if(command.size!=sizeof(command) || command.abi!=sn2_mod::CommandAbi || command.reserved
           || command.expected_pid!=GetCurrentProcessId()
           || command.expected_creation_filetime!=sn2::loader::creation_filetime(GetCurrentProcess())
           || (command.operation!=static_cast<std::uint32_t>(Operation::start)
               && command.operation!=static_cast<std::uint32_t>(Operation::status)
               && command.operation!=static_cast<std::uint32_t>(Operation::disable)))
            throw std::invalid_argument("invalid_mod_command_or_process_identity");
        response=dispatch(command);
    } catch(const std::invalid_argument& error) {
        result=sn2_mod::Result::invalid_request;
        response={{"request_id",command.request_id},{"error",error.what()},{"executed",false}};
    } catch(const std::exception& error) {
        result=sn2_mod::Result::command_failed;
        response={{"request_id",command.request_id},{"error",error.what()},{"status_available",false}};
    } catch(...) {
        result=sn2_mod::Result::command_failed;
        response={{"request_id",command.request_id},{"error","mod_command_exception"}};
    }
    try {
        const auto bytes=response.dump();command.response_required=bytes.size();
        if(response_valid) {
            if(command.response_capacity<bytes.size()) {
                if(result==sn2_mod::Result::ok)result=sn2_mod::Result::response_too_small;
            }
            else if(writable_copy(reinterpret_cast<void*>(command.response_address),bytes.data(),bytes.size()))
                command.response_size=bytes.size();
            else result=sn2_mod::Result::command_failed;
        }
    } catch(...) {result=sn2_mod::Result::command_failed;}
    command.result_code=static_cast<std::uint32_t>(result);
    if(!writable_copy(pointer,&command,sizeof(command)))
        return static_cast<DWORD>(sn2_mod::Result::command_failed);
    return command.result_code;
}

BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID) {
    if(reason==DLL_PROCESS_ATTACH) {module=instance;DisableThreadLibraryCalls(instance);}
    return TRUE;
}
