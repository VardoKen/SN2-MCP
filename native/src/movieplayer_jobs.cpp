#include <g1r_mcp/movieplayer_jobs.hpp>
#include <g1r_mcp/transport.hpp>
#include <algorithm>
#include <utility>

namespace g1r_mcp {
namespace {
using Json = nlohmann::json;

Json sample_error(const std::exception_ptr& error) {
    try { std::rethrow_exception(error); }
    catch (const HandlerError& caught) { return {{"code", caught.code()}, {"message", caught.what()}}; }
    catch (const std::exception& caught) { return {{"code", "movieplayer_sample_failed"}, {"message", caught.what()}}; }
    catch (...) { return {{"code", "movieplayer_sample_failed"}, {"message", "MoviePlayer sample failed"}}; }
}

std::exception_ptr callback_error(const std::exception_ptr& error, bool stop_started) {
    try { std::rethrow_exception(error); }
    catch (const HandlerError&) { return error; }
    catch (const std::exception& caught) {
        return std::make_exception_ptr(HandlerError(
            stop_started ? "movieplayer_finish_failed" : "movieplayer_sample_failed", caught.what(),
            {{"executed", false}, {"may_have_executed", stop_started}, {"do_not_retry", stop_started}}));
    }
    catch (...) {
        return std::make_exception_ptr(HandlerError(
            stop_started ? "movieplayer_finish_failed" : "movieplayer_sample_failed",
            stop_started ? "StopMovie call failed" : "MoviePlayer sample failed",
            {{"executed", false}, {"may_have_executed", stop_started}, {"do_not_retry", stop_started}}));
    }
}
}

thread_local MoviePlayerJobs::WaitFrame* MoviePlayerJobs::active_frame_ = nullptr;
thread_local bool MoviePlayerJobs::servicing_ = false;

MoviePlayerJobs::FinishJob::FinishJob(std::string session, std::string wait)
    : session_id(std::move(session)), expected_wait_id(std::move(wait)) {}

MoviePlayerJobs::WaitFrame::WaitFrame(MoviePlayerJobs& owner, std::uintptr_t player,
                                    std::uint32_t thread_id, bool allow_engine_tick,
                                    std::uint64_t entered_ms)
    : owner_(owner), player_(player) {
    context_.thread_id = thread_id;
    context_.allow_engine_tick = allow_engine_tick;
    context_.entered_ms = entered_ms;
    owner_.enter(*this);
}

MoviePlayerJobs::WaitFrame::~WaitFrame() noexcept {
    if (linked_) owner_.leave(*this, std::nullopt);
}

void MoviePlayerJobs::WaitFrame::returned(std::uint64_t now_ms) noexcept {
    if (linked_) owner_.leave(*this, now_ms);
}

void MoviePlayerJobs::enter(WaitFrame& frame) {
    std::lock_guard lock(mutex_);
    frame.context_.wait_id = "wait-" + std::to_string(++next_wait_id_);
    auto next = cache_;
    next.context = frame.context_;
    next.active_wait = true;
    next.wait_returned = false;
    cache_ = std::move(next);
    frame.previous_ = active_frame_;
    active_frame_ = &frame;
    frame.linked_ = true;
}

void MoviePlayerJobs::fail(const std::shared_ptr<FinishJob>& job,
                           const std::exception_ptr& error) noexcept {
    try { job->result.set_exception(error); } catch (...) {}
    job->state.finish();
}

void MoviePlayerJobs::cancelled(const std::shared_ptr<FinishJob>& job) noexcept {
    try {
        job->result.set_exception(std::make_exception_ptr(HandlerError(
            "movieplayer_finish_cancelled", "MoviePlayer finish request was cancelled before execution")));
    } catch (...) {}
}

void MoviePlayerJobs::fail_wait_jobs(const std::string& wait_id) noexcept {
    try {
        const auto error = std::make_exception_ptr(HandlerError(
            "movieplayer_wait_changed", "The requested MoviePlayer wait is no longer active"));
        for (;;) {
            std::shared_ptr<FinishJob> job;
            {
                std::lock_guard lock(mutex_);
                const auto found = std::find_if(jobs_.begin(), jobs_.end(), [&](const auto& item) {
                    return item->expected_wait_id == wait_id;
                });
                if (found == jobs_.end()) return;
                job = *found;
                jobs_.erase(found);
                if (!job->state.begin()) continue;
            }
            fail(job, error);
        }
    } catch (...) {}
}

void MoviePlayerJobs::leave(WaitFrame& frame,
                           std::optional<std::uint64_t> returned_ms) noexcept {
    if (active_frame_ == &frame) active_frame_ = frame.previous_;
    else {
        for (auto* current = active_frame_; current; current = current->previous_) {
            if (current->previous_ == &frame) { current->previous_ = frame.previous_; break; }
        }
    }
    frame.linked_ = false;
    try {
        std::lock_guard lock(mutex_);
        auto next = cache_;
        if (returned_ms) {
            next.last_returned_wait_id = frame.context_.wait_id;
            next.last_returned_ms = *returned_ms;
        }
        if (active_frame_ && &active_frame_->owner_ == this) {
            next.context = active_frame_->context_;
            next.active_wait = true;
            next.wait_returned = false;
        } else {
            next.context = frame.context_;
            next.active_wait = false;
            next.wait_returned = returned_ms.has_value();
        }
        cache_ = std::move(next);
    } catch (...) {}
    fail_wait_jobs(frame.context_.wait_id);
}

void MoviePlayerJobs::publish_sample(WaitFrame& frame) {
    if (active_frame_ != &frame || !frame.linked_) return;
    std::lock_guard lock(mutex_);
    auto next = cache_;
    next.context = frame.context_;
    next.active_wait = true;
    next.wait_returned = false;
    cache_ = std::move(next);
}

MoviePlayerJobs::Json MoviePlayerJobs::cached_status() const {
    Cache copy;
    { std::lock_guard lock(mutex_); copy = cache_; }
    Json result{{"source", "native_movieplayer_cache"}, {"active_wait", copy.active_wait},
                {"wait_returned", copy.wait_returned}, {"wait_id", nullptr}, {"thread_id", nullptr},
                {"caller_allow_engine_tick", nullptr}, {"entered_ms", nullptr},
                {"last_sample_ms", nullptr}, {"sample_wait_id", nullptr},
                {"loading_finished", nullptr}, {"movie_playing", nullptr},
                {"finish_call_completed", false}, {"sample_error", nullptr},
                {"last_returned_wait_id", nullptr}, {"last_returned_ms", nullptr}};
    if (copy.context) {
        const auto& context = *copy.context;
        result["wait_id"] = context.wait_id;
        result["thread_id"] = context.thread_id;
        result["caller_allow_engine_tick"] = context.allow_engine_tick;
        result["entered_ms"] = context.entered_ms;
        result["finish_call_completed"] = context.finish_call_completed;
        result["sample_error"] = context.sample_error;
        if (context.sampled_ms) {
            result["last_sample_ms"] = *context.sampled_ms;
            result["sample_wait_id"] = context.wait_id;
        }
        if (context.loading_finished) result["loading_finished"] = *context.loading_finished;
        if (context.movie_playing) result["movie_playing"] = *context.movie_playing;
    }
    if (copy.last_returned_wait_id) result["last_returned_wait_id"] = *copy.last_returned_wait_id;
    if (copy.last_returned_ms) result["last_returned_ms"] = *copy.last_returned_ms;
    return result;
}

std::shared_ptr<MoviePlayerJobs::FinishJob> MoviePlayerJobs::request_finish(
    const std::string& session_id, const std::string& expected_wait_id) {
    if (session_id.empty() || expected_wait_id.empty())
        throw HandlerError("invalid_movieplayer_finish_params", "Session and expected_wait_id must be nonempty");
    auto job = std::make_shared<FinishJob>(session_id, expected_wait_id);
    std::lock_guard lock(mutex_);
    if (!cache_.active_wait || !cache_.context || cache_.context->wait_id != expected_wait_id)
        throw HandlerError("movieplayer_wait_changed", "The requested MoviePlayer wait is not active");
    jobs_.push_back(job);
    return job;
}

bool MoviePlayerJobs::cancel_finish(const std::shared_ptr<FinishJob>& job) {
    if (!job || !job->state.cancel()) return false;
    {
        std::lock_guard lock(mutex_);
        jobs_.erase(std::remove(jobs_.begin(), jobs_.end(), job), jobs_.end());
    }
    cancelled(job);
    return true;
}

void MoviePlayerJobs::disconnect(const std::string& session_id) {
    for (;;) {
        std::shared_ptr<FinishJob> job;
        {
            std::lock_guard lock(mutex_);
            const auto found = std::find_if(jobs_.begin(), jobs_.end(), [&](const auto& item) {
                return item->session_id == session_id;
            });
            if (found == jobs_.end()) return;
            job = *found;
            jobs_.erase(found);
            if (!job->state.cancel()) continue;
        }
        cancelled(job);
    }
}

void MoviePlayerJobs::sample_and_service(std::uint32_t thread_id, std::uint64_t now_ms,
                                        const ReadFlag& loading_finished,
                                        const ReadFlag& movie_playing,
                                        const StopMovie& stop_movie) noexcept {
    auto* frame = active_frame_;
    if (!frame || &frame->owner_ != this || !frame->linked_
        || frame->context_.thread_id != thread_id || !frame->player_ || servicing_) return;
    servicing_ = true;
    struct ServiceScope { ~ServiceScope() { MoviePlayerJobs::servicing_ = false; } } scope;
    std::exception_ptr read_error;
    try {
        const bool finished = loading_finished(frame->player_);
        const bool playing = movie_playing(frame->player_);
        frame->context_.loading_finished = finished;
        frame->context_.movie_playing = playing;
        frame->context_.sample_error = nullptr;
    } catch (...) {
        read_error = std::current_exception();
        frame->context_.loading_finished.reset();
        frame->context_.movie_playing.reset();
        try { frame->context_.sample_error = sample_error(read_error); } catch (...) {}
    }
    frame->context_.sampled_ms = now_ms;
    try { publish_sample(*frame); } catch (...) {}

    for (;;) {
        std::shared_ptr<FinishJob> job;
        try {
            {
                std::lock_guard lock(mutex_);
                if (jobs_.empty()) return;
                job = jobs_.front();
                jobs_.pop_front();
                if (!job->state.begin()) continue;
            }
            if (active_frame_ != frame || !frame->linked_
                || job->expected_wait_id != frame->context_.wait_id) {
                fail(job, std::make_exception_ptr(HandlerError(
                    "movieplayer_wait_changed", "The requested MoviePlayer wait is no longer current")));
                continue;
            }
            if (read_error) { fail(job, callback_error(read_error, false)); continue; }
            bool stop_started = false;
            try {
                const bool finished = loading_finished(frame->player_);
                frame->context_.loading_finished = finished;
                publish_sample(*frame);
                if (!finished)
                    throw HandlerError("movieplayer_loading_not_finished", "MoviePlayer loading has not finished");
                stop_started = true;
                stop_movie(frame->player_);
                frame->context_.finish_call_completed = true;
                publish_sample(*frame);
                job->result.set_value({{"source", "live_native_movieplayer"},
                    {"wait_id", job->expected_wait_id}, {"thread_id", thread_id},
                    {"loading_finished", true}, {"finish_call_completed", true}});
                job->state.finish();
            } catch (...) { fail(job, callback_error(std::current_exception(), stop_started)); }
        } catch (...) {
            if (job && job->state.get() == JobState::running) fail(job, std::current_exception());
            return;
        }
    }
}

} // namespace g1r_mcp
