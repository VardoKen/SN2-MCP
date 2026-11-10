#include <sn2_mcp/transport.hpp>
#include <ws2tcpip.h>

#include <array>
#include <atomic>
#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

using namespace std::chrono_literals;
using sn2_mcp::Transport;
using nlohmann::json;

namespace {

void require(bool value, const char* message) { if (!value) throw std::runtime_error(message); }

struct Client {
    SOCKET socket{INVALID_SOCKET};
    explicit Client(std::uint16_t port) {
        socket = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
        require(socket != INVALID_SOCKET, "Не создан тестовый сокет");
        DWORD timeout = 2000;
        setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
        setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        address.sin_port = htons(port);
        require(connect(socket, reinterpret_cast<sockaddr*>(&address), sizeof(address)) == 0, "Тестовый клиент не подключился");
    }
    ~Client() { if (socket != INVALID_SOCKET) closesocket(socket); }
    Client(const Client&) = delete;
    void send_bytes(const std::string& bytes, bool fragmented = false) {
        std::size_t offset = 0;
        while (offset < bytes.size()) {
            int amount = send(socket, bytes.data() + offset,
                              static_cast<int>(fragmented ? 1 : bytes.size() - offset), 0);
            require(amount > 0, "Тестовый клиент не отправил данные");
            offset += amount;
        }
    }
    void send_json(const json& request, bool fragmented = false) {
        auto payload = request.dump();
        auto count = static_cast<std::uint32_t>(payload.size());
        std::string bytes{static_cast<char>(count >> 24), static_cast<char>(count >> 16),
                          static_cast<char>(count >> 8), static_cast<char>(count)};
        bytes += payload;
        send_bytes(bytes, fragmented);
    }
    void read_exact(char* bytes, std::size_t size) {
        std::size_t offset = 0;
        while (offset < size) {
            int amount = recv(socket, bytes + offset, static_cast<int>(size - offset), 0);
            require(amount > 0, "Тестовый ответ не получен целиком");
            offset += amount;
        }
    }
    json read_json() {
        std::array<unsigned char, 4> header{};
        read_exact(reinterpret_cast<char*>(header.data()), header.size());
        std::uint32_t size = (static_cast<std::uint32_t>(header[0]) << 24)
                           | (static_cast<std::uint32_t>(header[1]) << 16)
                           | (static_cast<std::uint32_t>(header[2]) << 8) | header[3];
        require(size > 0 && size <= sn2_mcp::MaxFrameBytes, "Недопустимый размер тестового ответа");
        std::string payload(size, '\0');
        read_exact(payload.data(), size);
        return json::parse(payload);
    }
    bool closed() {
        char value;
        return recv(socket, &value, 1, 0) <= 0;
    }
};

json request(std::string id, std::string op = "echo") {
    return {{"protocol", 1}, {"id", std::move(id)}, {"op", std::move(op)}, {"params", {{"text", "Привет"}}}};
}

std::uint16_t port(const Transport& transport) { return transport.status().at("port").get<std::uint16_t>(); }

void exchange_duplicate_and_reconnect() {
    std::atomic_int calls{0};
    std::atomic_int closed{0};
    std::string closed_session;
    Transport transport([&](const json& input) {
        ++calls;
        return json{{"text", input.at("params").at("text")}, {"session", input.at("session_id")}};
    }, 0, 5000ms, [&](const std::string& session) {
        closed_session=session;
        ++closed;
    });
    require(transport.start(), "Транспорт не запущен");
    std::string first_session;
    {
        Client client(port(transport));
        client.send_json(request("one"), true);
        auto response = client.read_json();
        require(response.at("type") == "response" && response.at("id") == "one", "Неверная корреляция ответа");
        require(response.at("result").at("text") == "Привет", "Не сохранён UTF-8 текст");
        first_session = response.at("session_id");
        client.send_json(request("one"));
        auto duplicate = client.read_json();
        require(duplicate.at("error").at("code") == "duplicate_request_id", "Повтор не отвергнут");
        require(calls == 1, "Обработчик повторно вызван");
        client.send_json(request("two"));
        require(client.read_json().at("type") == "response", "Соединение не сохранилось после повтора");
    }
    {
        Client client(port(transport));
        client.send_json(request("one"));
        auto response = client.read_json();
        require(response.at("session_id") != first_session, "Новый клиент получил прежнюю сессию");
        require(closed.load()==1 && closed_session==first_session,
                "Reconnect начался до уведомления cleanup прежнего владельца");
    }
    transport.stop();
    require(closed.load()==2,"Отключённые сессии не получили ровно одно уведомление cleanup");
    require(!transport.status().at("running").get<bool>(), "Транспорт не остановлен");
}

void request_errors_and_handler_errors() {
    std::atomic_int calls{0};
    Transport transport([&](const json& input) -> json {
        ++calls;
        if (input.at("op") == "reject") throw sn2_mcp::HandlerError("not_ready", "Мир не готов");
        if (input.at("op") == "throw") throw std::runtime_error("Скрытая ошибка");
        if (input.at("op") == "scalar") return 1;
        return {{"ok", true}};
    }, 0);
    require(transport.start(), "Транспорт ошибок не запущен");
    {
        Client client(port(transport));
        auto invalid = request("bad"); invalid["protocol"] = 2;
        client.send_json(invalid);
        require(client.read_json().at("error").at("code") == "invalid_request", "Версия не отвергнута");
        require(calls == 0, "Неверный запрос вызвал обработчик");
        require(client.closed(), "Неверный запрос не закрыл соединение");
    }
    {
        Client client(port(transport));
        for (const auto& [op, code] : {std::pair{"reject", "not_ready"}, {"throw", "native_handler_error"},
                                    {"scalar", "invalid_handler_result"}}) {
            client.send_json(request(op, op));
            require(client.read_json().at("error").at("code") == code, "Неверный код ошибки обработчика");
        }
        client.send_json(request("last"));
        require(client.read_json().at("type") == "response", "Ошибка обработчика сломала соединение");
    }
    {
        Client client(port(transport));
        client.send_bytes(std::string{"\0\0\0\1x", 5});
        require(client.read_json().at("error").at("code") == "invalid_json", "JSON не отвергнут");
    }
    {
        Client client(port(transport));
        client.send_bytes(std::string{"\0\0\0\0", 4});
        require(client.read_json().at("error").at("code") == "invalid_frame_length", "Пустой кадр не отвергнут");
    }
    transport.stop();
}

void execution_error_metadata() {
    Transport transport([](const json& envelope)->json {
        if(envelope.at("op")=="pre_event")
            throw sn2_mcp::HandlerError("invalid_object_argument","До вызова");
        throw sn2_mcp::HandlerError("game_result_error","После вызова",
            {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},{"cause_code","stale_handle"}});
    },0);
    require(transport.start(),"Транспорт метаданных эффекта не запущен");
    {
        Client client(port(transport));
        client.send_json(request("before","pre_event"));
        const auto before=client.read_json().at("error").at("details");
        require(before.at("executed")==false && before.at("may_have_executed")==false,
                "До-вызовный отказ получил возможный эффект");
        client.send_json(request("after","post_event"));
        const auto after=client.read_json().at("error").at("details");
        require(after.at("executed")==true && after.at("do_not_retry")==true
                && after.at("cause_code")=="stale_handle","Потеря результата скрыла исполнение");
    }
    transport.stop();
}

