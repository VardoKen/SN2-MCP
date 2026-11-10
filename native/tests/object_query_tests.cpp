#include <g1r_mcp/object_query.hpp>

#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool result, const char* message) {
    if (!result) throw std::runtime_error(message);
}
}

int main() {
    using namespace g1r_mcp::object_query;
    try {
        require(valid_exact_path(""), "Пустой exact_path разрешён");
        require(valid_exact_path(std::string(8192, 'x')), "Граница 8192 байт разрешена");
        require(!valid_exact_path(std::string("World\0Actor", 11)), "NUL отклоняется");
        require(matches("actor", "", "Actor", ""), "Старый поиск нечувствителен к регистру");
        require(matches("", "", "Actor", ""), "Пустой запрос совпадает");
        require(contains_case_insensitive("", ""), "Пустая подстрока совпадает с пустым значением");
        require(!needs_path("actor", "", "Actor"), "Обычный поиск не требует полного пути");
        require(!matches("World", "", "Actor", "World.Actor"), "Обычный запрос сохраняет поиск имени");
        require(needs_path("world.actor", "", "Actor"), "Путь запрашивается для path query");
        require(matches("world.actor", "", "Actor", "World.Actor"), "Путь ищется по подстроке");
        require(matches("/script/core", "", "Class", "/Script/CoreUObject.Class"), "Слэш включает поиск пути");
        require(!needs_path("Actor.Name", "", "Actor.Name"), "Имя с точкой уже совпало, path не нужен");
        require(exact_path_may_match_name("World.Actor", "Actor"), "Предварительная проверка имени");
        require(!exact_path_may_match_name("World.Actor", "Other"), "Другой объект отбрасывается без пути");
        require(needs_path("Actor", "World.Actor", "Actor"), "Exact path всегда проверяется полностью");
        require(matches("actor", "World.Actor", "Actor", "World.Actor"), "Имя и точный путь совместимы");
        require(!matches("actor", "World.Actor", "Actor", "Other.Actor"), "Одинаковые имена разделяются outer");
        require(!matches("", "world.Actor", "Actor", "World.Actor"), "Exact path учитывает регистр");
        require(!matches("Other", "World.Actor", "Actor", "World.Actor"), "Фильтр имени сохраняется вместе с exact");
        require(!matches("world.actor", "World.Actor", "Actor", "Other.Actor"), "Exact не обходится path query");
        std::cout << "object_query: проверены имена, пути, регистр и пределы\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
