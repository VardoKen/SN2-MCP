#pragma once

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif

#include <winsock2.h>

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>

#include <nlohmann/json.hpp>

namespace sn2_mcp {

inline constexpr std::uint16_t DefaultPort = 27223;
inline constexpr std::uint32_t MaxFrameBytes = std::numeric_limits<std::uint32_t>::max();

class HandlerError : public std::runtime_error {
public:
    HandlerError(std::string code, std::string message,
                 nlohmann::json details={{"executed",false},{"may_have_executed",false},{"do_not_retry",false}})
        : std::runtime_error(std::move(message)), code_(std::move(code)), details_(std::move(details)) {}
    const std::string& code() const noexcept { return code_; }
    const nlohmann::json& details() const noexcept { return details_; }

private:
    std::string code_;
    nlohmann::json details_;
};

// The handler runs on the transport worker thread; game calls are
// prohibited here. The caller sends work to the game queue and bounds
// the result wait. stop() waits for an already entered handler.
class Transport {
public:
    using Handler = std::function<nlohmann::json(const nlohmann::json&)>;
    using SessionClosed = std::function<void(const std::string&)>;

    explicit Transport(Handler handler, std::uint16_t port = DefaultPort,
                       std::chrono::milliseconds io_timeout = std::chrono::milliseconds{5000},
                       SessionClosed session_closed = {});
    ~Transport();
    Transport(const Transport&) = delete;
    Transport& operator=(const Transport&) = delete;

    bool start();
    void stop();
    nlohmann::json status() const;

private:
    void run();
    void serve(SOCKET client, const std::string& session);
    bool read_exact(SOCKET socket, char* bytes, std::size_t length,
                    std::chrono::steady_clock::time_point deadline);
    bool write_exact(SOCKET socket, const char* bytes, std::size_t length,
                     std::chrono::steady_clock::time_point deadline);
    bool wait_socket(SOCKET socket, bool writing,
                     std::chrono::steady_clock::time_point deadline);
    bool send_json(SOCKET client, const nlohmann::json& response);
    void set_error(std::string code, std::string message);
    std::string new_session();

    Handler handler_;
    SessionClosed session_closed_;
    std::uint16_t requested_port_;
    std::chrono::milliseconds io_timeout_;
    mutable std::mutex state_mutex_;
    std::mutex lifecycle_mutex_;
    SOCKET listener_{INVALID_SOCKET};
    SOCKET active_client_{INVALID_SOCKET};
    std::uint16_t bound_port_{0};
    bool winsock_started_{false};
    std::string session_;
    std::string last_error_code_;
    std::string last_error_message_;
    std::uint64_t session_counter_{0};
    std::atomic_bool stop_requested_{true};
    std::atomic_bool running_{false};
    std::thread worker_;
};

} // namespace sn2_mcp
