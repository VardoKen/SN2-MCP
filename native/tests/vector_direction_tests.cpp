#include <g1r_mcp/vector_parameter_direction.hpp>

#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
using nlohmann::json;
using g1r_mcp::HandlerError;
namespace direction = g1r_mcp::vector_parameter_direction;
int checks{};

void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}

template<class Operation>
void rejects(const char* code,Operation&& operation) {
    try {operation();}
    catch(const HandlerError& error) {
        require(error.code()==code,"Неверный код отказа направления FVector");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Неверный FVector должен отвергаться до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ направления FVector");
}
}

int main() {
    try {
        // Литералы не зависят от констант helper: тест замечает ошибку таблицы CPF.
        require(direction::classify(0x80)==direction::Direction::input,
                "CPF_Parm без Out/Reference должен быть value input");
        require(direction::classify(0x180)==direction::Direction::output,
                "CPF_Parm|CPF_OutParm должен быть pure out");
        require(direction::classify(0x880)==direction::Direction::input,
                "0x800 не является ReferenceParm и не делает input ссылкой");
        require(direction::classify(0x980)==direction::Direction::output,
                "0x800 не является ReferenceParm и не превращает pure out в inout");

        // Const FVector с Out/ref в reflection остаётся входом. Его значение
        // читает encode(XYZ), а output-маркер и output decoder не выбираются.
        for(const auto flags:std::vector<std::uint64_t>{0x182,0x982,0x08000082,0x08000182,
                                                       0x08000882,0x08000982}) {
            const auto classified=direction::classify(flags);
            require(classified==direction::Direction::input,
                    "Const FVector с Out или Reference должен быть input");
            require(classified!=direction::Direction::output,
                    "Const FVector input не должен требовать out-маркер или выдавать out_parameters");
            rejects("unsupported_signature",[&]{g1r_mcp::parameter_direction::classify(flags);});
        }

        // Возвращаемый FVector обрабатывается другим маршрутом, не input/out.
        // Return нельзя принять за const input даже при наличии Out или Ref.
        for(const auto flags:std::vector<std::uint64_t>{0x480,0x580,0x400,0x500,0x08000480,
                                                       0x482,0x582,0x08000482,0x08000582,0x08000d82})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        // Нельзя принять out либо reference без CPF_Parm за аргумент функции.
        for(const auto flags:std::vector<std::uint64_t>{0,0x100,0x800,0x08000000,0x08000100,
                                                       0x102,0x902,0x08000002,0x08000102})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        // Mutable input ref и inout без ConstParm запрещены.
        for(const auto flags:std::vector<std::uint64_t>{0x08000080,0x08000180,0x08000880,0x08000980})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        // ConstParm без подтверждающего Out/ref не является новым допустимым ABI.
        for(const auto flags:std::vector<std::uint64_t>{0x82,0x882})
            rejects("unsupported_signature",[&]{direction::classify(flags);});

        const json marker={{"out",true}};
        const auto unchanged=marker;
        direction::validate_output_marker(marker);
        require(marker==unchanged,"Проверка out-маркера не должна изменять JSON");

        // Маркер обязателен ровно {out:true}; тип bool не подменяется числом.
        const std::vector<json> invalid={
            nullptr,true,false,0,1,1.0,"out",json::array(),json::array({true}),json::object(),
            json{{"out",false}},json{{"out",nullptr}},json{{"out",0}},json{{"out",1}},
            json{{"out",1.0}},json{{"out","true"}},json{{"out",json::array({true})}},
            json{{"out",json{{"out",true}}}},json{{"Out",true}},json{{"output",true}},
            json{{std::string("out\0",4),true}},json{{"out",true},{"value",nullptr}},
            json{{"out",true},{"X",0}},json{{"out",true},{"out ",true}},
            json{{"X",0},{"Y",0},{"Z",0}}
        };
        for(const auto& value:invalid) {
            const auto before=value;
            rejects("invalid_argument",[&]{direction::validate_output_marker(value);});
            require(value==before,"Отказ out-маркера должен сохранять входной JSON");
        }

        std::cout<<"vector_parameter_direction: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
