#pragma once

#include <sn2_mcp/transport.hpp>
#include <Windows.h>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <functional>
#include <type_traits>
#include <utility>

namespace sn2_mcp::game_memory {

// SEH wraps only byte copying; this function has no objects requiring C++ unwinding.
inline bool copy(void* destination,const void* source,std::size_t bytes) noexcept {
    __try {std::memcpy(destination,source,bytes);return true;}
    __except(EXCEPTION_EXECUTE_HANDLER){return false;}
}

template<class Value>Value read(std::uintptr_t address) {
    static_assert(std::is_trivially_copyable_v<Value>);
    Value value{};
    if(!copy(&value,reinterpret_cast<const void*>(address),sizeof(Value)))
        throw HandlerError("invalid_memory","Game memory is unavailable");
    return value;
}

template<class NameDecoder>
std::string field_type(std::uintptr_t field,NameDecoder&& decode_name) {
    const auto field_class=read<std::uintptr_t>(field+8);
    return std::invoke(std::forward<NameDecoder>(decode_name),read<std::uint64_t>(field_class));
}

} // namespace sn2_mcp::game_memory
