#include <sn2_mcp/vector_layout.hpp>

#include <algorithm>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <stdexcept>
#include <vector>

namespace {
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ reflected FVector");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Отказ reflected FVector должен быть до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ reflected FVector");
}
sn2_mcp::struct_values::Layout layout(int width) {
    const std::string type=width==4?"FloatProperty":"DoubleProperty";
    return {width*3,width,false,{{"X",type,width,0,1},{"Y",type,width,width,1},{"Z",type,width,2*width,1}}};
}
void validate(const sn2_mcp::struct_values::Layout& value) {
    sn2_mcp::vector_layout::validate("/Script/CoreUObject.Vector","Vector",value);
}

struct ReflectedMemory {
    static constexpr std::uintptr_t outer=0x1000,outer_class=0x2000,structure=0x3000,
        structure_class=0x4000,scalar_class=0x5000;
    std::map<std::uintptr_t,std::array<std::byte,8>> storage;
    std::map<std::uintptr_t,int> reads;
    sn2_mcp::ObjectSerialIdentity identity{structure,structure_class,4,10,0,7,40,0};
    std::string path="/Script/CoreUObject.Vector";
    int class_checks{};
    template<class T>void set(std::uintptr_t address,T value) {
        auto& bytes=storage[address];bytes.fill(std::byte{});std::memcpy(bytes.data(),&value,sizeof(value));
    }
    template<class T>T read(std::uintptr_t address) {
        ++reads[address];T value{};auto found=storage.find(address);
        if(found==storage.end())throw std::runtime_error("Непредусмотренное чтение fake reflected memory");
        std::memcpy(&value,found->second.data(),sizeof(value));return value;
    }
    explicit ReflectedMemory(int width) {
        set(outer+8,outer_class);set(outer+0x10,std::uint64_t{0x9990});set(outer+0x20,std::uint64_t{90});
        set(outer+0x34,std::int32_t{3*width});set(outer+0x30,std::int32_t{1});set(outer+0x70,structure);
        set(outer_class,std::uint64_t{1});
        set(structure+0x50,std::uintptr_t{0x6000});set(structure+0x40,std::uintptr_t{0});
        set(structure+0x58,std::int32_t{3*width});set(structure+0x5c,std::int32_t{0xA50000|(width)});
        set(scalar_class,std::uint64_t{width==4?2ULL:3ULL});
        for(int index=0;index<3;++index) {
            const auto field=std::uintptr_t{0x6000}+static_cast<std::uintptr_t>(index)*0x100;
            set(field+8,scalar_class);set(field+0x10,std::uint64_t{0x3330});
            set(field+0x18,index==2?std::uintptr_t{0}:field+0x100);
            set(field+0x20,static_cast<std::uint64_t>(11+index));
            set(field+0x34,std::int32_t{width});set(field+0x44,std::int32_t{index*width});set(field+0x30,std::int32_t{1});
        }
    }
    sn2_mcp::vector_layout::Snapshot capture(int size=24,int dim=1) {
        const auto name=[](std::uint64_t value)->std::string {
            switch(value) {
                case 1:case 101:return "StructProperty";
                case 2:return "FloatProperty";
                case 3:case 103:return "DoubleProperty";
                case 10:return "Vector";
                case 11:case 111:return "X";
                case 12:return "Y";
                case 13:return "Z";
                default:return "Other";
            }
        };
        return sn2_mcp::vector_layout::capture(outer,size,dim,
            [this]<class T>(std::uintptr_t address){return read<T>(address);},name,
            [this](std::uintptr_t address){require(address==structure,"ObjectCapture получил неверный Struct");return identity;},
            [this](std::uintptr_t address){++class_checks;require(address==structure_class,"ClassValidator получил неверный metaclass");},
            [this](std::uintptr_t address){require(address==structure,"PathResolver получил неверный Struct");return path;});
    }
};
}

