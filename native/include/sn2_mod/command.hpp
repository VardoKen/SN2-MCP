#pragma once

#include <cstdint>
#include <type_traits>

namespace sn2_mod {
inline constexpr std::uint32_t CommandAbi = 1;

enum class Operation : std::uint32_t { start = 1, status = 2, disable = 3 };
enum class Result : std::uint32_t {
    ok = 0,
    invalid_request = 1,
    response_too_small = 2,
    command_failed = 3
};

// The remote caller owns both this structure and the response allocation.
// Side-effect commands are admitted once by request_id. A larger response is
// obtained by STATUS with that same ID, never by repeating START or DISABLE.
struct alignas(8) Command {
    std::uint32_t size = sizeof(Command);
    std::uint32_t abi = CommandAbi;
    std::uint32_t operation{};
    std::uint32_t expected_pid{};
    std::uint64_t expected_creation_filetime{};
    std::uint64_t request_id{};
    std::uint64_t response_address{};
    std::uint64_t response_capacity{};
    std::uint64_t response_size{};
    std::uint64_t response_required{};
    std::uint32_t result_code{};
    std::uint32_t reserved{};
};

static_assert(sizeof(Command) == 72);
static_assert(std::is_standard_layout_v<Command>);
static_assert(std::is_trivially_copyable_v<Command>);
} // namespace sn2_mod