void large_request_and_result_preserve_effect_and_session() {
    constexpr std::size_t former_frame_limit=1024U*1024U;
    const std::string large_text=std::string(former_frame_limit+97U,'x')+"Привет";
    json nested={{"text",large_text}};
    for(int depth=0;depth<40;++depth)nested={{"nested",std::move(nested)}};
    auto large_request=request("large","large_action");
    large_request["params"]={{"payload",nested}};
    require(large_request.dump().size()>former_frame_limit,"Запрос должен превышать прежний предел 1 МиБ");
    std::atomic_int action_calls{0};
    Transport transport([&](const json& envelope)->json {
        if(envelope.at("op")=="large_action") {
            ++action_calls;
            return {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
                    {"result",envelope.at("params").at("payload")}};
        }
        return {{"ready",true}};
    },0);
    require(transport.start(),"Транспорт большого результата не запущен");
    {
        Client client(port(transport));
        client.send_json(large_request);
        const auto response=client.read_json();
        require(response.at("type")=="response" && response.at("id")=="large",
                "Большой запрос и результат не прошли транспорт");
        require(response.dump().size()>former_frame_limit && response.at("result").at("result")==nested,
                "Большой вложенный результат или его UTF-8 текст изменились");
        const auto& details=response.at("result");
        require(details.at("executed")==true && details.at("may_have_executed")==true
                && details.at("do_not_retry")==true,"Большой ответ скрыл выполненное действие");
        require(action_calls==1,"Большое действие было повторено");
        client.send_json(request("status","status"));
        const auto status=client.read_json();
        require(status.at("result").at("ready")==true && status.at("session_id")==response.at("session_id"),
                "Большой результат изменил сессию или сломал следующее чтение");
        require(action_calls==1,"Следующее чтение повторило действие");
    }
    transport.stop();
}
void bounded_timeout_and_stop() {
    Transport transport([](const json&) { return json{{"ok", true}}; }, 0, 150ms);
    require(transport.start(), "Транспорт таймаута не запущен");
    {
        Client client(port(transport));
        std::this_thread::sleep_for(300ms);
        client.send_json(request("after-idle"));
        require(client.read_json().at("type") == "response", "Неактивное соединение ошибочно завершено");
    }
    {
        Client client(port(transport));
        client.send_bytes(std::string{"\0\0", 2});
        require(client.closed(), "Неполный кадр не завершён по таймауту");
        require(transport.status().at("last_error").at("code") == "io_timeout", "Не отмечен таймаут");
    }
    transport.stop();
    for (int cycle = 0; cycle < 15; ++cycle) {
        require(transport.start(), "Повторный запуск не выполнен");
        Client client(port(transport));
        client.send_bytes(std::string{"\0", 1});
        const auto start = std::chrono::steady_clock::now();
        transport.stop();
        require(std::chrono::steady_clock::now() - start < 1s, "stop завис на незавершённом кадре");
        require(client.closed(), "stop не завершил сокет");
    }
}

