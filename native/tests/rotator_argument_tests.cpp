#include <g1r_mcp/rotator_arguments.hpp>

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
using g1r_mcp::rotator_arguments::Layout;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ аргумента FRotator");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Проверка собственного POD не исполняет игровой вызов");return;
    }
    throw std::runtime_error("Ожидался отказ аргумента FRotator");
}
Layout canonical() {return {24,8,false,{{"Pitch",0,{"DoubleProperty",8,1}},
    {"Yaw",8,{"DoubleProperty",8,1}},{"Roll",16,{"DoubleProperty",8,1}}}};}
double at(const std::array<std::byte,24>& bytes,int offset) {double value{};std::memcpy(&value,bytes.data()+offset,sizeof(value));return value;}
g1r_mcp::rotator_arguments::Snapshot snapshot() {
    g1r_mcp::rotator_arguments::Snapshot result;
    result.path="/Script/CoreUObject.Rotator";result.struct_name="Rotator";
    result.element_size=24;result.array_dim=1;result.layout=canonical();return result;
}
}

int main() {
    using namespace g1r_mcp;
    try {
        const auto layout=canonical();const json ordinary={{"Pitch",725.25},{"Yaw",-1080.5},{"Roll",540.125}};
        const auto encoded=rotator_arguments::encode(layout,ordinary);
        require(encoded.size()==24 && at(encoded,0)==725.25 && at(encoded,8)==-1080.5 && at(encoded,16)==540.125,
                "FRotator копирует три double без нормализации градусов");
        require(rotator_arguments::decode(layout,encoded)==ordinary,"Return сохраняет исходные градусы за пределами полного оборота");
        for(const auto& value:std::vector<json>{{{"Pitch",-3},{"Yaw",4},{"Roll",0}},
                {{"Pitch",std::numeric_limits<double>::max()},{"Yaw",-std::numeric_limits<double>::max()},{"Roll",1.0e100}},
                {{"Pitch",1.0+std::numeric_limits<double>::epsilon()},{"Yaw",std::numeric_limits<double>::denorm_min()},{"Roll",-1.0e-100}}}) {
            const auto bytes=rotator_arguments::encode(layout,value);const auto decoded=rotator_arguments::decode(layout,bytes);
            for(const char* name:{"Pitch","Yaw","Roll"})require(decoded.at(name).get<double>()==value.at(name).get<double>(),
                    "FRotator не вводит лимит угла, float32-округление или quaternion-нормализацию");
        }
        const auto zero=rotator_arguments::encode(layout,{{"Pitch",-0.0},{"Yaw",0.0},{"Roll",-0.0}});
        const auto zero_result=rotator_arguments::decode(layout,zero);
        require(std::signbit(at(zero,0)) && !std::signbit(at(zero,8)) && std::signbit(at(zero,16)),"Входная POD-копия сохраняет знаки нуля");
        require(std::signbit(zero_result.at("Pitch").get<double>()) && !std::signbit(zero_result.at("Yaw").get<double>()) &&
                std::signbit(zero_result.at("Roll").get<double>()),"Возвращаемые значения сохраняют знаки нуля");
        {auto reordered=layout;reordered.components[0].offset=16;reordered.components[1].offset=0;reordered.components[2].offset=8;
            std::reverse(reordered.components.begin(),reordered.components.end());const auto bytes=rotator_arguments::encode(reordered,ordinary);
            require(at(bytes,16)==725.25 && at(bytes,0)==-1080.5 && at(bytes,8)==540.125,"Вход размещается по отражённым offsets, без жёсткого Pitch/Yaw/Roll порядка");
            require(rotator_arguments::decode(reordered,bytes)==ordinary,"Возврат использует ту же перестановку отражённых offsets");}
        {auto raw=encoded;const auto before=raw;const auto decoded=rotator_arguments::decode(layout,raw);
            require(raw==before,"Decode не меняет собственную копию результата");raw.fill(std::byte{});
            require(decoded==ordinary,"JSON return владеет независимыми значениями после изменения исходного буфера");}
        {auto source=ordinary;const auto before=rotator_arguments::encode(layout,source);source["Yaw"]=10;
            require(at(before,8)==-1080.5,"POD аргумент владеет независимой копией исходного JSON");}
        {std::array<std::byte,26> storage{};storage.fill(std::byte{0xa5});std::copy(encoded.begin(),encoded.end(),storage.begin()+1);
            const auto before=storage;
            require(rotator_arguments::decode(layout,std::span<const std::byte>(storage).subspan(1,24))==ordinary && storage==before,
                    "Невыровненный return читается копированием, соседние байты сохраняются");}

        rotator_arguments::validate(snapshot());++checks;
        {auto valid=snapshot();valid.layout.components[0].offset=16;valid.layout.components[2].offset=0;rotator_arguments::validate(valid);++checks;}
        {auto bad=snapshot();bad.path="/Script/G1R.Rotator";reject("unsupported_rotator_type",[&]{rotator_arguments::validate(bad);});}
        {auto bad=snapshot();bad.struct_name="Rotation";reject("unsupported_rotator_type",[&]{rotator_arguments::validate(bad);});}
        {auto bad=layout;bad.components.front().layout={"FloatProperty",4,1};reject("unsupported_rotator_layout",[&]{rotator_arguments::encode(bad,ordinary);});}
        {auto bad=layout;bad.components.pop_back();reject("unsupported_rotator_layout",[&]{rotator_arguments::encode(bad,ordinary);});}
        {auto bad=layout;bad.components.back().name="Z";reject("unsupported_rotator_layout",[&]{rotator_arguments::encode(bad,ordinary);});}
        {auto bad=layout;bad.components[1].offset=0;reject("unsupported_rotator_layout",[&]{rotator_arguments::decode(bad,encoded);});}
        for(const auto& bad:std::vector<json>{nullptr,true,"Pitch",json::array({1,2,3}),json::object(),
                {{"Pitch",1},{"Yaw",2}},{{"Pitch",1},{"Yaw",2},{"Roll",3},{"Extra",4}},
                {{"pitch",1},{"Yaw",2},{"Roll",3}}})reject("invalid_argument",[&]{rotator_arguments::encode(layout,bad);});
        for(const char* name:{"Pitch","Yaw","Roll"})for(const auto& bad:std::vector<json>{true,nullptr,"1",json::array(),
                std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity()}) {
            auto value=ordinary;value[name]=bad;reject("invalid_argument",[&]{rotator_arguments::encode(layout,value);});
        }
        for(const auto length:std::array<std::size_t,3>{0,23,25}) {
            std::array<std::byte,25> bytes{};reject("invalid_struct_copy",[&]{rotator_arguments::decode(layout,std::span(bytes).first(length));});
        }
        std::cout<<"rotator_argument: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
