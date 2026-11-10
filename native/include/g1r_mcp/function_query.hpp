#pragma once

#include <g1r_mcp/object_query.hpp>
#include <string_view>

namespace g1r_mcp::function_query {

inline bool valid(std::string_view query) {
    return query.find('\0') == std::string_view::npos;
}

inline bool name_matches(std::string_view name, std::string_view query) {
    return valid(query) && object_query::contains_case_insensitive(name, query);
}

} // namespace g1r_mcp::function_query
