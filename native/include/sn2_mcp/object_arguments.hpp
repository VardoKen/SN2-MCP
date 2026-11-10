#pragma once

#include <sn2_mcp/transport.hpp>
#include <cstdint>
#include <functional>
#include <set>

namespace sn2_mcp {

struct ObjectIdentity {
    std::uintptr_t object{};
    std::uintptr_t object_class{};
    std::int32_t index{};
    std::uint64_t name{};
};
inline bool same_object_identity(const ObjectIdentity& left,const ObjectIdentity& right) {
    return left.object==right.object && left.object_class==right.object_class
        && left.index==right.index && left.name==right.name;
}

// An empty string denotes explicit JSON null. Raw pointers are never
// external arguments; use only an opaque handle from the current transport session.
inline std::string object_argument_handle(const nlohmann::json& value) {
    if (value.is_null()) return {};
    if (!value.is_object() || value.size()!=1 || !value.contains("handle")
        || !value.at("handle").is_string() || value.at("handle").get_ref<const std::string&>().empty())
        throw HandlerError("invalid_object_argument", "An object argument requires null or only {handle}");
    return value.at("handle").get<std::string>();
}

inline bool compatible_object_class(std::uintptr_t actual, std::uintptr_t expected,
                                    const std::function<std::uintptr_t(std::uintptr_t)>& superclass) {
    if (!actual || !expected) throw HandlerError("invalid_object_class", "The object class is missing");
    std::set<std::uintptr_t> visited;
    while (actual) {
        if (!visited.insert(actual).second)
            throw HandlerError("invalid_object_class", "The object class inheritance chain contains a cycle");
        if (actual==expected) return true;
        actual=superclass(actual);
    }
    return false;
}

} // namespace sn2_mcp
