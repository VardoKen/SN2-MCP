#pragma once
#include <cstdint>
namespace sn2_mcp::runtime_package {
// CityHash64 variant used by the target Unreal package/name contracts.
std::uint64_t city_hash64(const char* bytes,std::uint32_t size);
}
