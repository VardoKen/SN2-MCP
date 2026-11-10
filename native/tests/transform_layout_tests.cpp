#include <sn2_mcp/transform_layout.hpp>
#include <algorithm>
#include <array>
#include <cstring>
#include <iostream>
#include <map>
#include <stdexcept>
#include <vector>

namespace {
using nlohmann::json;
using sn2_mcp::transform_layout::Layout;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ отражённой раскладки Transform");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Отказ раскладки должен явно исключать исполнение");return;
    }
    throw std::runtime_error("Ожидался отказ раскладки Transform");
}
Layout layout() {
    return {96,16,false,{{"Rotation","StructProperty",32,0,1},{"Translation","StructProperty",24,32,1},
                        {"Scale3D","StructProperty",24,64,1}},
            {32,16,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},
                          {"Z","DoubleProperty",8,16,1},{"W","DoubleProperty",8,24,1}}},
            {24,8,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},{"Z","DoubleProperty",8,16,1}}},
            {24,8,false,{{"X","DoubleProperty",8,0,1},{"Y","DoubleProperty",8,8,1},{"Z","DoubleProperty",8,16,1}}}};
}
void validate(const Layout& input) {sn2_mcp::transform_layout::validate("/Script/CoreUObject.Transform","Transform",input);}

struct ReflectedMemory {
    static constexpr std::uintptr_t outer=0x1000,struct_class=0x2000,transform=0x3000,metaclass=0x4000,
        quat=0x5000,vector=0x6000,double_class=0x7000,members=0x8000,quat_fields=0x9000,vector_fields=0xa000;
    std::map<std::uintptr_t,std::array<std::byte,8>> storage;
    std::map<std::uintptr_t,int> reads;
    std::map<std::uintptr_t,sn2_mcp::ObjectSerialIdentity> identities;
    std::map<std::uintptr_t,std::string> paths;
    int class_checks{};
    template<class T>void set(std::uintptr_t address,T value) {
        auto& bytes=storage[address];bytes.fill(std::byte{});std::memcpy(bytes.data(),&value,sizeof(value));
    }
    template<class T>T read(std::uintptr_t address) {
        ++reads[address];auto found=storage.find(address);
        if(found==storage.end())throw std::runtime_error("Непредусмотренное чтение fake Transform memory");
        T value{};std::memcpy(&value,found->second.data(),sizeof(value));return value;
    }
    void field(std::uintptr_t address,std::uintptr_t type,std::uint64_t name,std::uintptr_t next,
               int size,int offset,std::uintptr_t nested=0) {
        set(address+8,type);set(address+0x10,std::uint64_t{0x3330});set(address+0x18,next);set(address+0x20,name);
        set(address+0x30,std::int32_t{1});set(address+0x34,std::int32_t{size});set(address+0x38,std::uint64_t{0x80});
        set(address+0x44,std::int32_t{offset});set(address+0x70,nested);
    }
    ReflectedMemory() {
        set(struct_class,std::uint64_t{1});set(double_class,std::uint64_t{2});
        field(outer,struct_class,99,0,96,0,transform);
        for(const auto address:{transform,quat,vector}) {
            set(address+0x40,std::uintptr_t{0});
            identities[address]={address,metaclass,static_cast<std::int32_t>(address),
                address==transform?10ULL:address==quat?11ULL:12ULL,0,9,40,0};
        }
        paths={{transform,"/Script/CoreUObject.Transform"},{quat,"/Script/CoreUObject.Quat"},{vector,"/Script/CoreUObject.Vector"}};
        set(transform+0x50,members);set(transform+0x58,std::int32_t{96});set(transform+0x5c,std::int32_t{0xA50000|(16)});
        set(quat+0x50,quat_fields);set(quat+0x58,std::int32_t{32});set(quat+0x5c,std::int32_t{0xA50000|(16)});
        set(vector+0x50,vector_fields);set(vector+0x58,std::int32_t{24});set(vector+0x5c,std::int32_t{0xA50000|(8)});
        field(members,struct_class,20,members+0x100,32,0,quat);
        field(members+0x100,struct_class,21,members+0x200,24,32,vector);
        field(members+0x200,struct_class,22,0,24,64,vector);
        for(int index=0;index<4;++index)field(quat_fields+index*0x100,double_class,30+index,
            index==3?0:quat_fields+(index+1)*0x100,8,index*8);
        for(int index=0;index<3;++index)field(vector_fields+index*0x100,double_class,30+index,
            index==2?0:vector_fields+(index+1)*0x100,8,index*8);
    }
    sn2_mcp::transform_layout::Snapshot capture(int size=96,int dim=1) {
        return sn2_mcp::transform_layout::capture(outer,size,dim,
            [this]<class T>(std::uintptr_t address){return read<T>(address);},
            [](std::uint64_t value)->std::string {
                switch(value) {
                    case 1:case 101:return "StructProperty";case 2:case 102:return "DoubleProperty";
                    case 10:return "Transform";case 11:return "Quat";case 12:return "Vector";
                    case 20:case 120:return "Rotation";case 21:return "Translation";case 22:return "Scale3D";
                    case 30:case 130:return "X";case 31:return "Y";case 32:return "Z";case 33:return "W";
                    default:return "Other";
                }
            },[this](std::uintptr_t address){require(identities.contains(address),"Identity запрошена для неподтверждённой структуры");return identities.at(address);},
            [this](std::uintptr_t address){++class_checks;require(address==metaclass,"ClassValidator получил чужой metaclass");},
            [this](std::uintptr_t address){require(paths.contains(address),"Path запрошен для неподтверждённой структуры");return paths.at(address);});
    }
};
}

