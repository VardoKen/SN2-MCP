#include <sn2_mcp/transport.hpp>

#include <Windows.h>
#include <ws2tcpip.h>

#include <algorithm>
#include <array>
#include <climits>
#include <limits>
#include <unordered_set>
#include <utility>

namespace sn2_mcp {
namespace {

nlohmann::json error_response(const nlohmann::json& id, const std::string& session,
                             std::string code, std::string message,
                             nlohmann::json details={{"executed",false},{"may_have_executed",false},{"do_not_retry",false}}) {
    return {{"protocol", 1}, {"type", "error"}, {"id", id}, {"session_id", session},
            {"error", {{"code", std::move(code)}, {"message", std::move(message)}, {"details",std::move(details)}}}};
}

bool nonblocking(SOCKET socket) {
    u_long enabled = 1;
    return ioctlsocket(socket, FIONBIO, &enabled) == 0;
}

} // namespace

Transport::Transport(Handler handler, std::uint16_t port, std::chrono::milliseconds io_timeout,
                     SessionClosed session_closed)
    : handler_(std::move(handler)), session_closed_(std::move(session_closed)),
      requested_port_(port), io_timeout_(io_timeout) {
    if (!handler_) throw std::invalid_argument("The transport handler is missing");
    if (io_timeout_.count() <= 0)
        throw std::invalid_argument("Transport timeout must be positive");
}

Transport::~Transport() { stop(); }

bool Transport::start() {
    std::lock_guard lifecycle(lifecycle_mutex_);
    if (running_.load()) return true;
    if (worker_.joinable()) worker_.join();
    if (winsock_started_) {
        WSACleanup();
        winsock_started_ = false;
    }
    WSADATA data{};
    if (WSAStartup(MAKEWORD(2, 2), &data) != 0) {
        set_error("winsock_start_failed", "Could not initialize Winsock");
        return false;
    }
    winsock_started_ = true;
    SOCKET listener = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listener == INVALID_SOCKET) {
        set_error("socket_create_failed", "Could not create the local socket");
        WSACleanup();
        winsock_started_ = false;
        return false;
    }
    BOOL exclusive = TRUE;
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    address.sin_port = htons(requested_port_);
    if (setsockopt(listener, SOL_SOCKET, SO_EXCLUSIVEADDRUSE,
                   reinterpret_cast<const char*>(&exclusive), sizeof(exclusive)) != 0
        || bind(listener, reinterpret_cast<const sockaddr*>(&address), sizeof(address)) != 0
        || listen(listener, 1) != 0 || !nonblocking(listener)) {
        set_error("listen_failed", "Could not open the local transport port");
        closesocket(listener);
        WSACleanup();
        winsock_started_ = false;
        return false;
    }
    int address_size = sizeof(address);
    if (getsockname(listener, reinterpret_cast<sockaddr*>(&address), &address_size) != 0) {
        set_error("socket_address_failed", "Could not identify the local port");
        closesocket(listener);
        WSACleanup();
        winsock_started_ = false;
        return false;
    }
    {
        std::lock_guard state(state_mutex_);
        listener_ = listener;
        bound_port_ = ntohs(address.sin_port);
        last_error_code_.clear();
        last_error_message_.clear();
    }
    stop_requested_.store(false);
    running_.store(true);
    try {
        worker_ = std::thread(&Transport::run, this);
    } catch (...) {
        stop_requested_.store(true);
        running_.store(false);
        {
            std::lock_guard state(state_mutex_);
            listener_ = INVALID_SOCKET;
            closesocket(listener);
        }
        WSACleanup();
        winsock_started_ = false;
        set_error("worker_start_failed", "Could not create the transport thread");
        return false;
    }
    return true;
}

void Transport::stop() {
    std::lock_guard lifecycle(lifecycle_mutex_);
    stop_requested_.store(true);
    {
        // External threads only call shutdown. The owning thread calls closesocket
        // after I/O exits, so the socket handle cannot be reused during cancellation.
        std::lock_guard state(state_mutex_);
        if (active_client_ != INVALID_SOCKET) shutdown(active_client_, SD_BOTH);
    }
    if (worker_.joinable()) {
        if (worker_.get_id() == std::this_thread::get_id()) return;
        worker_.join();
    }
    if (winsock_started_) {
        WSACleanup();
        winsock_started_ = false;
    }
}

nlohmann::json Transport::status() const {
    std::lock_guard state(state_mutex_);
    return {{"running", running_.load()}, {"listening", listener_ != INVALID_SOCKET},
            {"connected", active_client_ != INVALID_SOCKET}, {"host", "127.0.0.1"},
            {"port", bound_port_}, {"session_id", session_.empty() ? nlohmann::json(nullptr) : nlohmann::json(session_)},
            {"max_frame_bytes", MaxFrameBytes}, {"max_session_requests", nullptr},
            {"last_error", {{"code", last_error_code_}, {"message", last_error_message_}}}};
}

