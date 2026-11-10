#pragma once

#include <g1r_mcp/transport.hpp>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstring>
#include <set>
#include <span>
#include <string>
#include <vector>

namespace g1r_mcp::struct_values {
struct Component {
    std::string name;
    std::string type;
    int element_size{};
    int offset{};
    int array_dim{};
    bool operator==(const Component&) const=default;
};
struct Layout {
    int properties_size{};
    int min_alignment{};
    bool has_super{};
    std::vector<Component> components;
    bool operator==(const Layout&) const=default;
};
inline void validate_layout(const Layout& layout) {
    if(layout.has_super || layout.components.size()!=3
       || (layout.properties_size!=12 && layout.properties_size!=24))
        throw HandlerError("unsupported_struct_layout","The struct requires exactly three own numeric fields and size 12 or 24");
    const int width=layout.properties_size/3;
    if(layout.min_alignment<1 || layout.min_alignment>width
       || (layout.min_alignment&(layout.min_alignment-1)) || layout.properties_size%layout.min_alignment)
        throw HandlerError("unsupported_struct_layout","The struct alignment is unsupported");
    std::set<std::string> names;
    std::set<int> offsets;
    for(const auto& component:layout.components) {
        if(component.array_dim!=1 || component.element_size!=width
           || component.type!=(width==4?"FloatProperty":"DoubleProperty")
           || component.offset<0 || component.offset%width || component.offset>layout.properties_size-width
           || !names.insert(component.name).second || !offsets.insert(component.offset).second)
            throw HandlerError("unsupported_struct_layout","Struct fields must be separate uniform float or double fields");
    }
    if(names!=std::set<std::string>{"X","Y","Z"}
       && names!=std::set<std::string>{"Pitch","Yaw","Roll"})
        throw HandlerError("unsupported_struct_layout","Unsupported struct component names");
    if(offsets!=std::set<int>{0,width,2*width})
        throw HandlerError("unsupported_struct_layout","The struct fields do not cover all copied bytes");
}
inline nlohmann::json copied_components(const Layout& layout,std::span<const std::byte> bytes) {
    validate_layout(layout);
    if(bytes.size()!=static_cast<size_t>(layout.properties_size))
        throw HandlerError("invalid_struct_copy","The struct copy size does not match its layout");
    nlohmann::json result=nlohmann::json::array();
    for(const auto& component:layout.components) {
        double value{};
        if(component.element_size==4) {
            float copied{};std::memcpy(&copied,bytes.data()+component.offset,sizeof(copied));value=copied;
        } else std::memcpy(&value,bytes.data()+component.offset,sizeof(value));
        if(!std::isfinite(value))throw HandlerError("invalid_struct_value","The struct component is not finite");
        result.push_back({{"name",component.name},{"type",component.type},{"element_size",component.element_size},
                          {"offset",component.offset},{"value",value}});
    }
    return result;
}
} // namespace g1r_mcp::struct_values
