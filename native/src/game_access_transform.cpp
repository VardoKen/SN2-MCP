#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/game_memory.hpp"
#include "sn2_mcp/transform_layout.hpp"

namespace sn2_mcp {

transform_layout::Snapshot GameAccess::capture_transform_parameter(std::uintptr_t field,int size,int dim) const {
    const auto capture=[&] {
        return transform_layout::capture(field,size,dim,
            []<class Value>(std::uintptr_t address) {return game_memory::read<Value>(address);},
            [this](std::uint64_t value) {return name(value);},
            [this](std::uintptr_t object) {return serial_identity(validate_object(reinterpret_cast<void*>(object)));},
            [this](std::uintptr_t object_class) {validate_class(reinterpret_cast<void*>(object_class));},
            [this](std::uintptr_t object) {return object_path(reinterpret_cast<void*>(object));});
    };
    const auto before=capture();
    const auto after=capture();
    if(before!=after)
        throw HandlerError("transform_metadata_changed", "Reflected Transform metadata changed during validation");
    return before;
}

} // namespace sn2_mcp
