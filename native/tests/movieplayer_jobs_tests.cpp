#include <g1r_mcp/movieplayer_jobs.hpp>
#include <g1r_mcp/transport.hpp>
#include <atomic>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <vector>

using g1r_mcp::HandlerError;
using g1r_mcp::JobState;
using g1r_mcp::MoviePlayerJobs;
using nlohmann::json;
namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template<class Operation> void rejects(const char* code, Operation operation) {
    try { operation(); }
    catch (const HandlerError& error) {
        require(error.code() == code, "Unexpected MoviePlayer error code");
        return;
    }
    throw std::runtime_error("Expected a MoviePlayer error");
}
struct Callbacks {
    bool finished{true};
    bool playing{true};
    int reads{}, playing_reads{}, stops{};
    std::uintptr_t expected{101};
    bool read_finished(std::uintptr_t player) {
        require(player == expected, "Wrong Wait player was read");
        ++reads;
        return finished;
    }
    bool read_playing(std::uintptr_t player) {
        require(player == expected, "Wrong Wait player was read");
        ++playing_reads;
        return playing;
    }
    void stop(std::uintptr_t player) {
        require(player == expected, "Wrong Wait player was stopped");
        ++stops;
    }
    void service(MoviePlayerJobs& model, std::uint32_t tid, std::uint64_t now) {
        model.sample_and_service(tid, now,
            [&](auto player) { return read_finished(player); },
            [&](auto player) { return read_playing(player); },
            [&](auto player) { stop(player); });
    }
};

void cached_status_and_execution_context() {
    MoviePlayerJobs model;
    Callbacks calls;
    const auto empty = model.cached_status();
    require(empty.at("active_wait") == false && empty.at("wait_id").is_null()
            && empty.at("loading_finished").is_null(), "Initial cache claims a live sample");
    calls.service(model, 7, 1);
    require(calls.reads == 0 && calls.stops == 0, "Outside Wait executed game callbacks");
    rejects("movieplayer_wait_changed", [&] { model.request_finish("session", "wait-1"); });
    MoviePlayerJobs::WaitFrame frame(model, 101, 7, false, 10);
    const auto active = model.cached_status();
    require(active.at("active_wait") == true && active.at("wait_returned") == false
            && active.at("thread_id") == 7 && active.at("caller_allow_engine_tick") == false
            && active.at("last_sample_ms").is_null(), "Wait entry incorrectly claims a sample");
    auto job = model.request_finish("session", frame.wait_id());
    auto future = job->result.get_future();
    calls.service(model, 8, 11);
    std::thread unrelated([&] { calls.service(model, 7, 12); });
    unrelated.join();
    require(calls.reads == 0 && job->state.get() == JobState::queued,
            "Wrong TID or a different actual thread executed a request");
    require(model.cancel_finish(job), "Queued request could not be cancelled");
    rejects("movieplayer_finish_cancelled", [&] { future.get(); });
    calls.service(model, 7, 13);
    const auto sampled = model.cached_status();
    require(calls.reads == 1 && calls.playing_reads == 1 && calls.stops == 0
            && sampled.at("last_sample_ms") == 13 && sampled.at("sample_wait_id") == frame.wait_id()
            && sampled.at("loading_finished") == true && sampled.at("movie_playing") == true,
            "Platform sample is incomplete or cancelled finish executed");
    for (int i = 0; i < 10; ++i) require(model.cached_status() == sampled, "Cache-only read mutated its sample");
    require(calls.reads == 1, "Reading status executed a game callback");
    frame.returned(14);
    calls.service(model, 7, 15);
    const auto returned = model.cached_status();
    require(calls.reads == 1 && returned.at("active_wait") == false && returned.at("wait_returned") == true
            && returned.at("last_returned_wait_id") == frame.wait_id()
            && returned.at("last_returned_ms") == 14 && returned.at("last_sample_ms") == 13,
            "Returned Wait was serviced or its sample was misrepresented");
}