void Transport::set_error(std::string code, std::string message) {
    std::lock_guard state(state_mutex_);
    last_error_code_ = std::move(code);
    last_error_message_ = std::move(message);
}

std::string Transport::new_session() {
    FILETIME time{};
    GetSystemTimeAsFileTime(&time);
    const auto stamp = (static_cast<std::uint64_t>(time.dwHighDateTime) << 32U) | time.dwLowDateTime;
    return std::to_string(GetCurrentProcessId()) + "-" + std::to_string(stamp) + "-" + std::to_string(++session_counter_);
}

bool Transport::wait_socket(SOCKET socket, bool writing, std::chrono::steady_clock::time_point deadline) {
    while (!stop_requested_.load()) {
        const auto remaining = std::chrono::duration_cast<std::chrono::milliseconds>(deadline - std::chrono::steady_clock::now());
        if (remaining.count() <= 0) {
            set_error("io_timeout", "Frame exchange timed out");
            return false;
        }
        const auto slice = std::min<std::int64_t>(remaining.count(), 100);
        timeval timeout{0, static_cast<long>(slice * 1000)};
        fd_set available;
        FD_ZERO(&available);
        FD_SET(socket, &available);
        const int result = select(0, writing ? nullptr : &available, writing ? &available : nullptr, nullptr, &timeout);
        if (result > 0) return true;
        if (result == SOCKET_ERROR) {
            if (!stop_requested_.load()) set_error("socket_wait_failed", "Failed while waiting for the local socket");
            return false;
        }
    }
    return false;
}

bool Transport::read_exact(SOCKET socket, char* bytes, std::size_t length,
                           std::chrono::steady_clock::time_point deadline) {
    std::size_t offset = 0;
    while (offset < length && !stop_requested_.load()) {
        if (!wait_socket(socket, false, deadline)) return false;
        const int amount = recv(socket, bytes + offset, static_cast<int>(std::min<std::size_t>(length - offset, INT_MAX)), 0);
        if (amount > 0) offset += static_cast<std::size_t>(amount);
        else if (amount == 0) return false;
        else if (WSAGetLastError() != WSAEWOULDBLOCK) {
            if (!stop_requested_.load()) set_error("socket_read_failed", "Failed to read the local socket");
            return false;
        }
    }
    return offset == length;
}

bool Transport::write_exact(SOCKET socket, const char* bytes, std::size_t length,
                            std::chrono::steady_clock::time_point deadline) {
    std::size_t offset = 0;
    while (offset < length && !stop_requested_.load()) {
        if (!wait_socket(socket, true, deadline)) return false;
        const int amount = send(socket, bytes + offset, static_cast<int>(std::min<std::size_t>(length - offset, INT_MAX)), 0);
        if (amount > 0) offset += static_cast<std::size_t>(amount);
        else if (amount == 0) return false;
        else if (WSAGetLastError() != WSAEWOULDBLOCK) {
            if (!stop_requested_.load()) set_error("socket_write_failed", "Failed to write the local socket");
            return false;
        }
    }
    return offset == length;
}

bool Transport::send_json(SOCKET client, const nlohmann::json& response) {
    std::string payload;
    try { payload = response.dump(); }
    catch (...) {
        set_error("response_encoding_failed", "The response cannot be encoded as UTF-8 JSON");
        return false;
    }
    if (payload.empty() || payload.size() > MaxFrameBytes) {
        set_error("response_too_large", "The response exceeds the frame size limit");
        return false;
    }
    const auto length = static_cast<std::uint32_t>(payload.size());
    const std::array<char, 4> header{static_cast<char>(length >> 24U), static_cast<char>(length >> 16U),
                                    static_cast<char>(length >> 8U), static_cast<char>(length)};
    const auto deadline = std::chrono::steady_clock::now() + io_timeout_;
    return write_exact(client, header.data(), header.size(), deadline)
        && write_exact(client, payload.data(), payload.size(), deadline);
}

