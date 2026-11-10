#include <sn2_mcp/function_contract.hpp>

#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

namespace {
using nlohmann::json;
int checks{};
void require(bool good,const char* message) {
    ++checks;
    if(!good)throw std::runtime_error(message);
}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный код отказа");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Отказ должен явно исключать исполнение");
        return;
    }
    throw std::runtime_error("Ожидался отказ контракта");
}
json contract(const std::string& name) {
    return {{"session","session-1"},{"target_handle","obj-1"},{"function_name",name},
            {"header",{{"count",1},{"child_head",17}}},
            {"fields",json::array({{{"pointer",17},{"next",std::uint64_t{18}},{"flags",std::uint64_t{0}}},
                                   {{"pointer",18},{"next",std::uint64_t{0}},{"flags",std::uint64_t{0x80}}}})}};
}
}

int main() {
    using namespace sn2_mcp;
    try {
        std::map<std::string,json> saved;
        std::uint64_t sequence{};
        auto original=contract("ReadMarker");
        const auto first=store_function_contract(saved,sequence,original);
        for(std::size_t index=1;index<257;++index)
            store_function_contract(saved,sequence,contract("Function"+std::to_string(index)));
        require(saved.size()==257 && sequence==257,
                "257 разных контрактов должны сохраняться без отзыва существующих токенов");
        const auto full=saved;
        const auto full_sequence=sequence;
        require(store_function_contract(saved,sequence,original)==first,
                "Большая таблица должна сохранять повторный доступ к известному контракту");
        require(saved==full && sequence==full_sequence,
                "Повторное чтение не должно менять большую таблицу или счётчик");
        auto large=contract("LargeFunction");large["payload"]=std::string(65537,'x');
        const auto large_bytes=large.dump();
        const auto large_token=store_function_contract(saved,sequence,large);
        require(large_bytes.size()>65536 && saved.size()==258 && sequence==258,
                "Контракт больше 64 КиБ должен сохраняться после 257 известных сигнатур");
        require(checked_function_contract(saved,large_token,"session-1","obj-1","LargeFunction").dump()==large_bytes,
                "Чтение большого контракта должно возвращать всю сериализацию без сокращения");
        require(store_function_contract(saved,sequence,large)==large_token && saved.size()==258 && sequence==258,
                "Повторный большой контракт должен переиспользовать токен");
        large["payload"].get_ref<std::string&>()[0]='y';
        require(checked_function_contract(saved,large_token,"session-1","obj-1","LargeFunction").dump()==large_bytes,
                "Сохранённый большой контракт должен оставаться независимой копией");

        // Сохранённый контракт — собственная копия. Изменение исходного JSON не меняет выданный токен.
        original["fields"][0]["flags"]=std::uint64_t{0x80};
        require(checked_function_contract(saved,first,"session-1","obj-1","ReadMarker")==full.at(first),
                "Изменение входного JSON не должно мутировать сохранённый контракт");
        reject("signature_changed",[&]{require_same_function_contract(full.at(first),original);});
        auto reordered=full.at(first);std::swap(reordered["fields"][0],reordered["fields"][1]);
        reject("signature_changed",[&]{require_same_function_contract(full.at(first),reordered);});

        std::map<std::string,json> exhausted;
        std::uint64_t final_sequence=std::numeric_limits<std::uint64_t>::max()-1;
        const auto final_token=store_function_contract(exhausted,final_sequence,contract("Final"));
        require(final_token=="sig-18446744073709551615" && valid_signature_token(final_token),
                "Последнее значение uint64 должно давать допустимый токен без переполнения");
        const auto exhausted_before=exhausted;
        require(store_function_contract(exhausted,final_sequence,contract("Final"))==final_token,
                "Исчерпание счётчика не должно лишать доступа к уже выданному токену");
        reject("signature_contract_limit",[&]{store_function_contract(exhausted,final_sequence,contract("AfterFinal"));});
        require(exhausted==exhausted_before && final_sequence==std::numeric_limits<std::uint64_t>::max(),
                "Отказ исчерпания должен быть атомарным и не переходить на sig-0");
        exhausted.clear();
        reject("signature_contract_limit",[&]{store_function_contract(exhausted,final_sequence,contract("NewSession"));});
        require(exhausted.empty() && final_sequence==std::numeric_limits<std::uint64_t>::max(),
                "Очистка сессии не должна сбрасывать исчерпанную монотонную последовательность");

        int reads{};
        const auto chain=capture_function_field_chain(1,[&](std::uintptr_t pointer){
            ++reads;return json{{"pointer",pointer},{"next",pointer<129?pointer+1:std::uintptr_t{0}}};
        });
        require(reads==129 && chain.size()==129 && chain.back()["pointer"]==129 && chain.back()["next"]==std::uintptr_t{0},
                "129-я запись должна читаться и завершать конечную цепочку");
        reads=0;
        reject("unsupported_signature",[&]{
            capture_function_field_chain(1,[&](std::uintptr_t pointer){
                ++reads;return json{{"next",pointer==1?std::uintptr_t{2}:std::uintptr_t{1}}};
            });
        });
        require(reads==2,"Повторный узел цикла не должен вызывать reader второй раз");
        reads=0;
        require(capture_function_field_chain(0,[&](std::uintptr_t){++reads;return json{};}).empty() && reads==0,
                "Пустая цепочка не должна вызывать reader");
        for(const json& malformed:std::vector<json>{json::array(),json{{"pointer",1}},json{{"next",nullptr}},
                json{{"next",true}},json{{"next",-1}},json{{"next",0}},json{{"next","0"}}}) {
            reads=0;
            reject("unsupported_signature",[&]{
                capture_function_field_chain(1,[&](std::uintptr_t){++reads;return malformed;});
            });
            require(reads==1,"Неверный next должен остановить чтение после первого поля");
        }
        std::cout<<"function_contract_model: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
