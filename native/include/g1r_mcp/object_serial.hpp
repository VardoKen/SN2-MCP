#pragma once

#include <cstdint>
#include <string_view>

namespace g1r_mcp {
// Observed FUObjectItem::SerialNumber values. This contract never calls
// AllocateSerialNumber or modifies the registry.
struct ObjectSerialIdentity {
    std::uintptr_t object{};
    std::uintptr_t object_class{};
    std::int32_t index{};
    std::uint64_t name{};
    std::int32_t serial{};
    std::int32_t class_index{};
    std::uint64_t class_name{};
    std::int32_t class_serial{};
    bool operator==(const ObjectSerialIdentity&) const=default;
};

inline bool valid_observed_serial(std::int32_t serial) noexcept {
    return serial>=0;
}

// Strict equality includes zero; an observed serial change invalidates
// the old handle. Matching zeros do not prove protection against object reuse.
inline bool same_serial_identity(const ObjectSerialIdentity& left,
                                 const ObjectSerialIdentity& right) noexcept {
    return valid_observed_serial(left.serial) && valid_observed_serial(left.class_serial)
        && valid_observed_serial(right.serial) && valid_observed_serial(right.class_serial)
        && left.object==right.object && left.object_class==right.object_class
        && left.index==right.index && left.name==right.name
        && left.serial==right.serial && left.class_index==right.class_index
        && left.class_name==right.class_name && left.class_serial==right.class_serial;
}

inline std::string_view serial_coverage(const ObjectSerialIdentity& identity) noexcept {
    if(!valid_observed_serial(identity.serial) || !valid_observed_serial(identity.class_serial))
        return "invalid";
    if(identity.serial && identity.class_serial)return "object_and_class_nonzero";
    if(identity.serial)return "object_nonzero";
    if(identity.class_serial)return "class_nonzero";
    return "unavailable_zero";
}
} // namespace g1r_mcp
