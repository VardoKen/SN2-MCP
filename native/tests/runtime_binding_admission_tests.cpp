#include "sn2_mcp/runtime_bindings.hpp"
#include "sn2_mcp/request_identity.hpp"
#include <iostream>
#include <stdexcept>

namespace {
void require(bool value,const char* message) {
    if(!value)throw std::runtime_error(message);
}
void reject(const nlohmann::json& params,const char* expected) {
    bool effect{};
    try {
        sn2_mcp::validate_request_identity(params,12,1234,"session-a");
        effect=true; // Equivalent ordering to enable_game or queue admission.
    } catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==expected,"wrong identity rejection");
        require(error.details().at("executed")==false,"identity rejection claims execution");
        require(error.details().at("may_have_executed")==false,"identity rejection uncertain");
    }
    require(!effect,"identity mismatch reached effect");
}
}
int main() {
    try {
        using namespace sn2_mcp;
        RuntimeBindings bindings;
        require(!bindings.executor_discovered() && !bindings.reflection_discovered(),"empty bindings ready");
        bindings.tick={0x100,{},false};
        require(bindings.executor_discovered(),"Tick discovery depends on optional allocators");
        require(bindings.describe().at("tick").at("state")=="discovered","static candidate claims runtime proof");
        bindings.names={0x200,{},false};bindings.object_array={0x300,{},false};
        require(bindings.reflection_discovered(),"reflection depends on optional invocation binding");
        require(!bindings.process_event.available() && !bindings.free.available(),"unknown owned bindings available");
        bindings.tick.runtime_validated=true;
        require(bindings.describe().at("tick").at("state")=="verified","observed binding not verified");
        bindings.names.error="non_unique_pattern";
        require(!bindings.reflection_discovered(),"ambiguous name binding allowed");
        require(bindings.describe().at("names").at("state")=="unavailable","binding error hidden");
        validate_request_identity(nlohmann::json::object(),12,1234,"session-a");
        validate_request_identity({{"expected_process_id",12},{"expected_creation_filetime","1234"},
            {"expected_session_id","session-a"}},12,1234,"session-a");
        reject({{"expected_process_id",13}},"target_process_mismatch");
        reject({{"expected_process_id",12},{"expected_creation_filetime","1235"}},"target_process_mismatch");
        reject({{"expected_creation_filetime",1234}},"invalid_target_identity");
        reject({{"expected_creation_filetime","18446744073709551616"}},"invalid_target_identity");
        reject({{"expected_creation_filetime","0"}},"invalid_target_identity");
        reject({{"expected_session_id","session-b"}},"native_session_mismatch");
        std::cout<<"Runtime binding and request admission checks passed\n";
        return 0;
    }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
