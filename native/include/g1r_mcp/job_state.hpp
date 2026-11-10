#pragma once
#include <atomic>

namespace g1r_mcp {
// Only queued jobs can become running or cancelled. Cancelled jobs never execute.
class JobState {
public:
    enum State { queued, running, cancelled, completed };
    bool begin() { int expected=queued;return state_.compare_exchange_strong(expected,running); }
    bool cancel() { int expected=queued;return state_.compare_exchange_strong(expected,cancelled); }
    void finish() { state_.store(completed); }
    State get() const { return static_cast<State>(state_.load()); }
private:
    std::atomic<int> state_{queued};
};
}
