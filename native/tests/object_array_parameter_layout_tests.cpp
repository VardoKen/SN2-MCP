#include <g1r_mcp/object_array_parameter_layout.hpp>

#include <array>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <vector>

namespace {
using g1r_mcp::ObjectSerialIdentity;
using nlohmann::json;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ раскладки объектного массива");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Проверка метаданных не должна приписывать исполнение игровому вызову");return;
    }
    throw std::runtime_error("Ожидался отказ раскладки объектного массива");
}
struct ReflectedMemory {
    static constexpr std::uintptr_t outer=0x1000,outer_class=0x2000,inner=0x3000,
        inner_class=0x4000,property_class=0x5000,metaclass=0x6000;
    static constexpr auto flags=g1r_mcp::parameter_direction::parm|g1r_mcp::parameter_direction::const_parm|
        g1r_mcp::parameter_direction::reference_parm;
    std::map<std::uintptr_t,std::array<std::byte,8>> storage;
    std::map<std::uint64_t,std::string> names{{1,"ArrayProperty"},{2,"ObjectProperty"},{3,"ClassProperty"},
        {4,"WeakObjectProperty"},{5,"SoftObjectProperty"},{6,"StructProperty"},{7,"IntProperty"},
        {10,"Npc"},{40,"Class"},{90,"Targets"},{91,"Inner"},{101,"ArrayProperty"},
        {102,"ObjectProperty"},{110,"Npc"},{190,"Targets"},{191,"Inner"}};
    std::set<std::uintptr_t> valid_classes{property_class,metaclass};
    ObjectSerialIdentity identity{property_class,metaclass,4,10,0,7,40,0};
    std::string path="/Script/G1R.Npc";
    std::vector<std::uintptr_t> class_checks;
    int read_count{},identity_count{},path_count{};
    std::uintptr_t failing_address{};

    template<class T> void set(std::uintptr_t address,T value) {
        static_assert(sizeof(T)<=8);
        auto& bytes=storage[address];bytes.fill(std::byte{});std::memcpy(bytes.data(),&value,sizeof(value));
    }
    template<class T> T read(std::uintptr_t address) {
        ++read_count;const auto found=storage.find(address);
        if(address==failing_address || found==storage.end())
            throw g1r_mcp::HandlerError("invalid_memory","Изолированная память метаданных недоступна");
        T result{};std::memcpy(&result,found->second.data(),sizeof(result));return result;
    }
    void set_field(std::uintptr_t field,std::uintptr_t field_class,std::uint64_t name,int size,int offset,std::uint64_t value_flags) {
        set(field+8,field_class);set(field+0x10,std::uint64_t{0x1990});set(field+0x18,std::uintptr_t{});
        set(field+0x20,name);set(field+0x28,std::int32_t{1});set(field+0x30,std::int32_t{1});
        set(field+0x34,static_cast<std::int32_t>(size));set(field+0x38,value_flags);set(field+0x44,static_cast<std::int32_t>(offset));
    }
    ReflectedMemory() {
        set_field(outer,outer_class,90,16,8,flags);set(outer_class,std::uint64_t{1});
        set(outer+0x70,std::uint8_t{});set(outer+0x78,inner);
        set_field(inner,inner_class,91,8,0,0);set(inner_class,std::uint64_t{2});set(inner+0x70,property_class);
    }
    json capture(int size=16,int dim=1,std::uint64_t expected_flags=flags,std::uintptr_t field=outer) {
        return g1r_mcp::object_array_parameter_layout::capture(field,size,dim,expected_flags,
            [this]<class T>(std::uintptr_t address){return read<T>(address);},
            [this](std::uint64_t value){const auto found=names.find(value);return found==names.end()?std::string{}:found->second;},
            [this](std::uintptr_t pointer){++identity_count;require(pointer==property_class || pointer==identity.object,"Identity запрошена для постороннего объекта");return identity;},
            [this](std::uintptr_t pointer){class_checks.push_back(pointer);if(!valid_classes.contains(pointer))
                throw g1r_mcp::HandlerError("unsupported_object_array_type","Изолированный класс не подтверждён");},
            [this](std::uintptr_t pointer){++path_count;require(pointer==identity.object,"Путь запрошен для другого класса");return path;});
    }
};
template<class Mutate> void changed_metadata(Mutate&& mutate) {
    ReflectedMemory memory;const auto before=memory.capture();mutate(memory);const auto after=memory.capture();
    require(before!=after,"Изменившиеся исходные токены должны менять снимок метаданных");
    require(before.at("public")==after.at("public"),"Точные исходные токены сохраняются даже при одинаковом публичном описании");
}
}

