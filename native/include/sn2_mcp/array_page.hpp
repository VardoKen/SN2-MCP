#pragma once

#include <sn2_mcp/transport.hpp>
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>

namespace sn2_mcp {

struct ArrayHeader {
    std::uintptr_t data{};
    std::int32_t count{};
    std::int32_t capacity{};
};
static_assert(sizeof(ArrayHeader)==16);
struct ArrayPage {
    std::uintptr_t begin{};
    std::size_t bytes{};
    std::int32_t offset{};
    std::int32_t count{};
    std::int32_t next_offset{};
    bool has_more{};
};

inline bool same_array_header(const ArrayHeader& left,const ArrayHeader& right) {
    return left.data==right.data && left.count==right.count && left.capacity==right.capacity;
}

inline ArrayPage array_page(const ArrayHeader& header,int stride,int array_dim,
                            std::int32_t offset,std::size_t limit) {
    if(stride<1 || array_dim!=1)
        throw HandlerError("unsupported_array_layout","Unsupported element stride or ArrayDim");
    if(header.count<0 || header.capacity<header.count || (!header.data && (header.count || header.capacity)))
        throw HandlerError("invalid_array_header","Invalid array sizes or pointer");
    if(header.data && header.capacity==0)
        throw HandlerError("invalid_array_header","A nonzero array pointer requires capacity");
    if(offset<0 || offset>header.count || limit<1)
        throw HandlerError("invalid_array_page","A page requires offset within the array and a positive limit");
    const auto unsigned_stride=static_cast<std::uintptr_t>(stride);
    const auto capacity_bytes=static_cast<std::uintptr_t>(header.capacity)*unsigned_stride;
    if(header.data>std::numeric_limits<std::uintptr_t>::max()-capacity_bytes)
        throw HandlerError("invalid_array_header","The array address range overflows");
    const auto count=static_cast<std::int32_t>(std::min<std::size_t>(
        static_cast<std::size_t>(header.count-offset),limit));
    const auto bytes=static_cast<std::size_t>(count)*static_cast<std::size_t>(stride);
    const auto begin=header.data+static_cast<std::uintptr_t>(offset)*unsigned_stride;
    return {begin,bytes,offset,count,offset+count,offset+count<header.count};
}

} // namespace sn2_mcp
