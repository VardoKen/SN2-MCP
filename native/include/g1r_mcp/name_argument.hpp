#pragma once

#include <g1r_mcp/transport.hpp>
#include <cstdint>
#include <functional>
#include <string>
#include <utility>

namespace g1r_mcp {
struct NameArgumentSource {
    std::string handle;
    std::string property_name;
    bool operator==(const NameArgumentSource&) const=default;
};
inline void validate_name_argument_layout(int size,int array_dim) {
    if(size!=8 || array_dim!=1)
        throw HandlerError("unsupported_signature","A FName argument requires 8 bytes and ArrayDim=1");
}
inline NameArgumentSource name_argument_source(const nlohmann::json& value) {
    if(!value.is_object() || value.size()!=1 || !value.contains("name_from"))
        throw HandlerError("invalid_name_argument","FName requires only {name_from:{handle,property_name}}");
    const auto& source=value.at("name_from");
    if(!source.is_object() || source.size()!=2 || !source.contains("handle") || !source.contains("property_name")
       || !source.at("handle").is_string() || !source.at("property_name").is_string())
        throw HandlerError("invalid_name_argument","A FName source requires only handle and property_name");
    auto handle=source.at("handle").get<std::string>(),property=source.at("property_name").get<std::string>();
    if(handle.empty() || handle.find('\0')!=std::string::npos
       || property.empty() || property.find('\0')!=std::string::npos)
        throw HandlerError("invalid_name_argument","FName source handle and property_name must be nonempty and contain no NUL");
    return {std::move(handle),std::move(property)};
}
template<class Capture>
auto validated_name_argument(int size,int array_dim,const nlohmann::json& value,Capture&& capture) {
    validate_name_argument_layout(size,array_dim);
    const auto source=name_argument_source(value);
    return std::invoke(std::forward<Capture>(capture),source);
}
} // namespace g1r_mcp
