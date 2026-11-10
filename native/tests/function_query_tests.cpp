#include <g1r_mcp/function_query.hpp>

#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
void require(bool result, const char* message) {
    if (!result) throw std::runtime_error(message);
}
}

int main() {
    namespace query = g1r_mcp::function_query;
    try {
        require(query::valid(""), "Пустой фильтр разрешён");
        require(query::valid(std::string(512, 'a')), "Граница 512 байт разрешена");
        require(!query::valid(std::string("Read\0Value", 10)), "NUL не разрешён");
        require(query::name_matches("ReadProbeValue", "probevalue"), "Сравнение подстроки нечувствительно к регистру");
        require(query::name_matches("ReadProbeValue", ""), "Пустой фильтр сохраняет все имена");
        require(!query::name_matches("OtherMethod", "Probe"), "Неподходящая функция отклоняется");
        std::vector<std::string> functions;
        for (int index = 0; index < 220; ++index) functions.push_back("Unrelated" + std::to_string(index));
        functions.push_back("ReadTarget");
        functions.push_back("ReadTargetInherited");
        std::vector<std::string> selected;
        for (const auto& name : functions) {
            if (!query::name_matches(name, "target")) continue;
            if (selected.size() == 2) break;
            selected.push_back(name);
        }
        require(selected == std::vector<std::string>{"ReadTarget", "ReadTargetInherited"},
                "Фильтрация до лимита находит методы за первыми 200 и сохраняет порядок");
        std::cout << "function_query: проверены фильтр, порядок и пределы\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
