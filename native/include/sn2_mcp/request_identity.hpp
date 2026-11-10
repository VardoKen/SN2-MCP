#pragma once

#include "sn2_mcp/process_identity.hpp"
#include "sn2_mcp/transport.hpp"
#include <nlohmann/json.hpp>
#include <string>

namespace sn2_mcp {
// Called before enable_game and queue admission. PID alone is insufficient
// because Windows can reuse it; compare the selected process creation time.
inline void validate_request_identity(const nlohmann::json& params,
                                     std::uint32_t process_id,
                                     std::uint64_t creation_time,
                                     const nlohmann::json& session_id) {
    const auto refused = [](const char* code, const char* message) {
        throw HandlerError(code, message,
            {{"executed",false},{"may_have_executed",false},{"do_not_retry",false}});
    };
    if(params.contains("expected_process_id") && params.at("expected_process_id")!=process_id)
        refused("target_process_mismatch","The request selected a different native process");
    if(params.contains("expected_creation_filetime")) {
        const auto& selected=params.at("expected_creation_filetime");
        if(!selected.is_string())
            refused("invalid_target_identity","expected_creation_filetime must be a decimal string");
        const auto text=selected.get<std::string>();
        std::uint64_t expected{};
        try {expected=sn2::loader::parse_creation_filetime(std::wstring(text.begin(),text.end()));}
        catch(const std::exception&) {
            refused("invalid_target_identity","expected_creation_filetime must be a positive uint64 decimal string");
        }
        if(expected!=creation_time)
            refused("target_process_mismatch","The selected native process creation time changed");
    }
    if(params.contains("expected_session_id") && params.at("expected_session_id")!=session_id)
        refused("native_session_mismatch","The request selected an expired native session");
}
} // namespace sn2_mcp
