#include <g1r_mcp/vector_arguments.hpp>

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
int checks{};
void require(bool good,const char* message) {
    ++checks;
    if(!good)throw std::runtime_error(message);
}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный код отказа FVector");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Отказ FVector должен быть до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ FVector");
}
g1r_mcp::struct_values::Layout layout(int width) {
    return {width*3,width,false,{{"Z",width==4?"FloatProperty":"DoubleProperty",width,0,1},
                               {"X",width==4?"FloatProperty":"DoubleProperty",width,width*2,1},
                               {"Y",width==4?"FloatProperty":"DoubleProperty",width,width,1}}};
}
template<class T>T read(const g1r_mcp::vector_arguments::EncodedVector& copied,int offset) {
    T result{};std::memcpy(&result,copied.bytes.data()+offset,sizeof(result));return result;
}
void check_float(const json& value) {
    const auto captured=g1r_mcp::vector_arguments::encode(layout(4),value);
    require(captured.size==12,"FVector Float должен занимать 12 байтов");
    require(read<float>(captured,8)==static_cast<float>(value["X"].get<double>())
        && read<float>(captured,4)==static_cast<float>(value["Y"].get<double>())
        && read<float>(captured,0)==static_cast<float>(value["Z"].get<double>()),
        "Компоненты Float должны копироваться по отражённым offsets");
    for(int index=12;index<24;++index)require(captured.bytes[index]==std::byte{},"Хвост Float FVector должен оставаться нулевым");
}
void check_double(const json& value) {
    const auto captured=g1r_mcp::vector_arguments::encode(layout(8),value);
    require(captured.size==24,"FVector Double должен занимать 24 байта");
    require(read<double>(captured,16)==value["X"].get<double>()
        && read<double>(captured,8)==value["Y"].get<double>()
        && read<double>(captured,0)==value["Z"].get<double>(),
        "Компоненты Double должны копироваться по отражённым offsets");
}
}

