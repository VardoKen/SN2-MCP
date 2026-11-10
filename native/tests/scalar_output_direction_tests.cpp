#include <g1r_mcp/parameter_direction.hpp>

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using nlohmann::json;
using g1r_mcp::HandlerError;
namespace direction = g1r_mcp::parameter_direction;
int checks{};

void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}

template<class Operation>
void rejects(const char* code,Operation&& operation) {
    try {operation();}
    catch(const HandlerError& error) {
        require(error.code()==code,"Неверный код отказа скалярного out");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Скалярный out должен отвергаться до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ скалярного out");
}
}

int main() {
    try {
        // CPF-литералы не берутся из helper: тест замечает неверную таблицу битов.
        require(direction::classify(0x180)==direction::Direction::output,
                "CPF_Parm|CPF_OutParm должен классифицироваться как pure out");
        require(direction::classify(0x980)==direction::Direction::output,
                "DisableEditOnTemplate 0x800 не означает ReferenceParm");

        // Generic helper сохраняет input-направление; scalar-output branch
        // выбирается вызывающим кодом только для параметра с CPF_OutParm.
        require(direction::classify(0x80)==direction::Direction::input,
                "CPF_Parm без Out не является скалярным out");
        require(direction::classify(0x880)==direction::Direction::input,
                "0x800 не должен превращать обычный input в reference или output");

        for(const auto flags:std::vector<std::uint64_t>{0,0x100,0x800,0x900,0x08000000,0x08000100})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        // Возвраты обрабатываются отдельно и не могут стать output-параметром.
        for(const auto flags:std::vector<std::uint64_t>{0x400,0x480,0x500,0x580,0xd80})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        // Const и настоящий ReferenceParm запрещены независимо от наличия Out.
        for(const auto flags:std::vector<std::uint64_t>{0x82,0x182,0x882,0x982,
                0x08000080,0x08000180,0x08000880,0x08000980,
                0x08000082,0x08000182,0x08000580})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        const json marker={{"out",true}};
        const auto original=marker;
        direction::validate_output_marker(marker);
        require(marker==original,"Проверка разрешённого out-маркера не меняет JSON");

        const std::vector<json> invalid={
            nullptr,true,false,0,1,1.0,"out",json::array(),json::array({true}),json::object(),
            json{{"out",false}},json{{"out",nullptr}},json{{"out",0}},json{{"out",1}},
            json{{"out",1.0}},json{{"out","true"}},json{{"out",json::array({true})}},
            json{{"out",json{{"out",true}}}},json{{"Out",true}},json{{"output",true}},
            json{{std::string("out\0",4),true}},json{{"out",true},{"value",nullptr}},
            json{{"out",true},{"X",0}},json{{"out",true},{"out ",true}},
            json{{"out",true},{"",false}},json{{"out",true},{"reference",false}}
        };
        for(const auto& value:invalid) {
            const auto before=value;
            rejects("invalid_argument",[&]{direction::validate_output_marker(value);});
            require(value==before,"Отказ скалярного out-маркера должен сохранять входной JSON");
        }

        std::cout<<"scalar_output_direction: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