int main() {
    using namespace sn2_mcp;
    try {
        const auto canonical=layout();validate(canonical);++checks;
        std::vector<int> order={0,1,2};
        do {auto changed=canonical;changed.members={canonical.members[order[0]],canonical.members[order[1]],canonical.members[order[2]]};validate(changed);++checks;}
        while(std::next_permutation(order.begin(),order.end()));
        auto reordered=canonical;std::swap(reordered.rotation.components[0].offset,reordered.rotation.components[3].offset);
        std::reverse(reordered.rotation.components.begin(),reordered.rotation.components.end());validate(reordered);++checks;
        for(const int alignment:{0,-1,3,32}) {auto invalid=canonical;invalid.min_alignment=alignment;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.min_alignment=8;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto accepted=canonical;accepted.min_alignment=8;accepted.rotation.min_alignment=8;validate(accepted);++checks;}
        for(const int size:{0,88,95,97,128}) {auto invalid=canonical;invalid.properties_size=size;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.has_super=true;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        for(int index=0;index<3;++index) {
            for(const int offset:{-1,1,8,96}) {auto invalid=canonical;invalid.members[index].offset=offset;reject("unsupported_transform_layout",[&]{validate(invalid);});}
            for(const int size:{0,1,23,31,96}) {auto invalid=canonical;invalid.members[index].element_size=size;reject("unsupported_transform_layout",[&]{validate(invalid);});}
            for(const int dim:{0,-1,2}) {auto invalid=canonical;invalid.members[index].array_dim=dim;reject("unsupported_transform_layout",[&]{validate(invalid);});}
            auto invalid=canonical;invalid.members[index].type="ObjectProperty";reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.members[index].name="Other";reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.members[index].name=canonical.members[(index+1)%3].name;reject("unsupported_transform_layout",[&]{validate(invalid);});
        }
        for(int index=0;index<4;++index) {
            auto invalid=canonical;invalid.rotation.components[index].type="FloatProperty";reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.rotation.components[index].element_size=4;reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.rotation.components[index].array_dim=2;reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.rotation.components[index].offset=32;reject("unsupported_transform_layout",[&]{validate(invalid);});
            invalid=canonical;invalid.rotation.components[index].name="Other";reject("unsupported_transform_layout",[&]{validate(invalid);});
        }
        {auto invalid=canonical;invalid.rotation.components.pop_back();reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.rotation.has_super=true;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.translation.properties_size=12;reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.scale3d.components[0].type="FloatProperty";reject("unsupported_transform_layout",[&]{validate(invalid);});}
        {auto invalid=canonical;invalid.members.push_back({"Extra","StructProperty",8,88,1});reject("unsupported_transform_layout",[&]{validate(invalid);});}
        for(const std::string& path:std::vector<std::string>{"", "Transform", "/Script/Engine.Transform", "/Script/CoreUObject.TransformChild",std::string("/Script/CoreUObject.Transform\0",30)})
            reject("unsupported_transform_type",[&]{transform_layout::validate(path,"Transform",canonical);});
        for(const std::string& name:std::vector<std::string>{"", "transform", "FTransform", "TransformChild"})
            reject("unsupported_transform_type",[&]{transform_layout::validate("/Script/CoreUObject.Transform",name,canonical);});

        {ReflectedMemory memory;const auto before=memory.capture();require(before.layout==canonical,"Capture должен сохранить каноническую раскладку");
            require(before.translation.structure==before.scale3d.structure && memory.class_checks==4,"Shared Vector должен быть допустим для Translation и Scale3D");
            require(memory.capture()==before,"Неизменный snapshot должен сравниваться равным");
            memory.identities[ReflectedMemory::quat].serial=1;require(memory.capture()!=before,"Изменение serial Quat должно менять snapshot");}
        {ReflectedMemory memory;const auto before=memory.capture();memory.set(ReflectedMemory::members+0x20,std::uint64_t{120});
            const auto alias=memory.capture();require(alias!=before && alias.layout==before.layout,"Raw member name должен обнаруживаться даже при одинаковом декодировании");}
        {ReflectedMemory memory;const auto before=memory.capture();memory.set(ReflectedMemory::double_class,std::uint64_t{102});
            const auto alias=memory.capture();require(alias!=before && alias.layout==before.layout,"Raw nested type name должен входить в snapshot");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::members+0x100+0x18,ReflectedMemory::members);
            reject("unsupported_transform_layout",[&]{memory.capture();});require(memory.reads[ReflectedMemory::members+8]==1,"Цикл должен остановиться до повторного чтения member");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::members+0x200+0x18,ReflectedMemory::members+0x300);
            reject("unsupported_transform_layout",[&]{memory.capture();});require(!memory.reads.contains(ReflectedMemory::members+0x300+8),"Четвёртое поле не должно читаться");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::quat_fields+0x300+0x18,ReflectedMemory::quat_fields+0x400);
            reject("unsupported_transform_layout",[&]{memory.capture();});require(!memory.reads.contains(ReflectedMemory::quat_fields+0x400+8),"Пятый компонент Quat не должен читаться");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x34,std::int32_t{88});reject("unsupported_transform_layout",[&]{memory.capture();});
            require(!memory.reads.contains(ReflectedMemory::outer+0x70),"Неверный внешний размер отклоняется до Struct pointer");}
        {ReflectedMemory memory;reject("unsupported_transform_layout",[&]{memory.capture(96,2);});require(memory.reads.empty(),"Неверный ArrayDim отклоняется до чтений");}
        {ReflectedMemory memory;memory.paths[ReflectedMemory::quat]="/Script/Engine.Quat";reject("unsupported_transform_type",[&]{memory.capture();});
            require(!memory.reads.contains(ReflectedMemory::quat_fields+8),"Не-Quat должен отклоняться до компонентов");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::members+0x100+0x70,ReflectedMemory::transform);reject("unsupported_transform_layout",[&]{memory.capture();});}
        std::cout<<"transform_layout: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