int main() {
    using namespace g1r_mcp;
    try {
        const auto floats=layout(4),doubles=layout(8);
        const json ordinary={{"X",1.25},{"Y",-2.5},{"Z",3.75}};
        check_float(ordinary);check_double(ordinary);
        const json integers={{"X",std::int64_t{-3}},{"Y",std::uint64_t{2}},{"Z",0}};
        check_float(integers);check_double(integers);
        const json negatives={{"X",-0.0},{"Y",0.0},{"Z",-0.0}};
        const auto negative_float=vector_arguments::encode(floats,negatives);
        const auto negative_double=vector_arguments::encode(doubles,negatives);
        require(std::signbit(read<float>(negative_float,8)) && !std::signbit(read<float>(negative_float,4)),
                "Float не должен терять знак нуля");
        require(std::signbit(read<double>(negative_double,16)) && !std::signbit(read<double>(negative_double,8)),
                "Double не должен терять знак нуля");

        constexpr auto float_max=static_cast<double>(std::numeric_limits<float>::max());
        constexpr auto double_max=std::numeric_limits<double>::max();
        check_float({{"X",float_max},{"Y",-float_max},{"Z",static_cast<double>(std::numeric_limits<float>::denorm_min())}});
        check_double({{"X",double_max},{"Y",-double_max},{"Z",std::numeric_limits<double>::denorm_min()}});
        const json rounded={{"X",1.0+std::numeric_limits<float>::epsilon()/4.0},
                            {"Y",static_cast<double>(std::numeric_limits<float>::denorm_min())/4.0},
                            {"Z",-static_cast<double>(std::numeric_limits<float>::denorm_min())/4.0}};
        const auto rounding=vector_arguments::encode(floats,rounded);
        require(read<float>(rounding,8)==1.0F,"Float использует нормальное округление, не отказ за потерю точности");
        require(read<float>(rounding,4)==0.0F && read<float>(rounding,0)==0.0F,
                "Допустимо обычное округление очень малого Float к нулю");
        require(std::signbit(read<float>(rounding,0)),"Округление отрицательного Float к нулю сохраняет знак");
        const auto before=vector_arguments::encode(doubles,ordinary);
        auto changed=ordinary;changed["X"]=200;
        const auto after=vector_arguments::encode(doubles,changed);
        require(read<double>(before,16)==1.25 && read<double>(after,16)==200,
                "EncodedVector должен владеть независимой копией значений");

        for(const auto& reflected:std::vector<struct_values::Layout>{floats,doubles}) {
            const auto encoded=vector_arguments::encode(reflected,ordinary);
            const auto output=vector_arguments::decode(reflected,std::span(encoded.bytes).first(encoded.size));
            require(output.is_object() && output.size()==3 && output["X"]==1.25 && output["Y"]==-2.5 && output["Z"]==3.75,
                    "FVector decode должен возвращать точно XYZ из отражённых offsets");
            auto reordered=reflected;std::swap(reordered.components[0],reordered.components[2]);
            require(vector_arguments::decode(reordered,std::span(encoded.bytes).first(encoded.size))==output,
                    "Порядок цепочки reflected компонентов не меняет XYZ результат");
            for(const auto length:std::vector<std::size_t>{0,static_cast<std::size_t>(encoded.size-1),25}) {
                std::array<std::byte,25> wrong{};
                reject("invalid_struct_copy",[&]{vector_arguments::decode(reflected,std::span(wrong).first(length));});
            }
            // decode применяет memcpy, в том числе на входном диапазоне без выравнивания.
            std::array<std::byte,25> unaligned{};std::memcpy(unaligned.data()+1,encoded.bytes.data(),encoded.size);
            require(vector_arguments::decode(reflected,std::span(unaligned).subspan(1,encoded.size))==output,
                    "FVector decode не должен требовать выровненный адрес копии");
            for(const auto nonfinite:std::vector<double>{std::numeric_limits<double>::infinity(),
                    -std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}) {
                for(const auto& component:reflected.components) {
                    auto corrupted=encoded;
                    if(component.element_size==4) {
                        const auto value=static_cast<float>(nonfinite);
                        std::memcpy(corrupted.bytes.data()+component.offset,&value,sizeof(value));
                    } else std::memcpy(corrupted.bytes.data()+component.offset,&nonfinite,sizeof(nonfinite));
                    reject("invalid_struct_value",[&]{vector_arguments::decode(reflected,std::span(corrupted.bytes).first(corrupted.size));});
                }
            }
        }

        for(const char* key:{"X","Y","Z"}) {
            for(const json& wrong:std::vector<json>{nullptr,true,false,"1",json::array(),json::object(),
                    std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity(),
                    std::numeric_limits<double>::quiet_NaN()}) {
                auto invalid=ordinary;invalid[key]=wrong;
                reject("invalid_argument",[&]{vector_arguments::encode(floats,invalid);});
                reject("invalid_argument",[&]{vector_arguments::encode(doubles,invalid);});
            }
            auto missing=ordinary;missing.erase(key);
            reject("invalid_argument",[&]{vector_arguments::encode(floats,missing);});
            reject("invalid_argument",[&]{vector_arguments::encode(doubles,missing);});
            auto overflow=ordinary;overflow[key]=std::nextafter(float_max,std::numeric_limits<double>::infinity());
            reject("invalid_argument",[&]{vector_arguments::encode(floats,overflow);});
            check_double(overflow);
            overflow[key]=std::nextafter(-float_max,-std::numeric_limits<double>::infinity());
            reject("invalid_argument",[&]{vector_arguments::encode(floats,overflow);});
            check_double(overflow);
        }
        for(const json& shape:std::vector<json>{nullptr,true,1,1.0,"X=1,Y=2,Z=3",json::array({1,2,3}),
                json::object(),json{{"x",1},{"y",2},{"z",3}},
                json{{"X",1},{"Y",2},{"Z",3},{"W",4}},json{{"X",1},{"Y",2},{"Z",3},{"",4}},
                json{{"X",1},{"Y",2},{"Z",3},{std::string("W\0",2),4}}}) {
            reject("invalid_argument",[&]{vector_arguments::encode(floats,shape);});
            reject("invalid_argument",[&]{vector_arguments::encode(doubles,shape);});
        }
        // Некорректная раскладка отклоняется до попытки преобразовать заведомо неверный JSON.
        auto invalid_layout=floats;invalid_layout.components[0].offset=-4;
        reject("unsupported_vector_layout",[&]{vector_arguments::encode(invalid_layout,nullptr);});
        invalid_layout=floats;invalid_layout.components[0].element_size=8;
        reject("unsupported_vector_layout",[&]{vector_arguments::encode(invalid_layout,ordinary);});
        invalid_layout=floats;invalid_layout.components[0].name="Pitch";
        reject("unsupported_vector_layout",[&]{vector_arguments::encode(invalid_layout,ordinary);});
        const struct_values::Layout rotator{24,8,false,{{"Pitch","DoubleProperty",8,0,1},
            {"Yaw","DoubleProperty",8,8,1},{"Roll","DoubleProperty",8,16,1}}};
        reject("unsupported_vector_layout",[&]{vector_arguments::encode(rotator,ordinary);});
        reject("unsupported_vector_layout",[&]{vector_arguments::decode(rotator,std::span(before.bytes));});
        std::cout<<"vector_arguments: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
