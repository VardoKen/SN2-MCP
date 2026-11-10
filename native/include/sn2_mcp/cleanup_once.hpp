#pragma once

#include <concepts>
#include <functional>
#include <type_traits>

namespace sn2_mcp {

// Preserve the complete first Cleanup result, including a failed release attempt.
// The action must report failures in Result without throwing.
template<class Result> class CleanupOnce {
    static_assert(std::is_nothrow_default_constructible_v<Result>);
    static_assert(std::is_nothrow_copy_constructible_v<Result>);
    static_assert(std::is_nothrow_copy_assignable_v<Result>);

public:
    CleanupOnce() = default;
    CleanupOnce(const CleanupOnce&) = delete;
    CleanupOnce& operator=(const CleanupOnce&) = delete;
    CleanupOnce(CleanupOnce&&) = delete;
    CleanupOnce& operator=(CleanupOnce&&) = delete;

    template<class Action>
        requires std::is_nothrow_invocable_v<Action&>
            && std::same_as<std::invoke_result_t<Action&>, Result>
    Result run(Action&& action) noexcept {
        if (!called_) {
            called_ = true;
            const Result completed = std::invoke(action);
            result_ = completed;
        }
        return result_;
    }

    bool called() const noexcept {return called_;}

private:
    Result result_{};
    bool called_{};
};

} // namespace sn2_mcp