int main() {
    using namespace g1r_mcp;
    using namespace parameter_direction;
    try {
        {ReflectedMemory memory;const auto snapshot=memory.capture();
            require(snapshot.at("public")==json{{"inner_type","ObjectProperty"},{"inner_element_size",8},{"inner_array_dim",1},
                {"inner_offset",0},{"property_class",{{"path","/Script/G1R.Npc"},{"name","Npc"}}}},"Публичное описание содержит точную раскладку и класс элементов");
            require(snapshot.at("outer").at("pointer")==ReflectedMemory::outer && snapshot.at("outer").at("inner")==ReflectedMemory::inner &&
                snapshot.at("inner").at("property_class")==ReflectedMemory::property_class,"Снимок связывает внешний параметр, Inner и PropertyClass");
            require(snapshot.at("property_class_identity")==json{{"object",ReflectedMemory::property_class},{"object_class",ReflectedMemory::metaclass},
                {"index",4},{"name",10},{"serial",0},{"class_index",7},{"class_name",40},{"class_serial",0}},"Identity сохраняет все восемь токенов, включая нулевые serial");
            require(memory.class_checks==std::vector<std::uintptr_t>{ReflectedMemory::property_class,ReflectedMemory::metaclass} &&
                memory.identity_count==1 && memory.path_count==1,"Проверяются UClass элементов и его metaclass без обхода элементов массива");
            require(memory.capture()==snapshot,"Повторный стабильный снимок равен исходному");}
        for(const auto allowed:std::array<std::uint64_t,3>{parm|const_parm|reference_parm,parm|const_parm|out_parm,parm|const_parm|reference_parm|out_parm}) {
            ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x38,allowed);
            require(memory.capture(16,1,allowed).at("outer").at("flags")==allowed,"Подтверждённая const-ссылка сохраняет наблюдаемые флаги");
        }
        for(const int size:{0,8,15,17,32}) {ReflectedMemory memory;reject("unsupported_object_array_layout",[&]{memory.capture(size);});
            require(memory.read_count==0,"Неверный переданный размер отвергается до чтения метаданных");}
        for(const int dim:{0,2,-1}) {ReflectedMemory memory;reject("unsupported_object_array_layout",[&]{memory.capture(16,dim);});
            require(memory.read_count==0,"Статический массив параметров отвергается до чтения метаданных");}
        for(const auto bad:std::array<std::uint64_t,8>{0,parm,parm|const_parm,parm|reference_parm,parm|out_parm,
                parm|const_parm|reference_parm|return_parm,const_parm|reference_parm,parm|const_parm|0x800}) {
            ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x38,bad);
            reject("unsupported_signature",[&]{memory.capture(16,1,bad);});require(memory.class_checks.empty(),"Неверное направление отвергается до проверки класса элементов");
        }
        {ReflectedMemory memory;reject("unsupported_signature",[&]{memory.capture(16,1,parm|const_parm|out_parm);});}
        for(const int size:{0,8,15,17}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x34,static_cast<std::int32_t>(size));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const int dim:{0,2,-1}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x30,static_cast<std::int32_t>(dim));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const int offset:{-8,-1,1,7}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x44,static_cast<std::int32_t>(offset));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const int offset:{0,16,24}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x44,static_cast<std::int32_t>(offset));
            require(memory.capture().at("outer").at("offset")==offset,"Нулевой и выровненные смещения внешнего параметра допустимы");}
        for(const auto type:std::array<std::uint64_t,3>{2,3,6}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer_class,type);
            reject("unsupported_object_array_type",[&]{memory.capture();});}
        for(const auto type:std::array<std::uint64_t,5>{1,3,4,5,7}) {ReflectedMemory memory;memory.set(ReflectedMemory::inner_class,type);
            reject("unsupported_object_array_type",[&]{memory.capture();});require(memory.class_checks.empty(),"Ссылочные альтернативы и другой тип Inner не получают UClass validation");}
        for(const int size:{0,4,7,16}) {ReflectedMemory memory;memory.set(ReflectedMemory::inner+0x34,static_cast<std::int32_t>(size));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const int dim:{0,2,-1}) {ReflectedMemory memory;memory.set(ReflectedMemory::inner+0x30,static_cast<std::int32_t>(dim));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const int offset:{-1,1,8}) {ReflectedMemory memory;memory.set(ReflectedMemory::inner+0x44,static_cast<std::int32_t>(offset));
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const auto flags:std::array<std::uint8_t,2>{1,255}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x70,flags);
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const auto pointer:std::array<std::uintptr_t,2>{0,ReflectedMemory::outer}) {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x78,pointer);
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x18,ReflectedMemory::outer);
            reject("unsupported_object_array_layout",[&]{memory.capture();});}
        for(const auto next:std::array<std::uintptr_t,2>{ReflectedMemory::inner,ReflectedMemory::outer}) {ReflectedMemory memory;memory.set(ReflectedMemory::inner+0x18,next);
            reject("unsupported_object_array_layout",[&]{memory.capture();});}

        {ReflectedMemory memory;memory.set(ReflectedMemory::inner+0x70,std::uintptr_t{});
            reject("unsupported_object_array_type",[&]{memory.capture();});require(memory.identity_count==0,"Null PropertyClass отвергается до identity capture");}
        {ReflectedMemory memory;memory.valid_classes.erase(ReflectedMemory::metaclass);
            reject("unsupported_object_array_type",[&]{memory.capture();});require(memory.path_count==0,"Неверный metaclass отвергается до разрешения пути");}
        {ReflectedMemory memory;memory.identity.object+=8;reject("unsupported_object_array_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.object_class=0;reject("unsupported_object_array_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.serial=-1;reject("unsupported_object_array_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.class_serial=-1;reject("unsupported_object_array_type",[&]{memory.capture();});}
        for(const auto path:std::array<std::string,2>{"","Script/G1R.Npc"}) {ReflectedMemory memory;memory.path=path;
            reject("unsupported_object_array_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.names[10]="";reject("unsupported_object_array_type",[&]{memory.capture();});}

        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::outer_class,std::uint64_t{101});});
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::inner_class,std::uint64_t{102});});
        for(const auto field:std::array<std::uintptr_t,2>{ReflectedMemory::outer,ReflectedMemory::inner}) {
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x10,std::uint64_t{0x1991});});
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x18,std::uintptr_t{0x7000});});
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x20,std::uint64_t{field==ReflectedMemory::outer?190ULL:191ULL});});
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x28,std::int32_t{2});});
        }
        changed_metadata([](ReflectedMemory& memory){memory.identity.index=5;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.name=110;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.serial=1;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_index=8;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_name=41;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_serial=1;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.object_class+=8;memory.valid_classes.insert(memory.identity.object_class);});
        changed_metadata([](ReflectedMemory& memory){memory.identity.object+=8;memory.valid_classes.insert(memory.identity.object);memory.set(ReflectedMemory::inner+0x70,memory.identity.object);});
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::inner+0x38,std::uint64_t{0x10});});

        for(const auto address:std::array<std::uintptr_t,4>{ReflectedMemory::outer+8,ReflectedMemory::outer+0x78,ReflectedMemory::inner+0x34,ReflectedMemory::inner+0x70}) {
            ReflectedMemory memory;memory.failing_address=address;reject("invalid_memory",[&]{memory.capture();});
        }
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+8,std::uintptr_t{});reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::inner+8,std::uintptr_t{});reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x78,std::numeric_limits<std::uintptr_t>::max()-3);
            reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;reject("invalid_memory",[&]{memory.capture(16,1,ReflectedMemory::flags,0);});require(memory.read_count==0,"Null field не передаётся Reader");}
        {ReflectedMemory memory;reject("invalid_memory",[&]{memory.capture(16,1,ReflectedMemory::flags,std::numeric_limits<std::uintptr_t>::max()-3);});
            require(memory.read_count==0,"Переполнение адреса field отвергается до Reader");}
        {using object_array_parameter_layout::checked_address;const auto maximum=std::numeric_limits<std::uintptr_t>::max();
            require(checked_address(maximum-7,0,8)==maximum-7,"Диапазон метаданных может заканчиваться последним адресуемым байтом");
            reject("invalid_memory",[&]{checked_address(maximum-7,1,8);});
            reject("invalid_memory",[&]{checked_address(maximum,1,0);});}
        std::cout<<"object_array_parameter_layout: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
