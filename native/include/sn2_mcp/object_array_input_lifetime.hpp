#pragma once

#include <sn2_mcp/array_page.hpp>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <vector>

namespace sn2_mcp::object_array_input_lifetime {

inline constexpr std::size_t element_size = 8;

class InputBudget {
public:
    void add(std::size_t count, std::size_t stride = element_size) {
        if (count > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
            throw HandlerError("invalid_array_count", "The object array length is not representable by int32 TArray");
        const auto bytes = count * stride;
        ++count_;
        bytes_ += bytes;
    }

    std::size_t input_count() const noexcept {return count_;}
    std::size_t allocation_bytes() const noexcept {return bytes_;}

private:
    std::size_t count_{}, bytes_{};
};

struct Cleanup {
    bool attempted{}, confirmed{}, uncertain{};
    std::size_t released_count{};
};

// Ledger owns only array buffers allocated by the engine Realloc function.
// It never owns UObject instances and never passes them to Free.
// The caller validates element bytes and serial identity.
class Ledger {
public:
    Ledger() = default;
    Ledger(const Ledger&) = delete;
    Ledger& operator=(const Ledger&) = delete;
    Ledger(Ledger&&) = delete;
    Ledger& operator=(Ledger&&) = delete;

    // The caller reserves records before the first engine Realloc.
    void reserve_inputs(std::size_t count) {
        inputs_.reserve(count);
        allocations_.reserve(count);
    }

    void add_input(const ArrayHeader& header, std::size_t expected_count,
                   std::size_t stride = element_size, std::size_t alignment = 0) {
        if (uncertain_) throw_unknown();
        if (release_started_)
            fail("array_lifetime_unknown", "An array cannot be registered after cleanup starts");
        validate_header(header, expected_count, stride, alignment ? alignment : stride);
        if (header.data) add_allocation(header, stride);
        inputs_.push_back(header);
    }

    void validate_unchanged_input(std::size_t index, const ArrayHeader& current) {
        if (uncertain_) throw_unknown();
        if (index >= inputs_.size() || !same_array_header(inputs_[index], current))
            fail("array_lifetime_unknown", "The const object array header changed");
    }

    void mark_uncertain() noexcept {uncertain_ = true;}
    bool uncertain() const noexcept {return uncertain_;}
    std::size_t input_count() const noexcept {return inputs_.size();}
    std::size_t allocation_count() const noexcept {return allocations_.size();}
    std::size_t allocation_bytes() const noexcept {return allocation_bytes_;}
    std::size_t retained_count() const noexcept {return allocations_.size() - released_count_;}
    std::size_t retained_bytes() const noexcept {return allocation_bytes_ - released_bytes_;}

    template<class Releaser> Cleanup release(Releaser&& releaser) noexcept {
        Cleanup result{false, false, uncertain_, released_count_};
        if (uncertain_) return result;
        release_started_ = true;
        for (std::size_t i = 0; i < allocations_.size(); ++i) {
            if (allocations_[i].released) continue;
            result.attempted = true;
            bool confirmed{};
            try {confirmed = std::invoke(releaser, allocations_[i].header.data);}
            catch (...) {confirmed = false;}
            if (!confirmed) {
                uncertain_ = true;
                result.uncertain = true;
                return result;
            }
            allocations_[i].released = true;
            ++released_count_;
            released_bytes_ += allocations_[i].bytes;
            result.released_count = released_count_;
        }
        result.confirmed = true;
        return result;
    }

private:
    struct Allocation {ArrayHeader header{}; std::size_t bytes{}; bool released{};};
    std::vector<ArrayHeader> inputs_;
    std::vector<Allocation> allocations_;
    std::size_t allocation_bytes_{};
    std::size_t released_count_{}, released_bytes_{};
    bool uncertain_{}, release_started_{};

    [[noreturn]] static void throw_unknown() {
        throw HandlerError("array_lifetime_unknown", "Array buffer ownership is unconfirmed; release is prohibited");
    }

    [[noreturn]] void fail(const char* code, const char* message) {
        uncertain_ = true;
        throw HandlerError(code, message);
    }

    static std::size_t allocation_size(const ArrayHeader& header, std::size_t stride) noexcept {
        return static_cast<std::size_t>(header.capacity) * stride;
    }

    void validate_header(const ArrayHeader& header, std::size_t expected_count, std::size_t stride, std::size_t alignment) {
        if (expected_count > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
            fail("invalid_array_header", "The object array length is not representable by int32 TArray");
        const auto count = static_cast<std::int32_t>(expected_count);
        if (header.count != count || header.capacity != count
            || (!count && header.data) || (count && !header.data)
            || (header.data && header.data % alignment))
            fail("invalid_array_header", "An object array requires exact count/capacity and an aligned engine buffer");
        const auto bytes = expected_count * stride;
        if (header.data > std::numeric_limits<std::uintptr_t>::max() - bytes)
            fail("invalid_array_header", "The object array address range overflows");
    }

    void add_allocation(const ArrayHeader& header, std::size_t stride) {
        const auto bytes = allocation_size(header, stride);
        const auto end = header.data + bytes;
        for (std::size_t i = 0; i < allocations_.size(); ++i) {
            const auto& original = allocations_[i];
            const auto original_end = original.header.data + original.bytes;
            if (header.data >= original_end || original.header.data >= end) continue;
            fail("array_lifetime_unknown", "Object arrays have an unconfirmed buffer alias");
        }
        allocations_.push_back({header,bytes,false});
        allocation_bytes_ += bytes;
    }
};

} // namespace sn2_mcp::object_array_input_lifetime
