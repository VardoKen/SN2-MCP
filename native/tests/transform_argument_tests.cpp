#include <g1r_mcp/transform_arguments.hpp>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <stdexcept>
#include <vector>

namespace {
using nlohmann::json;
using g1r_mcp::transform_layout::Layout;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ аргумента Transform");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Ошибка Transform должна явно исключать исполнение");return;
    }
    throw std::runtime_error("Ожидался отказ Transform");
}
Layout layout() {
    return {96,16,false,{{"Rotation","StructProperty",32,0,1},{"Translation","StructProperty",24,32,1},
                        {"Scale3D","StructProperty",24,64,1}},
            {32,16,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},
                          {"Z","DoubleProperty",8,16,1},{"W","DoubleProperty",8,24,1}}},
            {24,8,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},{"Z","DoubleProperty",8,16,1}}},
            {24,8,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},{"Z","DoubleProperty",8,16,1}}}};
}
json value() {
    return {{"Rotation",{{"X",0.0},{"Y",0.0},{"Z",0.0},{"W",1.0}}},
            {"Translation",{{"X",1.25},{"Y",-2.5},{"Z",3.75}}},
            {"Scale3D",{{"X",1.0},{"Y",2.0},{"Z",-3.0}}}};
}
double number(const g1r_mcp::transform_arguments::EncodedTransform& encoded,int offset) {
    double result{};std::memcpy(&result,encoded.bytes.data()+offset,sizeof(result));return result;
}
void copied_correctly(const Layout& layout,const json& source) {
    const auto encoded=g1r_mcp::transform_arguments::encode(layout,source);
    require(encoded.size==96,"Transform должен занимать ровно 96 байтов");
    for(const auto& component:layout.rotation.components)
        require(number(encoded,component.offset)==source["Rotation"][component.name].get<double>(),
                "Quat должен копироваться по отражённым offsets");
    for(const auto& component:layout.translation.components)
        require(number(encoded,32+component.offset)==source["Translation"][component.name].get<double>(),
                "Translation должен копироваться по отражённым offsets");
    for(const auto& component:layout.scale3d.components)
        require(number(encoded,64+component.offset)==source["Scale3D"][component.name].get<double>(),
                "Scale3D должен копироваться по отражённым offsets");
    for(int index=56;index<64;++index)require(encoded.bytes[index]==std::byte{},"Padding Translation должен быть нулевым");
    for(int index=88;index<96;++index)require(encoded.bytes[index]==std::byte{},"Padding Scale3D должен быть нулевым");
}
}

