#pragma once

#include "sn2_mcp/runtime_package_writer.hpp"
#include <array>
#include <cstdint>
#include <functional>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace sn2_mcp::runtime_archive {

// Exact Shipping ABI established from the CL 131439 executable, SHA256
// 5d49f3be8cd5c78e20e492d9ba38e9b8f8a6eeec07ad2e7a96c103852f7c09ef.
// Addresses are supplied by the runtime owner after executable discovery.
inline constexpr std::size_t state_size=0x90;
inline constexpr std::size_t vtable_size=76;
inline constexpr std::uintptr_t state_constructor_rva=0x1459380;
inline constexpr std::uintptr_t state_destructor_rva=0x14596c0;
inline constexpr std::uintptr_t base_vtable_rva=0x972be98;

class ArchiveError : public std::runtime_error {
public:
    ArchiveError(std::string code,std::string message);
    const std::string code;
};

struct Bindings {
    std::uintptr_t state_constructor{},state_destructor{},base_vtable{};
    // Optional native FSoftObjectPath::SerializePath(FArchive&) binding.
    // It retains the game's custom-version and string serialization behavior.
    std::uintptr_t soft_path_serializer{};
};

enum class IndirectObject { object_ptr,weak_object_ptr };
struct FieldPath {
    std::vector<runtime_package::NameRef> path;
    std::int32_t owner{};
    bool filtered_editor_only{};
};
struct SoftPathIndex {
    // FLinkerSave writes an index only when the package has a nonempty
    // SoftObjectPath table. Inline paths use the native binding above.
    std::int32_t index{};
};
struct Mappers {
    // A live FName is 8 bytes. Its number component must be preserved.
    std::function<runtime_package::NameRef(std::uint64_t)> name;
    std::function<std::int32_t(std::uintptr_t)> object;
    std::function<FieldPath(std::uintptr_t)> field;
    // These callbacks receive the address of the live wrapper, rather than
    // guessing FObjectHandle encoding or resolving stale weak references.
    std::function<std::uintptr_t(IndirectObject,std::uintptr_t)> indirect_object;
    std::function<std::array<std::uint32_t,4>(std::uintptr_t)> lazy_guid;
    std::function<SoftPathIndex(std::uintptr_t)> soft_path_index;
};
struct Options {
    bool filter_editor_only{true};
    bool unversioned_properties{false};
    std::int32_t serializing_defaults{};
    std::uint32_t port_flags{};
};
struct Result {
    runtime_package::ExportData data;
    std::int32_t ue4_version{},ue5_version{},licensee_version{};
    runtime_package::EngineVersion engine_version;
    std::vector<runtime_package::CustomVersion> custom_versions;
    std::int64_t script_serialization_start{},script_serialization_end{};
    std::uint64_t raw_writes{},name_writes{},object_writes{},field_writes{};
};
using SerializerInvoker=std::function<void(std::uintptr_t,void*)>;

// The invoker must call the verified serializer of the actual live object on
// the game thread. No UObject::Serialize vtable slot is guessed here. The
// constructed state and its custom-version allocations belong to the game;
// the storage, vtable clone and output belong to the bridge. Callback failures
// poison the operation and are thrown only after the engine call returns.
Result serialize(std::uintptr_t object,const SerializerInvoker& invoke,
                 const Bindings& bindings,const Mappers& mappers,
                 const Options& options={});

} // namespace sn2_mcp::runtime_archive
