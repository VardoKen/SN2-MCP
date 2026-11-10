#include <sn2_mcp/observations.hpp>
#include <sn2_mcp/transport.hpp>
#include <algorithm>
#include <limits>
#include <set>
#include <utility>

namespace sn2_mcp {
namespace {
using Json = nlohmann::json;
std::uint64_t integer(const Json& params, const char* key, std::uint64_t minimum,
                      std::uint64_t maximum) {
    if (!params.contains(key) || !params.at(key).is_number_integer())
        throw HandlerError("invalid_watch_params", std::string("An integer field is required: ") + key);
    const auto& value = params.at(key);
    if ((!value.is_number_unsigned() && value.get<std::int64_t>() < 0)
        || value.get<std::uint64_t>() < minimum || value.get<std::uint64_t>() > maximum)
        throw HandlerError("invalid_watch_params", std::string("Field is outside the representable range: ") + key);
    return value.get<std::uint64_t>();
}
std::string watch_id(const Json& params) {
    if (!params.contains("watch_id") || !params.at("watch_id").is_string()
        || params.at("watch_id").get_ref<const std::string&>().empty())
        throw HandlerError("invalid_watch_params", "A nonempty watch_id string is required");
    return params.at("watch_id").get<std::string>();
}
bool complete_snapshot(const Json& snapshot) {
    if (!snapshot.is_object() || snapshot.value("partial", true)
        || snapshot.value("truncated", true) || !snapshot.contains("objects")) return false;
    return snapshot.at("objects").is_array() && !snapshot.at("objects").empty();
}
}

void Observations::validate_targets(const Json& targets) {
    if (!targets.is_array() || targets.empty())
        throw HandlerError("invalid_snapshot_params", "A snapshot requires a nonempty array of targets");
    std::set<std::string> handles;
    for (const auto& target : targets) {
        if (!target.is_object() || target.size()!=2 || !target.contains("handle") || !target.at("handle").is_string()
            || target.at("handle").get_ref<const std::string&>().empty()
            || !target.contains("properties") || !target.at("properties").is_array()
            || target.at("properties").empty())
            throw HandlerError("invalid_snapshot_params", "A target requires a nonempty handle and array of property names");
        if (!handles.insert(target.at("handle").get<std::string>()).second)
            throw HandlerError("invalid_snapshot_params", "Snapshot target handles must be unique");
        std::set<std::string> properties;
        for (const auto& property : target.at("properties")) {
            if (!property.is_string() || property.get_ref<const std::string&>().empty()
                || !properties.insert(property.get<std::string>()).second)
                throw HandlerError("invalid_snapshot_params", "Property names must be nonempty and unique");
        }
    }
}

Observations::Json Observations::metadata(const Watch& watch) {
    return {{"watch_id", watch.id}, {"session_id", watch.connection_session},
            {"client_owner", watch.client_owner}, {"started_ms", watch.started}, {"state", watch.state},
            {"reason", watch.reason}, {"cleanup_complete", watch.state != "running"},
            {"next_seq", watch.next_seq}, {"last_seq", watch.next_seq - 1},
            {"oldest_seq", watch.events.empty() ? watch.next_seq : watch.events.front().at("seq").get<std::uint64_t>()},
            {"dropped_events", watch.dropped}, {"capacity", watch.capacity},
            {"buffer_bytes", watch.bytes}, {"buffer_limit_bytes", nullptr}};
}

void Observations::append(Watch& watch, Json event) {
    event["seq"] = watch.next_seq++;
    const auto bytes = event.dump().size();
    while (!watch.events.empty() && watch.events.size() >= watch.capacity) {
        watch.bytes -= watch.events.front().dump().size();
        watch.events.pop_front();
        ++watch.dropped;
    }
    watch.bytes += bytes;
    watch.events.push_back(std::move(event));
}

void Observations::finish(Watch& watch, const std::string& state,
                          const std::string& reason, std::uint64_t now_ms,
                          std::uint64_t tick, const Json* snapshot,
                          const Json* transition) {
    if (watch.state != "running") return;
    watch.state = state;
    watch.reason = reason;
    Json event{{"kind", "terminal"}, {"reason", reason}, {"state", state},
               {"elapsed_ms", now_ms - watch.started}, {"tick_count", tick}};
    if (snapshot) event["snapshot"] = *snapshot;
    if (transition) event.update(*transition);
    append(watch, std::move(event));
    watch.latest = nullptr;
}

Observations::Json Observations::start(const std::string& owner, const Json& params,
                                      const Json& baseline, std::uint64_t now_ms,
                                      std::uint64_t tick) {
    return start(owner, owner, params, baseline, now_ms, tick);
}

Observations::Json Observations::start(const std::string& connection_session,
                                      const std::string& client_owner, const Json& params,
                                      const Json& baseline, std::uint64_t now_ms,
                                      std::uint64_t tick) {
    if (!params.is_object() || !params.contains("handle") || !params.contains("properties"))
        throw HandlerError("invalid_watch_params", "A watch requires handle and properties");
    Json targets = Json::array({{{"handle", params.at("handle")}, {"properties", params.at("properties")}}});
    validate_targets(targets);
    const auto duration = integer(params, "duration_ms", 1, std::numeric_limits<std::uint64_t>::max() - now_ms);
    const auto interval = integer(params, "interval_ms", 1, std::numeric_limits<std::uint64_t>::max());
    const auto capacity = integer(params, "capacity", 1, std::numeric_limits<std::size_t>::max());
    if (!complete_snapshot(baseline))
        throw HandlerError("unsupported_watch_target", "The initial snapshot must contain fully supported values");
    Watch watch;
    watch.id = "watch-" + std::to_string(++next_id_);
    watch.connection_session = connection_session;
    watch.client_owner = client_owner;
    watch.targets = std::move(targets);
    watch.latest = baseline.at("objects");
    watch.started = now_ms;
    watch.deadline = now_ms + duration;
    watch.interval = interval;
    watch.next_sample = now_ms + std::min(duration, interval);
    watch.capacity = static_cast<std::size_t>(capacity);
    append(watch, {{"kind", "baseline"}, {"elapsed_ms", 0}, {"tick_count", tick}, {"snapshot", baseline}});
    auto result = metadata(watch);
    result["start_seq"] = 1;
    result["duration_ms"] = duration;
    result["interval_ms"] = interval;
    result["sampling"] = "all_due_watches_per_tick_no_catchup";
    order_.push_back(watch.id);
    watches_.emplace(watch.id, std::move(watch));
    return result;
}

const Observations::Watch& Observations::find(const std::string&, const Json& params) const {
    auto found = watches_.find(watch_id(params));
    if (found == watches_.end()) throw HandlerError("unknown_watch", "The watch does not exist in the current environment");
    return found->second;
}
Observations::Watch& Observations::find(const std::string& owner, const Json& params) {
    return const_cast<Watch&>(std::as_const(*this).find(owner, params));
}

Observations::Json Observations::list(const std::string& connection_session,
                                     const std::string& client_owner) const {
    Json items=Json::array();
    for(const auto& id:order_) {
        const auto& watch=watches_.at(id);
        if(watch.connection_session==connection_session)items.push_back(metadata(watch));
    }
    return {{"session_id",connection_session},{"current_client_owner",client_owner},{"items",std::move(items)}};
}

Observations::Json Observations::resume(const std::string& connection_session,
                                       const std::string& new_client_owner, const Json& params) {
    const auto id=watch_id(params);
    if(params.contains("expected_session_id") && !params.at("expected_session_id").is_string())
        throw HandlerError("invalid_watch_params","Expected session_id must be a string");
    const auto found=watches_.find(id);
    if(found==watches_.end())throw HandlerError("unknown_watch","Watch does not exist in the current connection");
    auto& watch=found->second;
    if(watch.connection_session!=connection_session
       || (params.contains("expected_session_id")
           && watch.connection_session!=params.at("expected_session_id").get_ref<const std::string&>()))
        throw HandlerError("watch_resume_mismatch","Watch session does not match the expected identity");
    watch.client_owner=new_client_owner;
    return metadata(watch);
}

Observations::Json Observations::read(const std::string& owner, const Json& params) const {
    const auto& watch = find(owner, params);
    const auto after = integer(params, "after_seq", 0, watch.next_seq - 1);
    const auto limit = integer(params, "limit", 1, std::numeric_limits<std::size_t>::max());
    auto result = metadata(watch);
    result["events"] = Json::array();
    result["gap"] = !watch.events.empty() && after + 1 < watch.events.front().at("seq").get<std::uint64_t>();
    result["cursor"] = after;
    result["truncated"] = false;
    for (const auto& event : watch.events) {
        if (event.at("seq").get<std::uint64_t>() <= after) continue;
        if (result["events"].size() >= limit) {
            result["truncated"] = true;
            break;
        }
        result["events"].push_back(event);
        result["cursor"] = event.at("seq");
    }
    return result;
}

Observations::Json Observations::stop(const std::string& owner, const Json& params,
                                     std::uint64_t now_ms, std::uint64_t tick) {
    auto& watch = find(owner, params);
    finish(watch, "stopped", "explicit_stop", now_ms, tick);
    return metadata(watch);
}
Observations::Json Observations::forget(const std::string& owner, const Json& params) {
    const auto& watch = find(owner, params);
    if (watch.state == "running") throw HandlerError("watch_running", "Stop the watch first");
    const auto id = watch.id;
    watches_.erase(id);
    order_.erase(std::remove(order_.begin(), order_.end(), id), order_.end());
    cursor_ = 0;
    return {{"watch_id", id}, {"forgotten", true}, {"cleanup_complete", true}};
}
void Observations::disconnect(const std::string& connection_session) {
    for (auto item = watches_.begin(); item != watches_.end();) {
        if (item->second.connection_session == connection_session) item = watches_.erase(item); else ++item;
    }
    order_.erase(std::remove_if(order_.begin(), order_.end(), [&](const auto& id) { return !watches_.contains(id); }), order_.end());
    cursor_ = 0;
}

void Observations::invalidate_world(const std::vector<std::string>& affected_handles,
                                    std::uint64_t previous_generation, std::uint64_t generation,
                                    std::uint64_t now_ms, std::uint64_t tick) {
    const Json transition{{"previous_world_generation", previous_generation}, {"world_generation", generation}};
    invalidate_handles(affected_handles, "world_changed", transition, now_ms, tick);
}

void Observations::invalidate_script(const std::vector<std::string>& affected_handles,
                                     std::uint64_t previous_generation, std::uint64_t generation,
                                     std::uint64_t now_ms, std::uint64_t tick) {
    const Json transition{{"previous_script_generation", previous_generation}, {"script_generation", generation}};
    invalidate_handles(affected_handles, "script_reloaded", transition, now_ms, tick);
}

void Observations::invalidate_handles(const std::vector<std::string>& affected_handles,
                                      const std::string& reason, const Json& transition,
                                      std::uint64_t now_ms, std::uint64_t tick) {
    const std::set<std::string> affected(affected_handles.begin(), affected_handles.end());
    for (auto& item : watches_) {
        auto& watch = item.second;
        if (watch.state != "running") continue;
        const bool invalidated = std::any_of(watch.targets.begin(), watch.targets.end(), [&](const Json& target) {
            return affected.contains(target.at("handle").get_ref<const std::string&>());
        });
        if (invalidated) finish(watch, "invalidated", reason, now_ms, tick, nullptr, &transition);
    }
}

void Observations::pump(std::uint64_t now_ms, std::uint64_t tick, const Reader& reader) {
    if (order_.empty()) return;
    for (std::size_t checked = 0; checked < order_.size(); ++checked) {
        cursor_ %= order_.size();
        auto& watch = watches_.at(order_.at(cursor_++));
        if (watch.state != "running") continue;
        if (now_ms >= watch.deadline) {
            finish(watch, "completed", "duration_elapsed", now_ms, tick);
            continue;
        }
        if (now_ms < watch.next_sample) continue;
        watch.next_sample = watch.interval < watch.deadline - now_ms
                          ? now_ms + watch.interval : watch.deadline;
        try {
            auto snapshot = reader(watch.targets);
            if (!complete_snapshot(snapshot)) {
                finish(watch, "failed", "object_or_property_invalidated", now_ms, tick, &snapshot);
            } else if (snapshot.at("objects") != watch.latest) {
                watch.latest = snapshot.at("objects");
                append(watch, {{"kind", "changed"}, {"elapsed_ms", now_ms - watch.started},
                               {"tick_count", tick}, {"snapshot", std::move(snapshot)}});
            }
        } catch (const HandlerError& error) {
            finish(watch, "failed", error.code(), now_ms, tick);
        } catch (...) {
            finish(watch, "failed", "snapshot_read_failed", now_ms, tick);
        }
    }
}

} // namespace sn2_mcp
