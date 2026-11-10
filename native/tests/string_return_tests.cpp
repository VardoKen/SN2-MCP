#include <g1r_mcp/string_return.hpp>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
int checks=0;
void require(bool condition,const char* message) {
    ++checks;if(!condition)throw std::runtime_error(message);
}
template<class Operation>
g1r_mcp::HandlerError failure(Operation&& operation) {
    try {operation();}
    catch(const g1r_mcp::HandlerError& error) {return error;}
    throw std::runtime_error("Ожидался отказ FString");
}
}
int main() {
    using namespace g1r_mcp;
    try {
        const StringHeader value{0x1000,3,3};
        int decoded=0,released=0;
        auto decoder=[&](const StringHeader& copied) {
            require(copied.data==value.data && copied.count==3,"Декодеру передана другая копия");
            ++decoded;return std::string("ok");
        };
        auto release=[&](uintptr_t pointer) {
            require(pointer==value.data,"Освобождён другой указатель");++released;return true;
        };
        require(copied_owned_string_return(value,decoder,release)=="ok","Корректный результат не возвращён");
        require(decoded==1 && released==1,"Корректная строка должна читаться и освобождаться один раз");
        decoded=0;released=0;
        require(copied_owned_string_return(StringHeader{0,0,0},
            [&](const StringHeader&){++decoded;return std::string();},release).empty(),"Null empty не поддержан");
        require(decoded==1 && released==0,"Null empty не должен обращаться к allocator");
        released=0;
        require(copied_owned_string_return(StringHeader{0x1000,0,8},
            [](const StringHeader&){return std::string();},release).empty(),"Reserved empty не поддержан");
        require(released==1,"Reserved empty должен освободить собственную память");
        const std::vector<StringHeader> invalid={{0,-1,0},{0,0,-1},{0,1,1},{0,0,1},
            {0x1000,2,1},{0x1000,0,0},{0x1001,1,1},
            {std::numeric_limits<uintptr_t>::max()-1,1,1}};
        for(const auto& header:invalid) {
            decoded=0;released=0;
            const auto error=failure([&]{copied_owned_string_return(header,decoder,release);});
            require(error.code()=="game_result_error","Неверный layout требует ошибку результата");
            require(!error.details().at("cleanup_attempted").get<bool>()
                    && !error.details().at("cleanup_confirmed").get<bool>(),"Неверный layout нельзя освобождать");
            require(decoded==0 && released==0,"Неверный layout не должен вызывать decoder/allocator");
        }
        released=0;
        const auto utf_error=failure([&]{copied_owned_string_return(value,
            [](const StringHeader&)->std::string{throw HandlerError("invalid_string","UTF-16");},release);});
        require(released==1 && utf_error.details().at("cleanup_confirmed")==true,"Ошибка UTF-16 обязана освободить valid layout");
        require(utf_error.details().at("cause_code")=="invalid_string","Причина decoder потеряна");
        released=0;
        const auto large_error=failure([&]{copied_owned_string_return(StringHeader{0x1000,40000,40000},
            [](const StringHeader&)->std::string{throw HandlerError("string_too_large","Предел копии");},release);});
        require(released==1 && large_error.details().at("cleanup_confirmed")==true,"Большой valid результат должен освобождаться");
        released=0;
        const auto free_error=failure([&]{copied_owned_string_return(value,decoder,
            [&](uintptr_t){++released;return false;});});
        require(released==1 && free_error.details().at("cause_code")=="string_cleanup_fault","Отказ освобождения не должен повторяться");
        require(free_error.details().at("executed")==true && free_error.details().at("do_not_retry")==true
                && free_error.details().at("cleanup_confirmed")==false,"Отказ освобождения требует факта выполненного вызова");
        released=0;
        const auto throw_error=failure([&]{copied_owned_string_return(value,decoder,
            [&](uintptr_t)->bool{++released;throw std::runtime_error("allocator");});});
        require(released==1 && throw_error.details().at("cleanup_state")=="unknown","Исключение allocator требует unknown без retry");
        released=0;
        const auto both_error=failure([&]{copied_owned_string_return(value,
            [](const StringHeader&)->std::string{throw HandlerError("invalid_memory","decoder");},
            [&](uintptr_t){++released;return false;});});
        require(released==1 && both_error.details().at("result_cause_code")=="invalid_memory","Двойная ошибка должна сохранять причины без doublefree");
        std::cout<<"FString: "<<checks<<" проверок прошли\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
