#include <g1r_mcp/struct_values.hpp>
#include <algorithm>
#include <array>
#include <iostream>
#include <limits>
#include <stdexcept>

namespace {
int checks=0;
void require(bool condition,const char* message) {++checks;if(!condition)throw std::runtime_error(message);}
template<class Operation>void rejects(const char* code,Operation operation) {
    try {operation();}catch(const g1r_mcp::HandlerError& error){require(error.code()==code,"Неверный код отказа");return;}
    throw std::runtime_error("Ожидался отказ struct layout");
}
}
int main() {
    using namespace g1r_mcp::struct_values;
    try {
        const Layout vector{12,4,false,{{"X","FloatProperty",4,0,1},{"Y","FloatProperty",4,4,1},{"Z","FloatProperty",4,8,1}}};
        const std::array<float,3> values{1.25f,-2.5f,3.0f};
        auto decoded=copied_components(vector,std::as_bytes(std::span(values)));
        require(decoded.size()==3 && decoded[0]["value"]==1.25 && decoded[1]["value"]==-2.5,"Float Vector копия не подтверждена");
        Layout rotator{24,8,false,{{"Pitch","DoubleProperty",8,16,1},{"Yaw","DoubleProperty",8,0,1},{"Roll","DoubleProperty",8,8,1}}};
        const std::array<double,3> angles{10.0,20.0,30.0};
        decoded=copied_components(rotator,std::as_bytes(std::span(angles)));
        require(decoded[0]["value"]==30.0 && decoded[1]["value"]==10.0 && decoded[2]["value"]==20.0,"Порядок runtime offsets не соблюдён");
        auto valid=vector;valid.min_alignment=1;validate_layout(valid);
        require(valid==valid,"Копия метаданных должна сравниваться без изменения");
        auto changed=vector;changed.components[1].offset=8;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].name="X";
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].type="IntProperty";
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].element_size=8;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].array_dim=2;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].offset=-4;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].offset=2;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].offset=12;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components[1].name="W";
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.properties_size=16;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.has_super=true;
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components.pop_back();
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        changed=vector;changed.components.push_back({"W","FloatProperty",4,12,1});
        rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        for(int alignment:std::array{0,-1,3,8}) {
            changed=vector;changed.min_alignment=alignment;
            rejects("unsupported_struct_layout",[&]{validate_layout(changed);});
        }
        rejects("invalid_struct_copy",[&]{copied_components(vector,std::as_bytes(std::span(values)).first(8));});
        auto bad=values;bad[1]=std::numeric_limits<float>::infinity();
        rejects("invalid_struct_value",[&]{copied_components(vector,std::as_bytes(std::span(bad)));});
        bad=values;bad[2]=std::numeric_limits<float>::quiet_NaN();
        rejects("invalid_struct_value",[&]{copied_components(vector,std::as_bytes(std::span(bad)));});
        std::array<std::byte,13> unaligned{};
        std::memcpy(unaligned.data()+1,values.data(),12);
        require(copied_components(vector,std::span(unaligned).subspan(1))==copied_components(vector,std::as_bytes(std::span(values))),"Невыровненная копия должна читаться через memcpy");
        changed=vector;std::swap(changed.components[0],changed.components[1]);
        require(!(changed==vector),"Изменение порядка reflection должно обнаруживаться до выдачи результата");
        std::cout<<"Struct values: "<<checks<<" проверок прошли\n";return 0;
    }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
