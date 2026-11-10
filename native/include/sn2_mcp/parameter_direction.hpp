#pragma once

#include <sn2_mcp/transport.hpp>
#include <cstdint>

namespace sn2_mcp::parameter_direction {

// EPropertyFlags values were confirmed by the UE4SS JMapGenerator.cpp table
// b4cefa18ac7349c4389c1ce244c1a70b66a05fe5, lines 564, 570-574 and 588.
// 0x800 is DisableEditOnTemplate; ReferenceParm is 0x08000000.
inline constexpr std::uint64_t const_parm = 0x2;
inline constexpr std::uint64_t parm = 0x80;
inline constexpr std::uint64_t out_parm = 0x100;
inline constexpr std::uint64_t return_parm = 0x400;
inline constexpr std::uint64_t reference_parm = 0x08000000;

enum class Direction { input, output };

// Only value arguments or pure outputs. The caller validates parameter
// type and layout; const references, inout parameters and returns are excluded.
inline Direction classify(std::uint64_t flags) {
    if(!(flags & parm) || (flags & (return_parm | const_parm | reference_parm)))
        throw HandlerError("unsupported_signature", "The parameter requires a value input or pure output without Const, Reference or Return");
    return flags & out_parm ? Direction::output : Direction::input;
}

inline void validate_output_marker(const nlohmann::json& value) {
    if(!value.is_object() || value.size()!=1 || !value.contains("out")
       || !value.at("out").is_boolean() || !value.at("out").get<bool>())
        throw HandlerError("invalid_argument", "An output parameter requires the exact {out:true} marker");
}

} // namespace sn2_mcp::parameter_direction
