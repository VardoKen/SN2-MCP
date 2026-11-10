#pragma once

#include <algorithm>
#include <cctype>
#include <string>
#include <string_view>

namespace g1r_mcp::object_query {

inline bool valid_exact_path(std::string_view value) {
    return value.find('\0') == std::string_view::npos;
}

inline bool query_uses_path(std::string_view query) {
    return query.find('/') != std::string_view::npos || query.find('.') != std::string_view::npos;
}

inline bool contains_case_insensitive(std::string_view value, std::string_view query) {
    if (query.empty()) return true;
    return std::search(value.begin(), value.end(), query.begin(), query.end(),
        [](char left, char right) {
            return std::tolower(static_cast<unsigned char>(left)) ==
                   std::tolower(static_cast<unsigned char>(right));
        }) != value.end();
}

inline bool exact_path_may_match_name(std::string_view exact_path, std::string_view name) {
    return exact_path.empty() || (!name.empty() && exact_path.ends_with(name));
}

inline bool needs_path(std::string_view query, std::string_view exact_path, std::string_view name) {
    return !exact_path.empty() || (query_uses_path(query) && !contains_case_insensitive(name, query));
}

// path is read only when needs_path() returned true. The emitted Unreal path is not normalized.
inline bool matches(std::string_view query, std::string_view exact_path,
                    std::string_view name, std::string_view path) {
    if (!valid_exact_path(exact_path) || !exact_path_may_match_name(exact_path, name)) return false;
    if (!exact_path.empty() && exact_path != path) return false;
    return contains_case_insensitive(name, query) ||
           (query_uses_path(query) && contains_case_insensitive(path, query));
}

} // namespace g1r_mcp::object_query