void long_session_and_busy_port() {
    std::atomic_int calls{0},action_calls{0};
    Transport transport([&](const json& envelope) {
        ++calls;
        if(envelope.at("op")=="action") {
            ++action_calls;
            return json{{"executed",true},{"may_have_executed",true},{"do_not_retry",true}};
        }
        return json{{"ok",true}};
    }, 0);
    require(transport.start(), "Первый слушатель не создан");
    require(transport.status().at("max_session_requests").is_null(),"Статус сохранил предел числа запросов сессии");
    Transport occupied([](const json&) { return json::object(); }, port(transport));
    require(!occupied.start(), "Занятый порт неожиданно открыт повторно");
    {
        Client client(port(transport));
        json first_session;
        constexpr int requests=1030;
        for (int index = 0; index < requests; ++index) {
            client.send_json(request(std::to_string(index)));
            const auto response=client.read_json();
            require(response.at("type")=="response" && response.at("result").at("ok")==true,
                    "Длинная сессия отвергла запрос");
            if(index==0)first_session=response.at("session_id");
            require(response.at("session_id")==first_session,"Длинная сессия сменилась между запросами");
        }
        client.send_json(request("0"));
        const auto duplicate=client.read_json();
        require(duplicate.at("error").at("code")=="duplicate_request_id" && duplicate.at("session_id")==first_session,
                "Длинная сессия потеряла защиту от повторного ID");
        require(calls==requests && action_calls==0,"Повторный ID вызвал обработчик");
        client.send_json(request("action-after-long-session","action"));
        const auto action=client.read_json();
        require(action.at("type")=="response" && action.at("session_id")==first_session
                && action.at("result").at("executed")==true && action_calls==1,
                "Действие после 1024 запросов не выполнилось ровно один раз");
        client.send_json(request("getter-after-action","getter"));
        const auto getter=client.read_json();
        require(getter.at("result").at("ok")==true && getter.at("session_id")==first_session
                && calls==requests+2 && action_calls==1,
                "Следующее чтение сменило сессию или повторило действие");
    }
    transport.stop();
}

} // namespace

int main() {
    try {
        exchange_duplicate_and_reconnect();
        execution_error_metadata();
        large_request_and_result_preserve_effect_and_session();
        request_errors_and_handler_errors();
        bounded_timeout_and_stop();
        long_session_and_busy_port();
        std::cout << "6 сценариев нативного транспорта пройдены\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "Проверка транспорта не пройдена: " << error.what() << '\n';
        return 1;
    }
}
