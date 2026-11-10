#include <g1r_mcp/selected_property_query.hpp>

#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
int checks=0;
void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}
template<class Operation>
void rejects(Operation operation) {
    try {operation();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()=="invalid_property_names","Неверный код отказа списка свойств");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Неверный список должен отвергаться до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ списка свойств");
}
}

int main() {
    using nlohmann::json;
    namespace query=g1r_mcp::selected_property_query;
    try {
        const json requested=json::array({"StoredNone","StoredEntry","MissingForProbe","CallsCount"});
        int calls=0;
        const auto result=query::validated(requested,[&](const json& names) {
            ++calls;
            require(&names==&requested,"Reader должен получать исходный запрос без копии/нормализации");
            return names;
        });
        require(calls==1 && result==requested,"Порядок имён не должен меняться");
        const json differently_cased=json::array({"Entry","entry"," Entry "});
        query::validate(differently_cased);
        require(differently_cased[0]=="Entry" && differently_cased[1]=="entry" && differently_cased[2]==" Entry ",
                "Проверка не должна сворачивать регистр или убирать пробелы");

        json extended=json::array();
        for(std::size_t i=0;i<33;++i)extended.push_back("Property"+std::to_string(i));
        extended[0]=std::string(257,'a');
        std::string unicode;
        for(int i=0;i<129;++i)unicode+="\xD0\xAF";
        extended[32]=unicode;
        calls=0;
        const auto extended_result=query::validated(extended,[&](const json& names) {
            ++calls;
            require(&names==&extended,"Большой запрос не должен копироваться или нормализоваться до reader");
            return names;
        });
        require(calls==1 && extended_result==extended && extended_result.size()==33,
                "Запрос выше прежних пределов не дошёл до reader полностью и в исходном порядке");
        require(extended_result[0].get_ref<const std::string&>().size()==257
                && extended_result[32].get_ref<const std::string&>()==unicode && unicode.size()==258,
                "Длинные ASCII и UTF-8 имена изменены");
        auto duplicate_extended=extended;duplicate_extended[32]=extended[0];
        const std::vector<json> invalid={
            nullptr,false,7,"StoredEntry",json::object(),json::array(),duplicate_extended,
            json::array({nullptr}),json::array({false}),json::array({1}),json::array({json::object()}),
            json::array({""}),
            json::array({std::string("Entry\0Other",11)}),json::array({"Entry","Entry"}),
            json::array({"StoredEntry",nullptr})};
        for(const auto& names:invalid) {
            calls=0;
            rejects([&]{query::validated(names,[&](const json&){++calls;return true;});});
            require(calls==0,"Неверный список не должен доходить до reader");
        }
        std::cout<<"selected_property_query: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<'\n';
        return 1;
    }
}
