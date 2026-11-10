#include <g1r_mcp/linear_color_arguments.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <span>
#include <stdexcept>
#include <vector>

namespace {
using nlohmann::json;
using g1r_mcp::linear_color_arguments::EncodedLinearColor;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ аргумента FLinearColor");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Чистая проверка POD не должна приписывать исполнение игровому вызову");return;
    }
    throw std::runtime_error("Ожидался отказ аргумента FLinearColor");
}
g1r_mcp::linear_color_layout::Layout layout() {return {16,4,false,{{"R","FloatProperty",4,0,1},
    {"G","FloatProperty",4,4,1},{"B","FloatProperty",4,8,1},{"A","FloatProperty",4,12,1}}};}
float read(const EncodedLinearColor& color,int offset) {float value{};std::memcpy(&value,color.bytes.data()+offset,sizeof(value));return value;}
void check_roundtrip(const json& value) {
    const auto reflected=layout();const auto encoded=g1r_mcp::linear_color_arguments::encode(reflected,value);
    require(encoded.size==16 && encoded.bytes.size()==16,"FLinearColor занимает ровно 16 байт");
    const auto output=g1r_mcp::linear_color_arguments::decode(reflected,encoded.bytes);
    require(output.is_object() && output.size()==4,"FLinearColor decode возвращает только четыре компонента");
    for(const auto& component:reflected.components) {
        const auto expected=static_cast<float>(value.at(component.name).get<double>());
        require(read(encoded,component.offset)==expected && output.at(component.name).get<double>()==static_cast<double>(expected),
                "RGBA сохраняются по каноническим offsets без ограничения HDR и отрицательных значений");
    }
}
}

int main() {
    using namespace g1r_mcp;
    using namespace linear_color_arguments;
    static_assert(alignof(EncodedLinearColor)>=4);
    try {
        const auto reflected=layout();const json ordinary={{"R",1.25},{"G",-2.5},{"B",3.75},{"A",-0.5}};
        check_roundtrip(ordinary);check_roundtrip({{"R",-3},{"G",std::uint64_t{2}},{"B",0},{"A",4}});
        constexpr auto maximum=static_cast<double>(std::numeric_limits<float>::max());
        check_roundtrip({{"R",maximum},{"G",-maximum},{"B",static_cast<double>(std::numeric_limits<float>::denorm_min())},{"A",2.0}});
        check_roundtrip({{"R",0.0},{"G",1.0},{"B",-100.0},{"A",100.0}});
        const auto signed_zero=encode(reflected,{{"R",-0.0},{"G",0.0},{"B",-0.0},{"A",0.0}});
        require(std::signbit(read(signed_zero,0)) && !std::signbit(read(signed_zero,4)) && std::signbit(read(signed_zero,8)),"Копирование сохраняет знаки нуля");
        const auto zero_output=decode(reflected,signed_zero.bytes);
        require(std::signbit(zero_output.at("R").get<double>()) && !std::signbit(zero_output.at("G").get<double>()),"Decode сохраняет знаки нуля");
        const auto rounded=encode(reflected,{{"R",1.0+std::numeric_limits<float>::epsilon()/4.0},
            {"G",static_cast<double>(std::numeric_limits<float>::denorm_min())/4.0},
            {"B",-static_cast<double>(std::numeric_limits<float>::denorm_min())/4.0},{"A",0.25}});
        require(read(rounded,0)==1.0F && read(rounded,4)==0.0F && read(rounded,8)==0.0F && std::signbit(read(rounded,8)),
                "Обычное округление float32 допустимо и сохраняет знак underflow");
        {auto source=ordinary;const auto before=encode(reflected,source);source["R"]=200;const auto after=encode(reflected,source);
            require(read(before,0)==1.25F && read(after,0)==200.0F,"EncodedLinearColor владеет независимой копией RGBA");}
        {std::array<int,4> order{0,1,2,3};const auto expected=encode(reflected,ordinary);
            do {auto reordered=reflected;for(int i=0;i<4;++i)reordered.components[static_cast<std::size_t>(i)]=reflected.components[static_cast<std::size_t>(order[static_cast<std::size_t>(i)])];
                const auto actual=encode(reordered,ordinary);
                require(actual.bytes==expected.bytes && decode(reordered,actual.bytes)==decode(reflected,expected.bytes),"Порядок reflection не меняет расположение RGBA в POD");
            } while(std::next_permutation(order.begin(),order.end()));}

        for(const auto& bad:std::vector<json>{nullptr,true,false,0,1.5,"RGBA",json::array(),json::array({1,2,3,4}),json::object(),
                {{"R",1},{"G",2},{"B",3}},{{"R",1},{"G",2},{"B",3},{"A",4},{"Extra",5}},
                {{"r",1},{"G",2},{"B",3},{"A",4}},{{"X",1},{"Y",2},{"Z",3},{"A",4}}})
            reject("invalid_argument",[&]{encode(reflected,bad);});
        for(const auto& component:reflected.components) {
            auto missing=ordinary;missing.erase(component.name);reject("invalid_argument",[&]{encode(reflected,missing);});
            for(const auto& bad:std::vector<json>{nullptr,true,false,"1",json::array({1}),json::object(),
                    std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity(),
                    maximum*2,-maximum*2,std::nextafter(maximum,std::numeric_limits<double>::infinity()),
                    std::nextafter(-maximum,-std::numeric_limits<double>::infinity())}) {
                auto value=ordinary;value[component.name]=bad;reject("invalid_argument",[&]{encode(reflected,value);});
            }
        }
        {const auto integers=encode(reflected,{{"R",std::numeric_limits<std::uint64_t>::max()},{"G",std::numeric_limits<std::int64_t>::min()},{"B",0},{"A",1}});
            require(std::isfinite(read(integers,0)) && std::isfinite(read(integers,4)) && read(integers,0)>1 && read(integers,4)<0,
                    "Крупные целые в диапазоне float32 проходят обычное округление без clamp");}
        {const auto encoded=encode(reflected,ordinary);const auto output=decode(reflected,encoded.bytes);
            require(output.at("R")==1.25 && output.at("G")==-2.5 && output.at("B")==3.75 && output.at("A")==-0.5,"Decode не применяет гамму или диапазон 0..1");}
        for(const auto length:std::array<std::size_t,5>{0,4,15,17,32}) {
            std::array<std::byte,32> wrong{};reject("invalid_struct_copy",[&]{decode(reflected,std::span(wrong).first(length));});
        }
        for(const auto& component:reflected.components)for(const float value:std::array<float,3>{std::numeric_limits<float>::quiet_NaN(),
                std::numeric_limits<float>::infinity(),-std::numeric_limits<float>::infinity()}) {
            auto encoded=encode(reflected,ordinary);std::memcpy(encoded.bytes.data()+component.offset,&value,sizeof(value));
            reject("invalid_struct_value",[&]{decode(reflected,encoded.bytes);});
        }
        {auto bad=reflected;bad.min_alignment=1;reject("unsupported_linear_color_layout",[&]{encode(bad,nullptr);});
            reject("unsupported_linear_color_layout",[&]{decode(bad,{});});}
        {auto bad=reflected;std::swap(bad.components[0].offset,bad.components[1].offset);reject("unsupported_linear_color_layout",[&]{encode(bad,ordinary);});}
        std::cout<<"linear_color_argument: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