void nested_wait_and_abnormal_exit() {
    MoviePlayerJobs model;
    Callbacks calls;
    MoviePlayerJobs::WaitFrame outer(model, 101, 7, true, 10);
    calls.service(model, 7, 11);
    auto outer_job = model.request_finish("session", outer.wait_id());
    auto future = outer_job->result.get_future();
    std::string inner_id;
    {
        MoviePlayerJobs::WaitFrame inner(model, 202, 7, false, 12);
        inner_id = inner.wait_id();
        calls.expected = 202;
        calls.service(model, 7, 13);
        rejects("movieplayer_wait_changed", [&] { future.get(); });
        require(calls.stops == 0 && model.cached_status().at("wait_id") == inner_id,
                "A stale outer request stopped the inner Wait");
        inner.returned(14);
    }
    const auto restored = model.cached_status();
    require(restored.at("active_wait") == true && restored.at("wait_id") == outer.wait_id()
            && restored.at("caller_allow_engine_tick") == true && restored.at("last_sample_ms") == 11
            && restored.at("sample_wait_id") == outer.wait_id()
            && restored.at("last_returned_wait_id") == inner_id && restored.at("last_returned_ms") == 14,
            "Nested exit lost the outer context or mixed sample identities");
    {
        MoviePlayerJobs::WaitFrame abandoned(model, 303, 7, false, 15);
    }
    require(model.cached_status().at("wait_id") == outer.wait_id()
            && model.cached_status().at("last_returned_wait_id") == inner_id,
            "Abnormal inner cleanup falsely reported a normal Wait return");
    outer.returned(16);
    {
        MoviePlayerJobs::WaitFrame abandoned(model, 101, 7, false, 17);
    }
    require(model.cached_status().at("active_wait") == false
            && model.cached_status().at("wait_returned") == false
            && model.cached_status().at("last_returned_wait_id") == outer.wait_id(),
            "Abnormal cleanup fabricated a returned marker");
}

void explicit_finish_and_fresh_validation() {
    MoviePlayerJobs model;
    Callbacks calls;
    MoviePlayerJobs::WaitFrame frame(model, 101, 7, true, 10);
    calls.service(model, 7, 11);
    auto premature = model.request_finish("session", frame.wait_id());
    auto premature_future = premature->result.get_future();
    calls.finished = false;
    calls.service(model, 7, 12);
    rejects("movieplayer_loading_not_finished", [&] { premature_future.get(); });
    require(calls.stops == 0 && premature->state.get() == JobState::completed
            && model.cached_status().at("loading_finished") == false,
            "A cached true loading flag bypassed fresh finish validation");
    auto finish = model.request_finish("session", frame.wait_id());
    auto future = finish->result.get_future();
    calls.finished = true;
    calls.service(model, 7, 13);
    const auto reply = future.get();
    const auto cache = model.cached_status();
    require(calls.stops == 1 && reply.at("finish_call_completed") == true
            && reply.at("wait_id") == frame.wait_id() && reply.at("loading_finished") == true
            && !reply.contains("wait_returned") && !reply.contains("ordinary_tick_ready")
            && cache.at("active_wait") == true && cache.at("wait_returned") == false,
            "StopMovie completion prematurely claimed Wait or Tick readiness");
    require(!model.cancel_finish(finish), "Completed finish was reported cancelled");
    auto expired = model.request_finish("session", frame.wait_id());
    auto expired_future = expired->result.get_future();
    frame.returned(14);
    rejects("movieplayer_wait_changed", [&] { expired_future.get(); });
    MoviePlayerJobs::WaitFrame next(model, 101, 7, true, 15);
    rejects("movieplayer_wait_changed", [&] { model.request_finish("session", frame.wait_id()); });
    calls.service(model, 7, 16);
    require(calls.stops == 1 && next.wait_id() != frame.wait_id(), "A prior finish reached a later Wait");
    next.returned(17);
}

void cancellation_sessions_and_late_cancel() {
    MoviePlayerJobs model;
    Callbacks calls;
    MoviePlayerJobs::WaitFrame frame(model, 101, 7, true, 1);
    auto one = model.request_finish("session-one", frame.wait_id());
    auto two = model.request_finish("session-two", frame.wait_id());
    auto first = one->result.get_future();
    auto second = two->result.get_future();
    model.disconnect("session-one");
    rejects("movieplayer_finish_cancelled", [&] { first.get(); });
    require(two->state.get() == JobState::queued, "Disconnect cancelled another session");
    std::atomic<bool> entered{}, cancelled{};
    std::thread cancel([&] {
        while (!entered.load()) std::this_thread::yield();
        cancelled.store(model.cancel_finish(two));
    });
    model.sample_and_service(7, 2, [&](auto player) { return calls.read_finished(player); },
        [&](auto player) { return calls.read_playing(player); }, [&](auto player) {
            calls.stop(player);
            entered.store(true);
            cancel.join();
        });
    if (cancel.joinable()) cancel.join();
    require(!cancelled.load() && second.get().at("finish_call_completed") == true
            && calls.stops == 1, "Late cancellation hid an entered StopMovie call");
    require(!model.cancel_finish(one), "Repeated cancellation changed JobState");
    frame.returned(3);
    rejects("invalid_movieplayer_finish_params", [&] { model.request_finish("", frame.wait_id()); });
    rejects("invalid_movieplayer_finish_params", [&] { model.request_finish("session", ""); });
}

