#pragma once

#include <nlohmann/json.hpp>
#include <cstdint>
#include <map>
#include <mutex>
#include <string>

namespace sn2_mcp {
// Deduplicated metadata shapes only. Successful execution does not establish
// semantic correctness or universal support for every instance of a type.
class RuntimeEvidence {
    mutable std::mutex mutex_;
    std::map<std::string,nlohmann::json> shapes_;
public:
    void observe(const std::string& operation,const nlohmann::json& shape,
                 std::uint64_t tick,bool owned,bool cleanup_confirmed,
                 bool partial=false,bool value_observed=true) {
        if(partial || !value_observed || (owned&&!cleanup_confirmed))return;
        const auto key=nlohmann::json{{"operation",operation},{"shape",shape}}.dump();
        std::lock_guard lock(mutex_);
        auto [entry,inserted]=shapes_.try_emplace(key,nlohmann::json{{"operation",operation},{"shape",shape},
            {"state","observed_success"},{"first_tick",tick},{"last_tick",tick},{"successful_count",0},
            {"owned",owned},{"cleanup_confirmed",owned?nlohmann::json(cleanup_confirmed):nlohmann::json(nullptr)}});
        entry->second["last_tick"]=tick;
        entry->second["successful_count"]=entry->second.at("successful_count").get<std::uint64_t>()+1;
        (void)inserted;
    }
    nlohmann::json describe() const {
        std::lock_guard lock(mutex_);auto items=nlohmann::json::array();
        for(const auto& [key,value]:shapes_){(void)key;items.push_back(value);}
        return {{"state",items.empty()?"unverified":"observed_success"},
            {"source","successful_target_operations"},{"scope","current_process_lifetime"},
            {"semantic_acceptance","requires_external_receipt_assertions"},
            {"coverage","listed_shapes_and_directions_only"},{"shapes",std::move(items)}};
    }
};
} // namespace sn2_mcp
