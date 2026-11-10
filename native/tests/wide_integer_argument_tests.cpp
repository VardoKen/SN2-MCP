#include <g1r_mcp/scalar_arguments.hpp>

#include <array>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

namespace {
using nlohmann::json;
int checks{};
void require(bool good,const char* message) {
    ++checks;
    if(!good)throw std::runtime_error(message);
}
template<class Action>void reject(Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()=="invalid_argument","Неверный код отказа целого аргумента");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Отказ должен явно исключать исполнение");
        return;
    }
    throw std::runtime_error("Ожидался отказ целого аргумента");
}
template<class T>void exact_bytes(T value) {
    std::array<unsigned char,sizeof(T)+2> memory{};
    memory.fill(0xa5);
    std::memcpy(memory.data()+1,&value,sizeof(value));
    T copied{};
    std::memcpy(&copied,memory.data()+1,sizeof(copied));
    require(copied==value,"Типизированная копия потеряла старшие биты");
    require(memory.front()==0xa5 && memory.back()==0xa5,"Копия вышла за пределы целого поля");
}
}

int main() {
    using namespace g1r_mcp;
    try {
        static_assert(std::is_same_v<decltype(int64_argument(json{})),std::int64_t>);
        static_assert(std::is_same_v<decltype(uint64_argument(json{})),std::uint64_t>);
        static_assert(std::is_same_v<decltype(uint32_argument(json{})),std::uint32_t>);
        constexpr auto signed_min=std::numeric_limits<std::int64_t>::min();
        constexpr auto signed_max=std::numeric_limits<std::int64_t>::max();
        constexpr auto unsigned_max=std::numeric_limits<std::uint64_t>::max();
        constexpr auto uint32_max=std::numeric_limits<std::uint32_t>::max();

        // Проверяем оба целочисленных домена JSON, а не только положительные литералы C++.
        for(const auto value:std::vector<std::int64_t>{signed_min,signed_min+1,-9007199254740993LL,
                -4294967296LL,-1,0,1,4294967296LL,9007199254740993LL,signed_max-1,signed_max}) {
            const json input=value;
            require(input.is_number_integer() && !input.is_number_unsigned(),"Нужен signed-домен JSON");
            require(int64_argument(input)==value,"Int64 потерял значение signed JSON");
            exact_bytes(int64_argument(input));
        }
        for(const auto value:std::vector<std::uint64_t>{0,1,uint32_max,
                std::uint64_t{uint32_max}+1,9007199254740992ULL,9007199254740993ULL,
                static_cast<std::uint64_t>(signed_max)}) {
            const json input=value;
            require(input.is_number_unsigned(),"Нужен unsigned-домен JSON");
            require(int64_argument(input)==static_cast<std::int64_t>(value),"Int64 потерял допустимый unsigned JSON");
        }
        for(const auto value:std::vector<std::uint64_t>{static_cast<std::uint64_t>(signed_max)+1,
                0xfedcba9876543210ULL,unsigned_max}) {
            reject([&]{int64_argument(json(value));});
        }

        for(const auto value:std::vector<std::uint64_t>{0,1,uint32_max,
                std::uint64_t{uint32_max}+1,9007199254740992ULL,9007199254740993ULL,
                static_cast<std::uint64_t>(signed_max),static_cast<std::uint64_t>(signed_max)+1,
                0xfedcba9876543210ULL,unsigned_max-1,unsigned_max}) {
            const json input=value;
            require(uint64_argument(input)==value,"UInt64 потерял unsigned JSON или старшие биты");
            exact_bytes(uint64_argument(input));
            // Число проходит JSON text без промежуточного double и сохраняет соседей выше 2^53.
            const auto reparsed=json::parse(input.dump());
            require(uint64_argument(reparsed)==value,"JSON roundtrip изменил UInt64");
        }
        for(const auto value:std::vector<std::int64_t>{0,1,4294967296LL,9007199254740993LL,signed_max}) {
            const json input=value;
            require(!input.is_number_unsigned(),"Нужен signed-домен для UInt64");
            require(uint64_argument(input)==static_cast<std::uint64_t>(value),"UInt64 отклонил неотрицательный signed JSON");
        }
        for(const auto value:std::vector<std::int64_t>{-1,-4294967296LL,signed_min}) {
            reject([&]{uint64_argument(json(value));});
        }

        for(const auto value:std::vector<std::uint64_t>{0,1,2147483648ULL,uint32_max}) {
            require(uint32_argument(json(value))==static_cast<std::uint32_t>(value),"UInt32 потерял unsigned JSON");
            require(uint32_argument(json(static_cast<std::int64_t>(value)))==static_cast<std::uint32_t>(value),
                    "UInt32 отклонил допустимый signed JSON");
            exact_bytes(uint32_argument(json(value)));
        }
        for(const json& input:std::vector<json>{json(std::uint64_t{uint32_max}+1),json(std::int64_t{4294967296LL}),
                json(std::uint64_t{9007199254740993ULL}),json(unsigned_max),json(std::int64_t{-1}),json(signed_min)}) {
            reject([&]{uint32_argument(input);});
        }

        // Десятичные границы в реальном JSON тексте: nlohmann сохраняет полный целочисленный диапазон.
        require(int64_argument(json::parse("-9223372036854775808"))==signed_min,"JSON-текст INT64_MIN изменился");
        require(int64_argument(json::parse("9223372036854775807"))==signed_max,"JSON-текст INT64_MAX изменился");
        require(uint64_argument(json::parse("18446744073709551615"))==unsigned_max,"JSON-текст UINT64_MAX изменился");
        require(uint32_argument(json::parse("4294967295"))==uint32_max,"JSON-текст UINT32_MAX изменился");
        reject([&]{int64_argument(json::parse("9223372036854775808"));});
        reject([&]{uint32_argument(json::parse("4294967296"));});
        const auto overflow_unsigned=json::parse("18446744073709551616");
        const auto overflow_signed=json::parse("-9223372036854775809");
        require(overflow_unsigned.is_number_float() && overflow_signed.is_number_float(),
                "JSON parser должен обозначить переполнение целого как float");
        for(const json& input:std::vector<json>{overflow_unsigned,overflow_signed}) {
            reject([&]{int64_argument(input);});
            reject([&]{uint64_argument(input);});
            reject([&]{uint32_argument(input);});
        }

        // Даже точное и целое float не разрешено: тип, а не математическая целочисленность, задаёт контракт.
        const std::vector<json> non_integer={nullptr,true,false,"0","9007199254740993","18446744073709551615",
            json::array(),json::array({1}),json::object(),json{{"value",1}},0.0,-0.0,1.0,1.5,-1.5,
            9007199254740992.0,std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN(),
            json::parse("1e0"),json::parse("0.0")};
        for(const auto& input:non_integer) {
            require(!input.is_number_integer(),"Матрица неверных типов содержит целое JSON");
            reject([&]{int64_argument(input);});
            reject([&]{uint64_argument(input);});
            reject([&]{uint32_argument(input);});
        }
        require(uint64_argument(json::parse("9007199254740992"))!=uint64_argument(json::parse("9007199254740993")),
                "Соседние целые выше double precision не должны сливаться");
        std::cout<<"wide_integer_arguments: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
