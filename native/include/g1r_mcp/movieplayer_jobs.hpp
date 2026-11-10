#pragma once

#include <g1r_mcp/job_state.hpp>
#include <cstdint>
#include <deque>
#include <functional>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <nlohmann/json.hpp>

namespace g1r_mcp {

// Only cached status and explicitly requested StopMovie. Native callbacks execute
// in the Platform pump on a live WaitFrame thread; reading the cache does not invoke them.
class MoviePlayerJobs {
public:
    using Json = nlohmann::json;
    using ReadFlag = std::function<bool(std::uintptr_t player)>;
    using StopMovie = std::function<void(std::uintptr_t player)>;

    struct FinishJob {
        FinishJob(std::string session, std::string wait);
        const std::string session_id;
        const std::string expected_wait_id;
        JobState state;
        std::promise<Json> result;
    };

    class WaitFrame {
    public:
        WaitFrame(MoviePlayerJobs& owner, std::uintptr_t player,
                  std::uint32_t thread_id, bool allow_engine_tick,
                  std::uint64_t entered_ms);
        ~WaitFrame() noexcept;
        WaitFrame(const WaitFrame&) = delete;
        WaitFrame& operator=(const WaitFrame&) = delete;
        void returned(std::uint64_t now_ms) noexcept;
        const std::string& wait_id() const noexcept { return context_.wait_id; }

    private:
        friend class MoviePlayerJobs;
        struct Context {
            std::string wait_id;
            std::uint32_t thread_id{};
            bool allow_engine_tick{};
            std::uint64_t entered_ms{};
            std::optional<std::uint64_t> sampled_ms;
            std::optional<bool> loading_finished;
            std::optional<bool> movie_playing;
            bool finish_call_completed{};
            Json sample_error = nullptr;
        };
        MoviePlayerJobs& owner_;
        std::uintptr_t player_{};
        WaitFrame* previous_{};
        bool linked_{};
        Context context_;
    };

    Json cached_status() const;
    std::shared_ptr<FinishJob> request_finish(const std::string& session_id,
                                            const std::string& expected_wait_id);
    bool cancel_finish(const std::shared_ptr<FinishJob>& job);
    void disconnect(const std::string& session_id);
    void sample_and_service(std::uint32_t thread_id, std::uint64_t now_ms,
                            const ReadFlag& loading_finished,
                            const ReadFlag& movie_playing,
                            const StopMovie& stop_movie) noexcept;

private:
    struct Cache {
        bool active_wait{};
        bool wait_returned{};
        std::optional<WaitFrame::Context> context;
        std::optional<std::string> last_returned_wait_id;
        std::optional<std::uint64_t> last_returned_ms;
    };
    static thread_local WaitFrame* active_frame_;
    static thread_local bool servicing_;
    mutable std::mutex mutex_;
    Cache cache_;
    std::deque<std::shared_ptr<FinishJob>> jobs_;
    std::uint64_t next_wait_id_{};

    void enter(WaitFrame& frame);
    void leave(WaitFrame& frame, std::optional<std::uint64_t> returned_ms) noexcept;
    void publish_sample(WaitFrame& frame);
    void fail_wait_jobs(const std::string& wait_id) noexcept;
    static void fail(const std::shared_ptr<FinishJob>& job,
                     const std::exception_ptr& error) noexcept;
    static void cancelled(const std::shared_ptr<FinishJob>& job) noexcept;
};

} // namespace g1r_mcp
