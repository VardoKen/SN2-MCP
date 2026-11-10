#pragma once

#include <g1r_mcp/transport.hpp>
#include <cstdint>
#include <functional>
#include <utility>

namespace g1r_mcp {

inline void validate_name_return_layout(int size,int array_dim) {
    if(size!=8 || array_dim!=1)
        throw HandlerError("unsupported_signature","A FName return requires 8 bytes and ArrayDim=1");
}
inline void reject_name_input() {
    throw HandlerError("unsupported_signature","FName input is unsupported");
}

// Pass the returned FName to the existing pool decoder as a copied
// 64-bit value. No name construction or engine memory release is required.
template<class Reader,class Decoder>
auto copied_name_return(int size,int array_dim,Reader&& reader,Decoder&& decoder) {
    validate_name_return_layout(size,array_dim);
    const std::uint64_t copied=std::invoke(std::forward<Reader>(reader));
    return std::invoke(std::forward<Decoder>(decoder),copied);
}

} // namespace g1r_mcp
