#pragma once

#include <g1r_mcp/string_arguments.hpp>
#include <g1r_mcp/string_return.hpp>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <span>
#include <utility>
#include <vector>

namespace g1r_mcp::string_input_lifetime {

class InputBudget {
public:
    void add(std::size_t units) {
        if (units > string_arguments::max_utf16_units)
            throw HandlerError("invalid_argument", "FString including its terminator does not fit int32");
        const auto bytes = (units + 1) * sizeof(char16_t);
        if (bytes > std::numeric_limits<std::size_t>::max() - bytes_)
            throw HandlerError("string_size_overflow", "The FString allocation size does not fit size_t");
        ++count_;
        bytes_ += bytes;
    }
    std::size_t input_count() const noexcept {return count_;}
    std::size_t allocation_bytes() const noexcept {return bytes_;}
private:
    std::size_t count_{},bytes_{};
};

struct Cleanup {
    bool attempted{},confirmed{},uncertain{};
    std::size_t released_count{};
};

inline bool same_header(const StringHeader& left, const StringHeader& right) noexcept {
    return left.data == right.data && left.count == right.count && left.capacity == right.capacity;
}

// Records appear only after confirmed FString construction, return or pure output.
// Ledger destruction releases nothing; uncertain ownership is quarantined.
class Ledger {
public:
    Ledger() = default;
    Ledger(const Ledger&) = delete;
    Ledger& operator=(const Ledger&) = delete;
    Ledger(Ledger&&) = delete;
    Ledger& operator=(Ledger&&) = delete;

    // Allocate host memory before constructing the first engine FString.
    void prepare(std::size_t input_count, std::size_t result_count = 1) {
        if (input_count > allocations_.max_size() || result_count > allocations_.max_size() - input_count)
            fail("string_lifetime_unknown", "FString ownership tracking exceeds its representable range");
        inputs_.resize(input_count);
        allocations_.resize(input_count+result_count);
    }

    void add_input(const StringHeader& header, std::size_t source_units) {
        if (uncertain_) throw_unknown();
        if (release_started_) fail("string_lifetime_unknown", "FString cannot be registered after cleanup starts");
        if (source_units > string_arguments::max_utf16_units)
            fail("invalid_argument", "FString including its terminator does not fit int32");
        if (input_count_ >= inputs_.size())
            fail("string_lifetime_unknown", "FString input tracking was not prepared before string construction");
        validate_owned_header(header);
        const auto expected_count = source_units ? static_cast<std::int32_t>(source_units + 1) : 0;
        if (header.count != expected_count || (!source_units && (header.data || header.capacity)))
            fail("string_lifetime_unknown", "The FString constructor returned an unexpected length");
        const auto bytes = allocation_size(header);
        if (bytes > std::numeric_limits<std::size_t>::max() - input_bytes_)
            fail("string_size_overflow", "The FString allocation size does not fit size_t");
        if (header.data) add_allocation(header, false);
        inputs_[input_count_++] = header;
        input_bytes_ += bytes;
    }

    // An exact range match denotes one owned allocation, including return aliases.
    // Partial overlaps or equal pointers with different capacities are invalid.
    void add_return(const StringHeader& header) {
        if (uncertain_) throw_unknown();
        if (release_started_) fail("string_lifetime_unknown", "A FString result cannot be registered after cleanup starts");
        if (return_added_) fail("string_lifetime_unknown", "The FString result was registered more than once");
        validate_owned_header(header);
        if (header.data) add_allocation(header, true);
        return_added_ = true;
    }
    void add_owned_range(std::span<const StringHeader> headers) {
        if (uncertain_) throw_unknown();
        if (release_started_) fail("string_lifetime_unknown", "FString payloads cannot be registered after cleanup starts");
        if (headers.size() > allocations_.max_size() - allocation_count_)
            fail("string_lifetime_unknown", "FString ownership tracking exceeds its representable range");
        const auto required = allocation_count_ + headers.size();
        if (required > allocations_.size()) {
            try {allocations_.resize(required);}
            catch (...) {uncertain_ = true;throw;}
        }
        for (const auto& header : headers) {
            validate_owned_header(header);
            if (header.data) add_allocation(header, true);
        }
    }

    void validate_unchanged_input(std::size_t index, const StringHeader& current) {
        if (index >= input_count_ || !same_header(inputs_[index], current))
            fail("string_lifetime_unknown", "The const FString input header changed");
        if (uncertain_) throw_unknown();
    }

    void mark_uncertain() noexcept {uncertain_ = true;}
    bool uncertain() const noexcept {return uncertain_;}
    std::size_t input_count() const noexcept {return input_count_;}
    std::size_t allocation_count() const noexcept {return allocation_count_;}
    std::size_t allocation_bytes() const noexcept {return allocation_bytes_;}
    std::size_t retained_count() const noexcept {return allocation_count_-released_count_;}
    std::size_t retained_bytes() const noexcept {return allocation_bytes_-released_bytes_;}

    template<class Releaser> Cleanup release(Releaser&& releaser) noexcept {
        Cleanup result{false, false, uncertain_, released_count_};
        if (uncertain_) return result;
        release_started_ = true;
        for (std::size_t i = 0; i < allocation_count_; ++i) {
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
            released_bytes_ += allocation_size(allocations_[i].header);
            result.released_count = released_count_;
        }
        result.confirmed = true;
        return result;
    }

private:
    struct Allocation {StringHeader header{};bool released{};};
    std::vector<StringHeader> inputs_;
    std::vector<Allocation> allocations_;
    std::size_t input_count_{},input_bytes_{},allocation_count_{},allocation_bytes_{};
    std::size_t released_count_{},released_bytes_{};
    bool uncertain_{},return_added_{},release_started_{};

    [[noreturn]] static void throw_unknown() {
        throw HandlerError("string_lifetime_unknown", "FString ownership is unconfirmed; release is prohibited");
    }
    [[noreturn]] void fail(const char* code,const char* message) {
        uncertain_ = true;
        throw HandlerError(code, message);
    }
    static std::size_t allocation_size(const StringHeader& header) noexcept {
        return static_cast<std::size_t>(header.capacity) * sizeof(char16_t);
    }
    void validate_owned_header(const StringHeader& header) {
        try {validate_string_header(header);}
        catch (...) {uncertain_ = true;throw;}
    }
    void add_allocation(const StringHeader& header, bool allow_alias) {
        const auto bytes = allocation_size(header);
        const auto end = header.data + bytes;
        for (std::size_t i = 0; i < allocation_count_; ++i) {
            const auto& original = allocations_[i].header;
            const auto original_end = original.data + allocation_size(original);
            if (header.data >= original_end || original.data >= end) continue;
            if (allow_alias && header.data == original.data && header.capacity == original.capacity) return;
            fail("string_lifetime_unknown", "The FString allocation has an unconfirmed alias");
        }
        if (allocation_count_ >= allocations_.size()
            || bytes > std::numeric_limits<std::size_t>::max() - allocation_bytes_)
            fail("string_lifetime_unknown", "FString ownership tracking exceeds its representable range");
        allocations_[allocation_count_++].header = header;
        allocation_bytes_ += bytes;
    }
};

} // namespace g1r_mcp::string_input_lifetime