int main() {
    using namespace g1r_mcp;
    try {
        const auto canonical=layout();const auto identity=value();copied_correctly(canonical,identity);
        auto integers=identity;integers["Rotation"]={{"X",0},{"Y",0},{"Z",0},{"W",1}};
        integers["Translation"]={{"X",-4},{"Y",std::uint64_t{8}},{"Z",0}};
        integers["Scale3D"]={{"X",0},{"Y",-1},{"Z",2}};copied_correctly(canonical,integers);
        auto arbitrary=identity;arbitrary["Rotation"]={{"X",0.5},{"Y",-0.5},{"Z",0.5},{"W",-0.5}};
        copied_correctly(canonical,arbitrary);
        auto swapped=canonical;std::swap(swapped.rotation.components[0].offset,swapped.rotation.components[3].offset);
        std::swap(swapped.translation.components[0].offset,swapped.translation.components[2].offset);
        std::swap(swapped.scale3d.components[0].offset,swapped.scale3d.components[1].offset);
        std::reverse(swapped.members.begin(),swapped.members.end());
        std::reverse(swapped.rotation.components.begin(),swapped.rotation.components.end());
        copied_correctly(swapped,arbitrary);

        auto signed_zero=identity;signed_zero["Rotation"]["X"]=-0.0;signed_zero["Translation"]["Y"]=-0.0;
        const auto zero_encoded=transform_arguments::encode(canonical,signed_zero);
        require(std::signbit(number(zero_encoded,0)) && std::signbit(number(zero_encoded,40)),"Собственная копия должна сохранять знак нуля");
        auto finite=identity;finite["Translation"]={{"X",std::numeric_limits<double>::max()},
            {"Y",-std::numeric_limits<double>::max()},{"Z",std::numeric_limits<double>::denorm_min()}};
        finite["Scale3D"]={{"X",0.0},{"Y",-1.0},{"Z",std::numeric_limits<double>::max()}};
        copied_correctly(canonical,finite);
        const auto saved=transform_arguments::encode(canonical,identity);auto modified=identity;modified["Translation"]["X"]=100;
        const auto later=transform_arguments::encode(canonical,modified);
        require(number(saved,32)==1.25 && number(later,32)==100,"EncodedTransform должен владеть независимой копией");

        for(const auto& source:std::vector<json>{identity,arbitrary,finite}) {
            const auto encoded=transform_arguments::encode(canonical,source);
            require(transform_arguments::decode(canonical,encoded.bytes)==source,
                    "FTransform return сохраняет все десять компонентов известной раскладки");
        }
        const auto swapped_encoded=transform_arguments::encode(swapped,arbitrary);
        require(transform_arguments::decode(swapped,swapped_encoded.bytes)==arbitrary,
                "Return читает именованные поля по reflection offsets при изменённом порядке метаданных");
        const auto decoded_zero=transform_arguments::decode(canonical,zero_encoded.bytes);
        require(std::signbit(decoded_zero["Rotation"]["X"].get<double>()) &&
                std::signbit(decoded_zero["Translation"]["Y"].get<double>()),
                "Return сохраняет отрицательный ноль quaternion и вектора");
        {
            auto raw=transform_arguments::encode(canonical,identity);
            const double two=2.0,precise=1.0+std::numeric_limits<double>::epsilon(),tiny=std::numeric_limits<double>::denorm_min();
            std::memcpy(raw.bytes.data()+24,&two,sizeof(two));
            std::memcpy(raw.bytes.data()+32,&precise,sizeof(precise));
            std::memcpy(raw.bytes.data()+80,&tiny,sizeof(tiny));
            std::fill(raw.bytes.begin()+56,raw.bytes.begin()+64,std::byte{0xff});
            std::fill(raw.bytes.begin()+88,raw.bytes.end(),std::byte{0xff});
            const auto before=raw.bytes;
            const auto decoded=transform_arguments::decode(canonical,raw.bytes);
            require(decoded["Rotation"]["W"]==2.0 && decoded["Rotation"]["X"]==0.0,
                    "Сырой конечный quaternion return не проходит проверку нормализации входного аргумента");
            require(decoded["Translation"]["X"].get<double>()==precise && decoded["Scale3D"]["Z"].get<double>()==tiny,
                    "Return сохраняет точность double и subnormal без промежуточного float");
            auto expected=identity;expected["Rotation"]["W"]=two;expected["Translation"]["X"]=precise;expected["Scale3D"]["Z"]=tiny;
            require(decoded==expected && raw.bytes==before,"Padding с произвольными битами игнорируется, собственная копия остаётся неизменной");
            raw.bytes.fill(std::byte{});
            require(decoded==expected,"JSON return владеет независимыми значениями после изменения исходного буфера");
        }
        for(const char* group:{"Rotation","Translation","Scale3D"}) {
            const std::vector<std::string> keys=std::string(group)=="Rotation"?std::vector<std::string>{"X","Y","Z","W"}:
                std::vector<std::string>{"X","Y","Z"};
            for(const auto& key:keys) {
                for(const json& invalid_value:std::vector<json>{nullptr,true,false,"0",json::array(),json::object(),
                        std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity(),std::numeric_limits<double>::quiet_NaN()}) {
                    auto invalid=identity;invalid[group][key]=invalid_value;
                    reject("invalid_argument",[&]{transform_arguments::encode(canonical,invalid);});
                }
                auto missing=identity;missing[group].erase(key);
                reject("invalid_argument",[&]{transform_arguments::encode(canonical,missing);});
            }
            for(const json& nested:std::vector<json>{nullptr,true,1,"vector",json::array({1,2,3}),json::object()}) {
                auto invalid=identity;invalid[group]=nested;
                reject("invalid_argument",[&]{transform_arguments::encode(canonical,invalid);});
            }
            auto extra=identity;extra[group]["Extra"]=1;
            reject("invalid_argument",[&]{transform_arguments::encode(canonical,extra);});
            auto missing=identity;missing.erase(group);
            reject("invalid_argument",[&]{transform_arguments::encode(canonical,missing);});
        }
        for(const json& top:std::vector<json>{nullptr,true,1,"Transform",json::array(),json::object()})
            reject("invalid_argument",[&]{transform_arguments::encode(canonical,top);});
        auto extra=identity;extra["Matrix"]=1;reject("invalid_argument",[&]{transform_arguments::encode(canonical,extra);});
        auto invalid_layout=canonical;invalid_layout.properties_size=88;
        reject("unsupported_transform_layout",[&]{transform_arguments::encode(invalid_layout,nullptr);});
        invalid_layout=canonical;invalid_layout.rotation.components[0].type="FloatProperty";
        reject("unsupported_transform_layout",[&]{transform_arguments::encode(invalid_layout,identity);});
        std::cout<<"transform_arguments: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