int main() {
    using namespace sn2_mcp;
    try {
        for(const int width:{4,8}) {
            const auto ordinary=layout(width);validate(ordinary);
            std::vector<int> order={0,1,2};
            do {
                auto reordered=ordinary;
                reordered.components={ordinary.components[order[0]],ordinary.components[order[1]],ordinary.components[order[2]]};
                validate(reordered);++checks;
            } while(std::next_permutation(order.begin(),order.end()));
            // Компоненты могут лежать в другом отражённом порядке, но должны покрыть все байты ровно раз.
            auto offsets=ordinary;std::swap(offsets.components[0].offset,offsets.components[2].offset);
            validate(offsets);++checks;
            for(const int alignment:std::vector<int>{1,2,4}) {
                auto allowed=ordinary;allowed.min_alignment=alignment;validate(allowed);++checks;
            }
            if(width==8){auto allowed=ordinary;allowed.min_alignment=8;validate(allowed);++checks;}
            for(const int alignment:std::vector<int>{0,-1,3,width*2}) {
                auto changed=ordinary;changed.min_alignment=alignment;
                reject("unsupported_vector_layout",[&]{validate(changed);});
            }
            for(const int size:std::vector<int>{0,-1,width*3-1,width*3+1,4096}) {
                auto changed=ordinary;changed.properties_size=size;
                reject("unsupported_vector_layout",[&]{validate(changed);});
            }
            for(const int index:{0,1,2}) {
                for(const int offset:std::vector<int>{-width,1,width*3,width*3+width}) {
                    auto changed=ordinary;changed.components[index].offset=offset;
                    reject("unsupported_vector_layout",[&]{validate(changed);});
                }
                auto changed=ordinary;changed.components[index].offset=ordinary.components[(index+1)%3].offset;
                reject("unsupported_vector_layout",[&]{validate(changed);});
                for(const int size:std::vector<int>{0,-1,width-1,width+1,width*2}) {
                    changed=ordinary;changed.components[index].element_size=size;
                    reject("unsupported_vector_layout",[&]{validate(changed);});
                }
                for(const int dim:std::vector<int>{0,-1,2,32}) {
                    changed=ordinary;changed.components[index].array_dim=dim;
                    reject("unsupported_vector_layout",[&]{validate(changed);});
                }
                for(const std::string& type:std::vector<std::string>{"IntProperty",width==4?"DoubleProperty":"FloatProperty", "StructProperty",""}) {
                    changed=ordinary;changed.components[index].type=type;
                    reject("unsupported_vector_layout",[&]{validate(changed);});
                }
                for(const std::string& name:std::vector<std::string>{"W","x","Pitch","",std::string("X\0",2)}) {
                    changed=ordinary;changed.components[index].name=name;
                    reject("unsupported_vector_layout",[&]{validate(changed);});
                }
                changed=ordinary;changed.components[index].name=ordinary.components[(index+1)%3].name;
                reject("unsupported_vector_layout",[&]{validate(changed);});
            }
            auto changed=ordinary;changed.has_super=true;
            reject("unsupported_vector_layout",[&]{validate(changed);});
            changed=ordinary;changed.components.pop_back();
            reject("unsupported_vector_layout",[&]{validate(changed);});
            changed=ordinary;changed.components.push_back({"W",width==4?"FloatProperty":"DoubleProperty",width,width*3,1});
            reject("unsupported_vector_layout",[&]{validate(changed);});
        }
        const auto vector=layout(8);
        for(const std::string& path:std::vector<std::string>{"", "Vector","/Script/Engine.Vector", "CoreUObject.Vector",
                "/Script/CoreUObject.Rotator","/Script/CoreUObject.VectorChild","/Script/CoreUObject.Vector ",
                std::string("/Script/CoreUObject.Vector\0",27)}) {
            reject("unsupported_vector_type",[&]{vector_layout::validate(path,"Vector",vector);});
        }
        for(const std::string& name:std::vector<std::string>{"", "vector","FVector","VectorChild","Rotator",std::string("Vector\0",7)}) {
            reject("unsupported_vector_type",[&]{vector_layout::validate("/Script/CoreUObject.Vector",name,vector);});
        }
        const struct_values::Layout rotator{24,8,false,{{"Pitch","DoubleProperty",8,0,1},
            {"Yaw","DoubleProperty",8,8,1},{"Roll","DoubleProperty",8,16,1}}};
        reject("unsupported_vector_layout",[&]{validate(rotator);});

        for(const int width:{4,8}) {
            ReflectedMemory memory(width);const auto before=memory.capture(width*3);
            require(before.layout==layout(width) && before.components.size()==3 && memory.class_checks==1,
                    "Capture должен сохранять подтверждённую reflected layout и проверять class");
            require(before.identity.serial==0 && before.identity.class_serial==0,
                    "Наблюдаемые нулевые serial допустимы без выдуманного назначения");
            require(memory.capture(width*3)==before,"Неизменный capture должен сравниваться равным");
            memory.identity.serial=1;
            require(memory.capture(width*3)!=before,"Переход serial с 0 на 1 должен менять snapshot");
            memory.identity.serial=0;memory.set(ReflectedMemory::outer+0x10,std::uint64_t{0x9991});
            require(memory.capture(width*3)!=before,"FFieldVariant owner входит в snapshot");
            memory.set(ReflectedMemory::outer+0x10,std::uint64_t{0x9990});memory.set(ReflectedMemory::outer_class,std::uint64_t{101});
            const auto alias=memory.capture(width*3);
            require(alias!=before && alias.layout==before.layout,"Смена raw type name должна обнаруживаться даже при одинаковом декодировании");
        }
        {
            ReflectedMemory memory(8);const auto before=memory.capture();
            memory.set(std::uintptr_t{0x6000}+0x20,std::uint64_t{111});
            const auto alias=memory.capture();
            require(alias!=before && alias.layout==before.layout,"Raw component name входит в snapshot отдельно от строки");
        }
        {
            ReflectedMemory memory(8);memory.set(std::uintptr_t{0x6100}+0x18,std::uintptr_t{0x6000});
            reject("unsupported_vector_layout",[&]{memory.capture();});
            require(memory.reads[std::uintptr_t{0x6000}+8]==1,"Цикл должен останавливаться до повторного чтения поля");
        }
        {
            ReflectedMemory memory(8);memory.set(std::uintptr_t{0x6200}+0x18,std::uintptr_t{0x6300});
            reject("unsupported_vector_layout",[&]{memory.capture();});
            require(memory.reads.find(std::uintptr_t{0x6300}+8)==memory.reads.end(),"Четвёртый компонент не должен читаться за пределом бюджета");
        }
        {
            ReflectedMemory memory(8);memory.set(ReflectedMemory::outer+0x34,std::int32_t{12});
            reject("unsupported_vector_layout",[&]{memory.capture();});
            require(memory.reads.find(ReflectedMemory::outer+0x70)==memory.reads.end(),"Внешний неверный размер должен остановить capture до Struct pointer");
        }
        {
            ReflectedMemory memory(8);
            reject("unsupported_vector_layout",[&]{memory.capture(24,2);});
            require(memory.reads.empty(),"Неверный ArrayDim должен отклоняться до чтений");
            memory.path="/Script/Engine.Vector";
            reject("unsupported_vector_type",[&]{memory.capture();});
            require(memory.reads.find(std::uintptr_t{0x6000}+8)==memory.reads.end(),"Чужой path должен отклоняться до компонентов");
        }
        {
            ReflectedMemory memory(8);memory.identity.class_serial=-1;
            reject("unsupported_vector_type",[&]{memory.capture();});
            require(memory.class_checks==0,"Некорректная serial identity не должна доходить до class validator");
        }
        require(vector_layout::checked_address(1,0,1)==1,"Минимальный адрес допустим");
        require(vector_layout::checked_address(std::numeric_limits<std::uintptr_t>::max(),0,1)==std::numeric_limits<std::uintptr_t>::max(),
                "Последний байт адресного пространства допустим без переполнения");
        reject("invalid_memory",[]{vector_layout::checked_address(0,0,1);});
        reject("invalid_memory",[]{vector_layout::checked_address(std::numeric_limits<std::uintptr_t>::max(),1,1);});
        reject("invalid_memory",[]{vector_layout::checked_address(std::numeric_limits<std::uintptr_t>::max(),0,2);});
        std::cout<<"vector_layout: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
