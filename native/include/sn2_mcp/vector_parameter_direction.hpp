#pragma once

#include <sn2_mcp/parameter_direction.hpp>

namespace sn2_mcp::vector_parameter_direction {

inline constexpr auto const_parm = parameter_direction::const_parm;
inline constexpr auto parm = parameter_direction::parm;
inline constexpr auto out_parm = parameter_direction::out_parm;
inline constexpr auto return_parm = parameter_direction::return_parm;
inline constexpr auto reference_parm = parameter_direction::reference_parm;

using Direction = parameter_direction::Direction;

// Const FVector input was confirmed by the reflected const T& form: ConstParm
// supplies constness; ReferenceParm or OutParm supplies reference semantics. For ProcessEvent,
// the argument remains an owned inline POD. Mutable references and inout are unsupported.
inline Direction classify(std::uint64_t flags) {
    if((flags & parm) && (flags & const_parm)
       && (flags & (reference_parm | out_parm)) && !(flags & return_parm))
        return Direction::input;
    return parameter_direction::classify(flags);
}

inline void validate_output_marker(const nlohmann::json& value) {
    parameter_direction::validate_output_marker(value);
}

} // namespace sn2_mcp::vector_parameter_direction
