#pragma once
#include "sn2_mcp/runtime_package_writer.hpp"
#include <functional>

namespace sn2_mcp::runtime_package {
std::uint64_t package_id(const std::string& package_name);
std::uint64_t script_import_index(const std::string& object_path);
std::uint64_t public_export_hash(const std::string& package_relative_path);
std::uint64_t zen_name_hash(const std::string& name);
std::array<std::uint8_t,20> io_hash(std::span<const std::uint8_t> bytes);

struct IoStoreOptions {
    std::string container_name;
    // Unreal-relative virtual filename of this package's .uasset. Indexing
    // points at its ExportBundleData chunk; loading still uses PackageStore.
    std::string package_filename;
    std::uint32_t compression_block_size{65536};
    // Optional confirmation against the live target's script-object registry
    // and external imported public exports. No game paths are invented here.
    std::function<bool(const std::string&,std::uint64_t)> script_import_exists;
    std::function<bool(const std::string&,std::uint64_t)> package_export_exists;
};
struct IoStorePackage {
    std::vector<std::uint8_t> zen_package,container_header,utoc,ucas;
    std::uint64_t id{},container_id{};
    std::vector<std::uint64_t> imported_package_ids,import_map,public_export_hashes;
    std::vector<std::array<std::uint32_t,2>> export_bundle;
};
// Converts the validated no-bulk legacy package into a real Zen package and an
// uncompressed indexed UToc v8/UCAS container. The existing game's global
// ScriptObjects chunk supplies native imports. No Editor/cooker is invoked.
IoStorePackage write_io_store(const Package& package,const CookedPackage& cooked,const IoStoreOptions& options);
} // namespace sn2_mcp::runtime_package