void Transport::serve(SOCKET client, const std::string& session) {
    std::unordered_set<std::string> seen;
    while (!stop_requested_.load()) {
        std::array<unsigned char, 4> header{};
        // MCP client reasoning time is not a frame timeout. Until the first
        // byte arrives, the connection may remain idle; cancellation is checked
        // every 100 ms. The shared frame deadline applies after the first byte.
        bool started = false;
        while (!stop_requested_.load() && !started) {
            fd_set ready;
            FD_ZERO(&ready);
            FD_SET(client, &ready);
            timeval timeout{0, 100000};
            const int available = select(0, &ready, nullptr, nullptr, &timeout);
            if (available == 0) continue;
            if (available == SOCKET_ERROR) return;
            const int amount = recv(client, reinterpret_cast<char*>(header.data()), 1, 0);
            if (amount == 1) started = true;
            else if (amount == 0 || WSAGetLastError() != WSAEWOULDBLOCK) return;
        }
        if (!started) return;
        const auto deadline = std::chrono::steady_clock::now() + io_timeout_;
        if (!read_exact(client, reinterpret_cast<char*>(header.data()) + 1, header.size() - 1, deadline)) return;
        const auto length = (static_cast<std::uint32_t>(header[0]) << 24U)
                          | (static_cast<std::uint32_t>(header[1]) << 16U)
                          | (static_cast<std::uint32_t>(header[2]) << 8U) | header[3];
        if (length == 0 || length > MaxFrameBytes) {
            send_json(client, error_response(nullptr, session, "invalid_frame_length", "Invalid frame size"));
            return;
        }
        std::string payload(length, '\0');
        if (!read_exact(client, payload.data(), payload.size(), deadline)) return;
        nlohmann::json request;
        try {
            request = nlohmann::json::parse(payload);
        }
        catch (...) {
            send_json(client, error_response(nullptr, session, "invalid_json", "The frame does not contain valid JSON"));
            return;
        }
        nlohmann::json id = nullptr;
        if (request.is_object() && request.contains("id") && request["id"].is_string()
            && !request["id"].get_ref<const std::string&>().empty()) id = request["id"];
        if (!request.is_object() || id.is_null() || !request.contains("protocol")
            || !request["protocol"].is_number_integer() || request["protocol"] != 1
            || !request.contains("op") || !request["op"].is_string()
            || request["op"].get_ref<const std::string&>().empty()
            || !request.contains("params") || !request["params"].is_object()) {
            send_json(client, error_response(id, session, "invalid_request", "Invalid request schema or protocol version"));
            return;
        }
        const auto request_id = id.get<std::string>();
        if (seen.contains(request_id)) {
            if (!send_json(client, error_response(id, session, "duplicate_request_id", "The request identifier was already used in this connection"))) return;
            continue;
        }
        seen.insert(request_id);
        request["session_id"] = session;
        nlohmann::json response;
        try {
            auto result = handler_(request);
            if (!result.is_object()) throw HandlerError("invalid_handler_result", "The handler must return a JSON object");
            response = {{"protocol", 1}, {"type", "response"}, {"id", id}, {"session_id", session},
                        {"result", std::move(result)}};
            if (response.dump().size() > MaxFrameBytes) {
                const bool executed=response.at("result").value("executed",false);
                response = error_response(id, session, "response_too_large", "The handler response exceeds the frame size limit",
                    {{"executed",executed},{"may_have_executed",executed},{"do_not_retry",executed}});
            }
        } catch (const HandlerError& error) {
            response = error_response(id, session, error.code(), error.what(),error.details());
        } catch (...) {
            response = error_response(id, session, "native_handler_error", "The handler failed",
                {{"executed",false},{"may_have_executed",true},{"do_not_retry",true}});
        }
        if (!send_json(client, response)) return;
    }
}

void Transport::run() {
    SOCKET listener;
    {
        std::lock_guard state(state_mutex_);
        listener = listener_;
    }
    try {
        while (!stop_requested_.load()) {
            fd_set ready;
            FD_ZERO(&ready);
            FD_SET(listener, &ready);
            timeval timeout{0, 100000};
            const int available = select(0, &ready, nullptr, nullptr, &timeout);
            if (available == 0) continue;
            if (available == SOCKET_ERROR) {
                if (!stop_requested_.load()) set_error("listener_wait_failed", "Failed while waiting for a local client");
                break;
            }
            sockaddr_in address{};
            int address_size = sizeof(address);
            SOCKET client = accept(listener, reinterpret_cast<sockaddr*>(&address), &address_size);
            if (client == INVALID_SOCKET) {
                if (WSAGetLastError() == WSAEWOULDBLOCK) continue;
                if (!stop_requested_.load()) set_error("accept_failed", "Could not accept the local client");
                break;
            }
            BOOL no_delay = TRUE;
            if (!nonblocking(client)
                || setsockopt(client, IPPROTO_TCP, TCP_NODELAY,
                              reinterpret_cast<const char*>(&no_delay), sizeof(no_delay)) != 0) {
                closesocket(client);
                continue;
            }
            const auto session = new_session();
            {
                std::lock_guard state(state_mutex_);
                active_client_ = client;
                session_ = session;
            }
            try { serve(client, session); }
            catch (...) { set_error("transport_worker_error", "Client service failed"); }
            if (session_closed_) {
                try { session_closed_(session); }
                catch (...) { set_error("session_cleanup_failed", "Could not notify the session disconnect handler"); }
            }
            {
                std::lock_guard state(state_mutex_);
                active_client_ = INVALID_SOCKET;
                session_.clear();
                shutdown(client, SD_BOTH);
                closesocket(client);
            }
        }
    } catch (...) {
        set_error("transport_worker_error", "The transport thread failed");
    }
    {
        std::lock_guard state(state_mutex_);
        if (active_client_ != INVALID_SOCKET) {
            shutdown(active_client_, SD_BOTH);
            closesocket(active_client_);
            active_client_ = INVALID_SOCKET;
        }
        session_.clear();
        closesocket(listener);
        listener_ = INVALID_SOCKET;
    }
    running_.store(false);
}

} // namespace sn2_mcp
