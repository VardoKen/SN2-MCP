#pragma once

#include <cstdint>
#include <deque>
#include <functional>
#include <map>
#include <string>
#include <vector>
#include <nlohmann/json.hpp>

namespace sn2_mcp {

// Pure model of copies and caller-bounded observations. Called only by the game
// pump; GameAccess owns the reader, which is never invoked after a terminal state.
class Observations {
public:
    using Json = nlohmann::json;
    using Reader = std::function<Json(const Json& targets)>;
    static void validate_targets(const Json& targets);
    Json start(const std::string& owner, const Json& params, const Json& baseline,
               std::uint64_t now_ms, std::uint64_t tick);
    Json start(const std::string& connection_session, const std::string& client_owner,
               const Json& params, const Json& baseline,
               std::uint64_t now_ms, std::uint64_t tick);
    Json read(const std::string& owner, const Json& params) const;
    Json list(const std::string& connection_session, const std::string& client_owner) const;
    Json resume(const std::string& connection_session, const std::string& new_client_owner,
                const Json& params);
    Json stop(const std::string& owner, const Json& params,
              std::uint64_t now_ms, std::uint64_t tick);
    Json forget(const std::string& owner, const Json& params);
    void disconnect(const std::string& connection_session);
    void invalidate_world(const std::vector<std::string>& affected_handles,
                          std::uint64_t previous_generation, std::uint64_t generation,
                          std::uint64_t now_ms, std::uint64_t tick);
    void invalidate_script(const std::vector<std::string>& affected_handles,
                           std::uint64_t previous_generation, std::uint64_t generation,
                           std::uint64_t now_ms, std::uint64_t tick);
    void pump(std::uint64_t now_ms, std::uint64_t tick, const Reader& reader);
    std::size_t size() const noexcept { return watches_.size(); }

private:
    struct Watch {
        std::string id;
        std::string connection_session;
        std::string client_owner;
        Json targets;
        Json latest;
        std::uint64_t started{};
        std::uint64_t deadline{};
        std::uint64_t next_sample{};
        std::uint64_t interval{};
        std::uint64_t next_seq{1};
        std::uint64_t dropped{};
        std::size_t capacity{};
        std::size_t bytes{};
        std::deque<Json> events;
        std::string state{"running"};
        std::string reason;
    };
    std::map<std::string, Watch> watches_;
    std::vector<std::string> order_;
    std::size_t cursor_{};
    std::uint64_t next_id_{};

    Watch& find(const std::string& owner, const Json& params);
    const Watch& find(const std::string& owner, const Json& params) const;
    static Json metadata(const Watch& watch);
    static void append(Watch& watch, Json event);
    static void finish(Watch& watch, const std::string& state,
                       const std::string& reason, std::uint64_t now_ms,
                       std::uint64_t tick, const Json* snapshot = nullptr,
                       const Json* transition = nullptr);
    void invalidate_handles(const std::vector<std::string>& affected_handles,
                            const std::string& reason, const Json& transition,
                            std::uint64_t now_ms, std::uint64_t tick);
};

} // namespace sn2_mcp
