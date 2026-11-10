#include <g1r_mcp/object_array_input_lifetime.hpp>

#include <array>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <type_traits>
#include <vector>

namespace {
using g1r_mcp::ArrayHeader;
using g1r_mcp::object_array_input_lifetime::Ledger;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ учёта объектных массивов");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Чистый helper не должен приписывать исполнение игровому вызову");return;
    }
    throw std::runtime_error("Ожидался отказ учёта объектных массивов");
}
void no_free(Ledger& ledger) {
    int calls{};const auto cleanup=ledger.release([&](std::uintptr_t){++calls;return true;});
    require(!cleanup.attempted && !cleanup.confirmed && cleanup.uncertain && calls==0,
            "Неизвестное владение запрещает все Free");
}
}

int main() {
    using namespace g1r_mcp::object_array_input_lifetime;
    static_assert(!std::is_copy_constructible_v<Ledger> && !std::is_copy_assignable_v<Ledger>);
    static_assert(!std::is_move_constructible_v<Ledger> && !std::is_move_assignable_v<Ledger>);
    static_assert(sizeof(std::uintptr_t)==8);
    try {
        {InputBudget budget;for(int i=0;i<65;++i)budget.add(257);
            require(budget.input_count()==65 && budget.allocation_bytes()==65*257*8,
                    "Количество массивов, элементов и байтов превышает прежние пределы");
            budget.add(0);
            require(budget.input_count()==66 && budget.allocation_bytes()==65*257*8,
                    "Пустой массив после крупных входов не приобретает память");}
        {InputBudget budget;for(int i=0;i<65;++i)budget.add(0);
            require(budget.input_count()==65 && budget.allocation_bytes()==0,"Пустые массивы не имеют предела 32 входа");}
        {InputBudget budget;const auto count=static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max());
            budget.add(count);
            require(budget.input_count()==1 && budget.allocation_bytes()==count*8,"Максимальный int32 count представим без искусственного бюджета");}
        for(const auto count:std::array<std::size_t,2>{static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())+1,
                std::numeric_limits<std::size_t>::max()}) {
            InputBudget budget;budget.add(1);reject("invalid_array_count",[&]{budget.add(count);});
            require(budget.input_count()==1 && budget.allocation_bytes()==8,"Длина за int32 отвергается до расчёта и изменения учёта");
        }

        {Ledger ledger;ledger.reserve_inputs(65);for(int i=0;i<65;++i)ledger.add_input({0,0,0},0);
            require(ledger.input_count()==65 && ledger.allocation_count()==0 && ledger.allocation_bytes()==0,"65 пустых массивов не приобретают allocation");
            int calls{};const auto cleanup=ledger.release([&](std::uintptr_t){++calls;return true;});
            require(!cleanup.attempted && cleanup.confirmed && !cleanup.uncertain && cleanup.released_count==0 && calls==0,
                    "Все пустые массивы очищаются без allocator");}
        {Ledger ledger;const auto cleanup=ledger.release([](std::uintptr_t)->bool {throw std::runtime_error("Неожиданный Free");});
            require(cleanup.confirmed && !cleanup.attempted && !cleanup.uncertain,"Ledger без inputs не вызывает Free");}
        {Ledger ledger;ledger.reserve_inputs(65);for(int i=0;i<65;++i)ledger.add_input({std::uintptr_t{0x1000}+static_cast<std::uintptr_t>(i)*2056,257,257},257);
            require(ledger.input_count()==65 && ledger.allocation_count()==65 && ledger.allocation_bytes()==65*257*8,
                    "65 смежных буферов по 257 элементов превышают 8192 байта");
            std::vector<std::uintptr_t> released;
            const auto cleanup=ledger.release([&](std::uintptr_t pointer){released.push_back(pointer);return true;});
            require(cleanup.confirmed && cleanup.released_count==65 && released.size()==65,"Все 65 самостоятельных буферов освобождаются один раз");
            for(int i=0;i<65;++i)require(released[static_cast<std::size_t>(i)]==std::uintptr_t{0x1000}+static_cast<std::uintptr_t>(i)*2056,
                    "Free получает адрес буфера, принятый от allocator");
            require(ledger.retained_count()==0 && ledger.retained_bytes()==0,"Успешная очистка исключает все буферы из retained");
            const auto again=ledger.release([&](std::uintptr_t pointer){released.push_back(pointer);return true;});
            require(again.confirmed && !again.attempted && again.released_count==65 && released.size()==65,"Повторная очистка не повторяет Free");}

        {alignas(8) std::array<std::uintptr_t,3> first{0x9000,0x9000,0xa000};
            alignas(8) std::array<std::uintptr_t,2> second{0x9000,0xa000};
            const auto first_buffer=reinterpret_cast<std::uintptr_t>(first.data());
            const auto second_buffer=reinterpret_cast<std::uintptr_t>(second.data());
            Ledger ledger;ledger.add_input({first_buffer,3,3},3);ledger.add_input({second_buffer,2,2},2);
            require(ledger.allocation_count()==2 && ledger.allocation_bytes()==40,"Повторяющиеся UObject являются значениями двух независимых буферов");
            std::vector<std::uintptr_t> released;const auto cleanup=ledger.release([&](std::uintptr_t pointer){released.push_back(pointer);return true;});
            require(cleanup.confirmed && released==std::vector<std::uintptr_t>{first_buffer,second_buffer},"Free получает только буферы, никогда содержащиеся в них UObject");
            require(first==std::array<std::uintptr_t,3>{0x9000,0x9000,0xa000} && second==std::array<std::uintptr_t,2>{0x9000,0xa000},
                    "Ledger не меняет значения и порядок повторяющихся UObject");}

        for(const auto alias:std::vector<ArrayHeader>{{0x1000,4,4},{0x1008,2,2},{0x0ff8,2,2},{0x1000,3,3},{0x1000,5,5}}) {
            Ledger ledger;ledger.add_input({0x1000,4,4},4);
            reject("array_lifetime_unknown",[&]{ledger.add_input(alias,static_cast<std::size_t>(alias.count));});
            require(ledger.uncertain() && ledger.input_count()==1 && ledger.retained_count()==1 && ledger.retained_bytes()==32,
                    "Повторный или пересекающийся диапазон сохраняет известный буфер в карантине");no_free(ledger);
        }
        for(const auto current:std::vector<ArrayHeader>{{0x2000,2,2},{0x1000,1,2},{0x1000,2,3},{0,0,0}}) {
            Ledger ledger;ledger.add_input({0x1000,2,2},2);ledger.validate_unchanged_input(0,{0x1000,2,2});
            reject("array_lifetime_unknown",[&]{ledger.validate_unchanged_input(0,current);});no_free(ledger);
        }
        {Ledger ledger;ledger.add_input({0,0,0},0);reject("array_lifetime_unknown",[&]{ledger.validate_unchanged_input(0,{0x1000,0,0});});no_free(ledger);}
        {Ledger ledger;ledger.add_input({0x1000,2,2},2);reject("array_lifetime_unknown",[&]{ledger.validate_unchanged_input(1,{0x1000,2,2});});no_free(ledger);}
        {Ledger ledger;ledger.add_input({0x1000,2,2},2);ledger.mark_uncertain();
            reject("array_lifetime_unknown",[&]{ledger.add_input({0x2000,2,2},2);});no_free(ledger);}
        {Ledger ledger;ledger.mark_uncertain();require(ledger.retained_count()==0,"Неизвестный allocator не создаёт выдуманный буфер");no_free(ledger);}

        for(const auto invalid:std::vector<ArrayHeader>{{0,1,1},{0x1000,0,0},{0x1001,1,1},{0x1000,-1,1},
                {0x1000,1,-1},{0x1000,2,2},{0x1000,1,2},{0x1000,0,1},
                {std::numeric_limits<std::uintptr_t>::max()-7,1,1}}) {
            Ledger ledger;ledger.add_input({0x2000,1,1},1);
            reject("invalid_array_header",[&]{ledger.add_input(invalid,1);});
            require(ledger.retained_count()==1 && ledger.retained_bytes()==8,"Неверный allocator header сохраняет ранее принятый буфер");no_free(ledger);
        }
        {Ledger ledger;reject("invalid_array_header",[&]{ledger.add_input({0x1000,0,0},0);});no_free(ledger);}
        {Ledger ledger;ledger.add_input({std::numeric_limits<std::uintptr_t>::max()-15,1,1},1);
            const auto cleanup=ledger.release([](std::uintptr_t){return true;});
            require(cleanup.confirmed && cleanup.released_count==1,"Последний непереполняющийся выровненный диапазон допустим");}
        for(const auto count:std::array<std::size_t,2>{static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())+1,
                std::numeric_limits<std::size_t>::max()}) {
            Ledger ledger;ledger.add_input({0x1000,1,1},1);reject("invalid_array_header",[&]{ledger.add_input({0x2000,1,1},count);});
            require(ledger.input_count()==1 && ledger.retained_bytes()==8,"Отказ длины сохраняет предыдущий input");no_free(ledger);
        }
        {Ledger ledger;ledger.reserve_inputs(66);for(int i=0;i<65;++i)ledger.add_input({0,0,0},0);
            ledger.add_input({0x1000,1,1},1);
            ledger.validate_unchanged_input(65,{0x1000,1,1});
            require(ledger.input_count()==66 && ledger.retained_count()==1,"Вход после 65 пустых массивов сохраняет правильный индекс");
            int calls{};const auto cleanup=ledger.release([&](std::uintptr_t pointer){++calls;return pointer==0x1000;});
            require(cleanup.confirmed && calls==1 && ledger.retained_count()==0,"Освобождается только единственный выделенный буфер");}

        for(const bool throws:{false,true}) {
            Ledger ledger;ledger.add_input({0x1000,1,1},1);ledger.add_input({0x2000,2,2},2);
            ledger.add_input({0x3000,3,3},3);ledger.add_input({0x4000,4,4},4);
            std::vector<std::uintptr_t> released;
            const auto cleanup=ledger.release([&](std::uintptr_t pointer)->bool {
                released.push_back(pointer);if(pointer!=0x3000)return true;
                if(throws)throw std::runtime_error("allocator fault");return false;
            });
            require(cleanup.attempted && !cleanup.confirmed && cleanup.uncertain && cleanup.released_count==2,
                    "Отказ третьего Free учитывает только два подтверждённых освобождения");
            require(released==std::vector<std::uintptr_t>{0x1000,0x2000,0x3000} && ledger.retained_count()==2 && ledger.retained_bytes()==56,
                    "Отказ Free останавливает обработку и сохраняет два неосвобождённых буфера");
            no_free(ledger);require(released.size()==3,"Неопределённый Free никогда не повторяется");
        }
        {Ledger ledger;ledger.add_input({0x1000,1,1},1);int calls{};ledger.release([&](std::uintptr_t){++calls;return true;});
            reject("array_lifetime_unknown",[&]{ledger.add_input({0x2000,1,1},1);});
            require(calls==1 && ledger.retained_count()==0,"Adopt после cleanup не возобновляет владение");no_free(ledger);}
        {Ledger ledger;ledger.release([](std::uintptr_t){return true;});
            reject("array_lifetime_unknown",[&]{ledger.add_input({0,0,0},0);});no_free(ledger);}
        std::cout<<"object_array_input_lifetime: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
