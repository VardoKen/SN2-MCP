#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace sn2_mcp::runtime_package {
// Wire contract: the acquired custom UE 5.6.1 PackageFileSummary.cpp,
// ObjectResource.cpp, UnrealNames.cpp and FieldPath.cpp. This writes package
// tables around serializer output; it is not a UObject snapshot serializer.
inline constexpr std::uint32_t file_tag=0x9e2a83c1;
inline constexpr std::uint32_t cooked_flag=0x200;
inline constexpr std::uint32_t unversioned_properties_flag=0x2000;
inline constexpr std::uint32_t filter_editor_only_flag=0x80000000;
inline constexpr std::int32_t current_ue4_version=522;
inline constexpr std::int32_t current_ue5_version=1017;

class PackageError : public std::runtime_error {
public:
    PackageError(std::string code,std::string message);
    const std::string code;
};

struct NameRef {
    std::int32_t index{};
    std::int32_t number{};
    bool operator==(const NameRef&) const = default;
};
struct NameEntry {
    std::string text;
    std::uint16_t non_case_hash{},case_hash{};
    bool operator==(const NameEntry&) const = default;
};
NameEntry make_name(const std::string& utf8);
// Shared wire encodings used by the IoStore optimizer. These encode actual
// Engine FString or UTF-16 units, without game calls or name-pool mutation.
std::vector<std::uint16_t> string_utf16(const std::string& utf8);
std::vector<std::uint8_t> encode_string(const std::string& utf8);

struct EngineVersion {
    std::uint16_t major{},minor{},patch{};
    std::uint32_t changelist{};
    std::string branch;
    bool operator==(const EngineVersion&) const = default;
};
struct CustomVersion {
    std::array<std::uint32_t,4> guid{};
    std::int32_t version{};
    bool operator==(const CustomVersion&) const = default;
};
struct SoftObjectPath {
    NameRef package,asset;
    std::string subpath;
    bool operator==(const SoftObjectPath&) const = default;
};
struct Import {
    NameRef class_package,class_name,name;
    // FPackageIndex: zero=null, positive=export ordinal+1, negative=-(import+1).
    std::int32_t outer{};
    bool optional{};
    bool operator==(const Import&) const = default;
};
struct Dependencies {
    std::vector<std::int32_t> serialization_before_serialization;
    std::vector<std::int32_t> create_before_serialization;
    std::vector<std::int32_t> serialization_before_create;
    std::vector<std::int32_t> create_before_create;
    bool operator==(const Dependencies&) const = default;
};
enum class ReferenceKind { name,object,field_path };
struct ReferenceWrite {
    std::size_t offset{},size{};
    ReferenceKind kind{};
    NameRef name;
    std::int32_t object{};
    std::vector<NameRef> path;
};
struct ExportData {
    std::vector<std::uint8_t> bytes;
    // Records correspond to actual package-local writes, not memory pointers.
    std::vector<ReferenceWrite> references;
};
// A serializer backend can target this sink after harvesting all package names
// and objects. FFieldPath expands to a name array and a package-local owner.
class ExportSink {
public:
    void bytes(std::span<const std::uint8_t> value);
    void u8(std::uint8_t value);
    void u16(std::uint16_t value);
    void u32(std::uint32_t value);
    void i32(std::int32_t value);
    void u64(std::uint64_t value);
    void i64(std::int64_t value);
    void string(const std::string& utf8);
    void name(NameRef value);
    void object(std::int32_t package_index);
    void field_path(std::span<const NameRef> path,std::int32_t owner);
    std::size_t tell() const noexcept;
    std::size_t size() const noexcept;
    // Real serializers seek back to fix storage lengths. Seeking is bounded
    // by the current byte extent; writing may extend that extent at its end.
    void seek(std::size_t position);
    void patch_i32(std::size_t offset,std::int32_t value);
    ExportData take();
private:
    ExportData data_;
    std::size_t position_{};
};
struct Export {
    NameRef name;
    std::int32_t class_index{},super_index{},template_index{},outer_index{};
    std::uint32_t object_flags{},package_flags{};
    bool forced{},not_for_client{},not_for_server{},inherited_instance{};
    bool not_always_loaded_for_editor_game{},is_asset{},generate_public_hash{};
    std::int64_t script_serialization_start{},script_serialization_end{};
    Dependencies dependencies;
    ExportData data;
};
struct Package {
    std::string name;
    // Unversioned package files use the exact target's registered file/custom
    // versions on load. Explicit versions remain a supported wire variant.
    bool unversioned_file{true};
    std::int32_t ue4_version{current_ue4_version},ue5_version{current_ue5_version},licensee_version{};
    std::array<std::uint8_t,20> saved_hash{};
    std::vector<CustomVersion> custom_versions;
    std::uint32_t flags{cooked_flag|filter_editor_only_flag};
    std::uint32_t package_source{};
    EngineVersion saved_engine,compatible_engine;
    std::vector<std::int32_t> chunk_ids;
    std::vector<NameEntry> names;
    std::vector<SoftObjectPath> soft_object_paths;
    std::vector<Import> imports;
    std::vector<Export> exports;
    // Number of initial names used in export payloads (all by default).
    std::int32_t names_referenced_from_exports{-1};
};
struct Layout {
    std::int32_t summary_size{},header_size{},name_offset{},soft_object_path_offset{};
    std::int32_t import_offset{},export_offset{},depends_offset{},asset_registry_offset{},preload_offset{};
    std::int32_t preload_count{};
    std::int64_t bulk_data_offset{};
    std::vector<std::int64_t> export_offsets,export_sizes;
};
struct CookedPackage {
    std::vector<std::uint8_t> uasset,uexp;
    Layout layout;
};
// Pure serialization: no filesystem, game calls, cooker or container mounting.
// Payloads must come from a verified UObject serializer backend.
CookedPackage write(const Package& package);
struct ParsedPackage {Package package;Layout layout;};
// Parses the supported cooked, editor-filtered no-bulk legacy format. It
// validates boundaries and tables and preserves export bytes for wire checks.
ParsedPackage read(std::span<const std::uint8_t> uasset,std::span<const std::uint8_t> uexp);
} // namespace sn2_mcp::runtime_package
