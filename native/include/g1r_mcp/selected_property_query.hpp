#pragma once

#include <g1r_mcp/transport.hpp>

#include <functional>
#include <string>
#include <unordered_set>
#include <utility>

namespace g1r_mcp::selected_property_query {

inline void validate(const nlohmann::json& names) {
    if(!names.is_array() || names.empty())
        throw HandlerError("invalid_property_names", "property_names requires a nonempty array of property names");
    std::unordered_set<std::string> unique;
    for(const auto& value:names) {
        if(!value.is_string())
            throw HandlerError("invalid_property_names", "Each property name must be a string");
        const auto& name=value.get_ref<const std::string&>();
        if(name.empty() || name.find('\0')!=std::string::npos)
            throw HandlerError("invalid_property_names", "A property name must be nonempty and contain no NUL");
        if(!unique.insert(name).second)
            throw HandlerError("invalid_property_names", "Property names must be unique");
    }
}

// Validate the request before invoking reader. Names are neither normalized nor reordered.
template<class Reader>
auto validated(const nlohmann::json& names,Reader&& reader) {
    validate(names);
    return std::invoke(std::forward<Reader>(reader),names);
}

} // namespace g1r_mcp::selected_property_query
