#pragma once

#include <sn2_mcp/transport.hpp>
#include <array>
#include <cstddef>
#include <limits>
#include <span>
#include <utility>
#include <vector>

namespace sn2_mcp::aligned_parameter_buffer {

inline constexpr std::size_t alignment=16;

// Host code owns this memory; engine allocators and destructors are not used.
class alignas(alignment) Buffer {
public:
    explicit Buffer(std::size_t extent):size_(extent) {
        const auto blocks=extent/alignment+(extent%alignment!=0);
        if(blocks>std::numeric_limits<std::size_t>::max()/alignment || blocks>storage_.max_size())
            throw HandlerError("unsupported_signature", "The aligned parameter buffer size is not representable");
        storage_.resize(blocks);
    }
    Buffer(const Buffer&)=default;
    Buffer& operator=(const Buffer& other) {
        if(this!=&other) {
            Buffer copied(other);
            storage_.swap(copied.storage_);
            std::swap(size_,copied.size_);
        }
        return *this;
    }

    unsigned char* data() noexcept {
        return reinterpret_cast<unsigned char*>(storage_.data());
    }
    const unsigned char* data() const noexcept {
        return reinterpret_cast<const unsigned char*>(storage_.data());
    }
    std::size_t size() const noexcept {return size_;}
    std::span<std::byte> bytes() noexcept {return {reinterpret_cast<std::byte*>(storage_.data()),size_};}
    std::span<const std::byte> bytes() const noexcept {return {reinterpret_cast<const std::byte*>(storage_.data()),size_};}

private:
    // Zero the entire rounded allocation, including POD padding beyond the current extent.
    struct alignas(alignment) Block {std::array<std::byte,alignment> bytes{};};
    static_assert(sizeof(Block)==alignment && alignof(Block)==alignment);
    std::vector<Block> storage_;
    std::size_t size_{};
};

static_assert(alignof(Buffer)>=alignment);

} // namespace sn2_mcp::aligned_parameter_buffer