void reentrancy_and_callback_failures() {
    MoviePlayerJobs model;
    Callbacks calls;
    MoviePlayerJobs::WaitFrame frame(model, 101, 7, true, 1);
    auto first = model.request_finish("session", frame.wait_id());
    auto second = model.request_finish("session", frame.wait_id());
    auto first_future = first->result.get_future();
    auto second_future = second->result.get_future();
    int nested_callbacks = 0;
    model.sample_and_service(7, 2, [&](auto player) { return calls.read_finished(player); },
        [&](auto player) { return calls.read_playing(player); }, [&](auto player) {
            calls.stop(player);
            MoviePlayerJobs::WaitFrame inner(model, 202, 7, false, 3);
            model.sample_and_service(7, 4, [&](auto) { ++nested_callbacks; return true; },
                [&](auto) { ++nested_callbacks; return true; }, [&](auto) { ++nested_callbacks; });
            inner.returned(5);
        });
    require(first_future.get().at("finish_call_completed") == true
            && second_future.get().at("finish_call_completed") == true
            && calls.stops == 2 && nested_callbacks == 0,
            "Reentrant pump replayed a job or entered native callbacks recursively");
    const auto sample_failure = model.request_finish("session", frame.wait_id());
    auto sample_future = sample_failure->result.get_future();
    model.sample_and_service(7, 6, [&](auto) -> bool {
        throw HandlerError("sample_fault", "Sample failed before StopMovie");
    }, [&](auto) { return true; }, [&](auto) { ++calls.stops; });
    rejects("sample_fault", [&] { sample_future.get(); });
    require(model.cached_status().at("loading_finished").is_null()
            && model.cached_status().at("movie_playing").is_null()
            && model.cached_status().at("sample_error").at("code") == "sample_fault"
            && calls.stops == 2, "Failed sample left a usable true cache or executed StopMovie");
    auto native_fault = model.request_finish("session", frame.wait_id());
    auto native_future = native_fault->result.get_future();
    model.sample_and_service(7, 7, [&](auto) { return true; }, [&](auto) { return true; }, [&](auto) {
        throw HandlerError("stop_fault", "StopMovie entered and faulted",
            {{"executed", true}, {"may_have_executed", true}, {"do_not_retry", true}, {"native_code", 123}});
    });
    try { native_future.get(); throw std::runtime_error("Expected native fault"); }
    catch (const HandlerError& error) {
        require(error.code() == "stop_fault" && error.details().at("executed") == true
                && error.details().at("native_code") == 123, "Native fault effect details were replaced");
    }
    auto uncertain = model.request_finish("session", frame.wait_id());
    auto uncertain_future = uncertain->result.get_future();
    model.sample_and_service(7, 8, [&](auto) { return true; }, [&](auto) { return true; }, [&](auto) {
        throw std::runtime_error("Unknown StopMovie failure");
    });
    try { uncertain_future.get(); throw std::runtime_error("Expected unknown fault"); }
    catch (const HandlerError& error) {
        require(error.code() == "movieplayer_finish_failed"
                && error.details().at("may_have_executed") == true
                && error.details().at("do_not_retry") == true, "Unknown StopMovie failure claims safe retry");
    }
    frame.returned(9);
}

void queue_follows_requests_without_a_cap() {
    MoviePlayerJobs model;
    Callbacks calls;
    MoviePlayerJobs::WaitFrame frame(model, 101, 7, true, 1);
    std::vector<std::future<json>> replies;
    for (int i = 0; i < 129; ++i)
        replies.push_back(model.request_finish("session", frame.wait_id())->result.get_future());
    calls.service(model, 7, 2);
    require(calls.stops == 129, "Typed queue imposed an unrequested count cap");
    for (auto& reply : replies) require(reply.get().at("finish_call_completed") == true, "An accepted job lost its result");
    frame.returned(3);
}
}

int main() {
    try {
        cached_status_and_execution_context();
        nested_wait_and_abnormal_exit();
        explicit_finish_and_fresh_validation();
        cancellation_sessions_and_late_cancel();
        reentrancy_and_callback_failures();
        queue_follows_requests_without_a_cap();
        std::cout << "MoviePlayer typed jobs, Wait contexts and cached samples passed\n";
        return 0;
    } catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}
