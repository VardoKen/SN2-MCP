#pragma once
#include <array>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

namespace sn2_mcp::runtime_package {
struct PakFile {
    std::string relative_path;
    std::vector<std::uint8_t> bytes;
};
struct PakOptions {
    std::string mount_point;
    std::uint64_t path_hash_seed{};
};
struct PakEntryLayout {
    std::string relative_path;
    std::int64_t header_offset{},payload_offset{},size{};
    std::array<std::uint8_t,20> hash{};
};
struct PakArchive {
    std::vector<std::uint8_t> bytes;
    std::int64_t index_offset{},index_size{},directory_offset{},directory_size{};
    std::vector<PakEntryLayout> entries;
};
std::array<std::uint8_t,20> pak_sha1(std::span<const std::uint8_t> bytes);
// Uncompressed, unencrypted Pak v11 with a real full directory index. Files
// are caller-provided mod metadata/content; no placeholder file is inserted.
PakArchive write_pak(const PakOptions& options,std::span<const PakFile> files);
struct ParsedPak {PakOptions options;std::vector<PakFile> files;PakArchive layout;};
// Verifies footer and index hashes, directory locations and entry payloads.
// This bounded parser accepts the writer's full-directory/unencoded variant.
ParsedPak read_pak(std::span<const std::uint8_t> bytes);
} // namespace sn2_mcp::runtime_package
