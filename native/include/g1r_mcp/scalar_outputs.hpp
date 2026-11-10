#pragma once

#include <g1r_mcp/transport.hpp>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <span>
#include <string>

namespace g1r_mcp::scalar_outputs {

// POD field from a ProcessEvent parameter copy. The caller checks CPF and offset.
struct Layout {
    std::string type;
    int element_size{};
    int array_dim{};
    std::uint8_t field_size{},byte_offset{},byte_mask{},field_mask{};
    bool operator==(const Layout&) const=default;
};

inline void validate_layout(const Layout& layout) {
    if(layout.array_dim!=1)
        throw HandlerError("unsupported_scalar_output_layout","Scalar output requires ArrayDim=1");
    if(layout.type=="BoolProperty") {
        const auto mask=layout.field_mask;
        const bool byte_mask_valid=layout.byte_mask!=0
            && (layout.byte_mask==0xff || !(layout.byte_mask&(layout.byte_mask-1)));
        // Native bool reads the whole byte (FieldMask=0xff); its setter may write only ByteMask=1.
        // Bitfield bool preserves the exact same single read/write mask.
        const bool masks_valid=mask==0xff?byte_mask_valid:
            mask!=0 && !(mask&(mask-1)) && layout.byte_mask==mask;
        if(layout.element_size!=1 || layout.field_size!=1 || layout.byte_offset!=0
           || !masks_valid)
            throw HandlerError("unsupported_scalar_output_layout","Bool output requires one byte and a confirmed mask");
        return;
    }
    const int expected=(layout.type=="IntProperty" || layout.type=="UInt32Property" || layout.type=="FloatProperty")?4:
        (layout.type=="Int64Property" || layout.type=="UInt64Property" || layout.type=="DoubleProperty")?8:0;
    if(expected==0 || layout.element_size!=expected)
        throw HandlerError("unsupported_scalar_output_layout","The scalar output type or size is unsupported");
}

template<class T>inline T copied_value(std::span<const std::byte> bytes) {
    T value{};
    std::memcpy(&value,bytes.data(),sizeof(value));
    return value;
}

inline nlohmann::json decode(const Layout& layout,std::span<const std::byte> bytes) {
    scalar_outputs::validate_layout(layout);
    if(bytes.size()!=static_cast<std::size_t>(layout.element_size))
        throw HandlerError("invalid_scalar_copy","The scalar output copy size does not match reflection");
    if(layout.type=="BoolProperty")
        return (copied_value<std::uint8_t>(bytes)&layout.field_mask)!=0;
    if(layout.type=="IntProperty")return copied_value<std::int32_t>(bytes);
    if(layout.type=="Int64Property")return copied_value<std::int64_t>(bytes);
    if(layout.type=="UInt32Property")return copied_value<std::uint32_t>(bytes);
    if(layout.type=="UInt64Property")return copied_value<std::uint64_t>(bytes);
    const double value=layout.type=="FloatProperty"?static_cast<double>(copied_value<float>(bytes)):copied_value<double>(bytes);
    if(!std::isfinite(value))
        throw HandlerError("invalid_scalar_value","Scalar output contains NaN or Infinity");
    return value;
}

} // namespace g1r_mcp::scalar_outputs
