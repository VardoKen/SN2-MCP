#include <sn2_mcp/linear_color_layout.hpp>

#include <algorithm>
#include <array>
#include <cstring>
#include <iostream>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <vector>

namespace {
using sn2_mcp::ObjectSerialIdentity;
using sn2_mcp::linear_color_layout::Layout;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный отказ раскладки FLinearColor");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Проверка раскладки FLinearColor не исполняет игровой вызов");return;
    }
    throw std::runtime_error("Ожидался отказ раскладки FLinearColor");
}
Layout layout() {return {16,4,false,{{"R","FloatProperty",4,0,1},{"G","FloatProperty",4,4,1},
    {"B","FloatProperty",4,8,1},{"A","FloatProperty",4,12,1}}};}
struct ReflectedMemory {
    static constexpr std::uintptr_t outer=0x1000,outer_class=0x2000,structure=0x3000,
        structure_class=0x4000,scalar_class=0x5000,head=0x6000;
    std::map<std::uintptr_t,std::array<std::byte,8>> storage;
    std::map<std::uint64_t,std::string> names{{1,"StructProperty"},{2,"FloatProperty"},{3,"DoubleProperty"},
        {4,"ByteProperty"},{10,"LinearColor"},{11,"R"},{12,"G"},{13,"B"},{14,"A"},{90,"Tint"},
        {101,"StructProperty"},{102,"FloatProperty"},{110,"LinearColor"},{111,"R"},{190,"Tint"}};
    std::set<std::uintptr_t> valid_classes{structure_class};
    ObjectSerialIdentity identity{structure,structure_class,4,10,0,7,40,0};
    std::string path="/Script/CoreUObject.LinearColor";
    std::uintptr_t failing_address{};
    int read_count{},identity_count{},class_count{},path_count{};
    template<class T> void set(std::uintptr_t address,T value) {
        static_assert(sizeof(T)<=8);auto& bytes=storage[address];bytes.fill(std::byte{});std::memcpy(bytes.data(),&value,sizeof(value));
    }
    template<class T> T read(std::uintptr_t address) {
        ++read_count;const auto found=storage.find(address);
        if(address==failing_address || found==storage.end())throw sn2_mcp::HandlerError("invalid_memory","Изолированные метаданные FLinearColor недоступны");
        T value{};std::memcpy(&value,found->second.data(),sizeof(value));return value;
    }
    void set_field(std::uintptr_t field,std::uintptr_t field_class,std::uint64_t name,int size,int offset,std::uintptr_t next) {
        set(field+8,field_class);set(field+0x10,std::uint64_t{0x9990});set(field+0x18,next);set(field+0x20,name);
        set(field+0x28,std::int32_t{1});set(field+0x30,std::int32_t{1});set(field+0x34,static_cast<std::int32_t>(size));
        set(field+0x38,std::uint64_t{0x80});set(field+0x44,static_cast<std::int32_t>(offset));
    }
    ReflectedMemory() {
        set_field(outer,outer_class,90,16,4,0);set(outer_class,std::uint64_t{1});set(outer+0x70,structure);
        set(structure+0x50,head);set(structure+0x40,std::uintptr_t{});set(structure+0x58,std::int32_t{16});set(structure+0x5c,std::int32_t{0xA50000|(4)});
        set(scalar_class,std::uint64_t{2});for(int i=0;i<4;++i)
            set_field(head+static_cast<std::uintptr_t>(i)*0x100,scalar_class,static_cast<std::uint64_t>(11+i),4,i*4,
                i==3?0:head+static_cast<std::uintptr_t>(i+1)*0x100);
    }
    sn2_mcp::linear_color_layout::Snapshot capture(int size=16,int dim=1,std::uintptr_t field=outer) {
        return sn2_mcp::linear_color_layout::capture(field,size,dim,
            [this]<class T>(std::uintptr_t address){return read<T>(address);},
            [this](std::uint64_t value){const auto found=names.find(value);return found==names.end()?std::string{}:found->second;},
            [this](std::uintptr_t pointer){++identity_count;if(!pointer)throw sn2_mcp::HandlerError("invalid_memory","Null ScriptStruct");
                require(pointer==structure || pointer==identity.object,"Identity запрошена для посторонней структуры");return identity;},
            [this](std::uintptr_t pointer){++class_count;if(!valid_classes.contains(pointer))throw sn2_mcp::HandlerError("unsupported_linear_color_type","Неподтверждённый metaclass");},
            [this](std::uintptr_t pointer){++path_count;require(pointer==identity.object,"Путь запрошен для другой структуры");return path;});
    }
};
template<class Mutate> void changed_metadata(Mutate&& mutate) {
    ReflectedMemory memory;const auto before=memory.capture();mutate(memory);const auto after=memory.capture();
    require(before!=after,"Изменение исходных токенов инвалидирует прежний снимок FLinearColor");
    require(before.layout==after.layout && before.path==after.path && before.struct_name==after.struct_name,
            "Исходные токены различаются даже при неизменном публичном типе и раскладке");
}
}

int main() {
    using namespace sn2_mcp;
    try {
        const auto ordinary=layout();std::array<int,4> order{0,1,2,3};
        do {auto permuted=ordinary;for(int i=0;i<4;++i)permuted.components[static_cast<std::size_t>(i)]=ordinary.components[static_cast<std::size_t>(order[static_cast<std::size_t>(i)])];
            linear_color_layout::validate("/Script/CoreUObject.LinearColor","LinearColor",permuted);++checks;
        } while(std::next_permutation(order.begin(),order.end()));
        for(const auto path:std::array<std::string,4>{"","/Script/G1R.LinearColor","/Script/CoreUObject.Color","/Script/CoreUObject.linearcolor"})
            reject("unsupported_linear_color_type",[&]{linear_color_layout::validate(path,"LinearColor",ordinary);});
        for(const auto name:std::array<std::string,3>{"","Color","linearcolor"})
            reject("unsupported_linear_color_type",[&]{linear_color_layout::validate("/Script/CoreUObject.LinearColor",name,ordinary);});
        for(const int size:{0,12,15,17,24}) {auto bad=ordinary;bad.properties_size=size;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        for(const int alignment:{0,1,2,3,8,16}) {auto bad=ordinary;bad.min_alignment=alignment;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        {auto bad=ordinary;bad.has_super=true;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        {auto bad=ordinary;bad.components.pop_back();reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        {auto bad=ordinary;bad.components.push_back({"Extra","FloatProperty",4,16,1});reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        {auto bad=ordinary;bad.components[1].name="R";reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        {auto bad=ordinary;std::swap(bad.components[0].offset,bad.components[1].offset);reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        for(int i=0;i<4;++i) {
            for(const auto name:std::array<std::string,2>{"X","r"}) {auto bad=ordinary;bad.components[static_cast<std::size_t>(i)].name=name;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
            for(const auto type:std::array<std::string,2>{"DoubleProperty","ByteProperty"}) {auto bad=ordinary;bad.components[static_cast<std::size_t>(i)].type=type;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
            for(const int width:{0,8}) {auto bad=ordinary;bad.components[static_cast<std::size_t>(i)].element_size=width;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
            for(const int dim:{0,2}) {auto bad=ordinary;bad.components[static_cast<std::size_t>(i)].array_dim=dim;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
            for(const int offset:{-1,1,16}) {auto bad=ordinary;bad.components[static_cast<std::size_t>(i)].offset=offset;reject("unsupported_linear_color_layout",[&]{linear_color_layout::validate_layout(bad);});}
        }

        {ReflectedMemory memory;const auto snapshot=memory.capture();
            require(snapshot.layout==ordinary && snapshot.components.size()==4 && snapshot.identity==memory.identity,"Снимок сохраняет каноническую раскладку и всю identity");
            require(snapshot.field==ReflectedMemory::outer && snapshot.structure==ReflectedMemory::structure && snapshot.head==ReflectedMemory::head && snapshot.super==0,
                    "Внешний параметр связывается с собственной цепочкой ScriptStruct");
            require(snapshot.field_owner==0x9990 && snapshot.field_name==90 && snapshot.field_type_name==1 && snapshot.flags==0x80 &&
                snapshot.field_flags==1 && snapshot.offset==4,"Внешние исходные токены сохраняются полностью");
            require(memory.identity_count==1 && memory.class_count==1 && memory.path_count==1,"Каноническая identity, metaclass и путь проверяются один раз");
            require(memory.capture()==snapshot,"Повторная стабильная reflection даёт равный снимок");}
        for(const int size:{0,12,24}) {ReflectedMemory memory;reject("unsupported_linear_color_layout",[&]{memory.capture(size);});require(memory.read_count==0,"Переданный неверный размер отвергается до чтения");}
        for(const int dim:{0,2}) {ReflectedMemory memory;reject("unsupported_linear_color_layout",[&]{memory.capture(16,dim);});require(memory.read_count==0,"Статический массив параметра отвергается до чтения");}
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer_class,std::uint64_t{2});reject("unsupported_linear_color_type",[&]{memory.capture();});require(memory.identity_count==0,"Другой тип параметра отвергается до identity capture");}
        for(const auto edit:std::array<std::pair<std::size_t,std::int32_t>,4>{{{0x34,12},{0x30,2},{0x44,-1},{0x44,2}}}) {
            ReflectedMemory memory;memory.set(ReflectedMemory::outer+edit.first,edit.second);reject("unsupported_linear_color_layout",[&]{memory.capture();});
        }
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x18,ReflectedMemory::outer);reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+0x70,std::uintptr_t{});reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.object+=8;reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.object_class=0;reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.serial=-1;reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.identity.class_serial=-1;reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.valid_classes.clear();reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.path="/Script/G1R.LinearColor";reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.names[10]="Color";reject("unsupported_linear_color_type",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::structure+0x40,std::uintptr_t{0x9000});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::structure+0x58,std::int32_t{24});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::structure+0x5c,std::int32_t{0xA50000|(8)});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::structure+0x50,std::uintptr_t{});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        for(const auto next:std::array<std::uintptr_t,3>{ReflectedMemory::head,ReflectedMemory::outer,ReflectedMemory::head+0x100}) {
            ReflectedMemory memory;memory.set(ReflectedMemory::head+0x118,next);reject("unsupported_linear_color_layout",[&]{memory.capture();});
        }
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x318,std::uintptr_t{0x7000});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x118,std::uintptr_t{});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        for(const auto type:std::array<std::uint64_t,2>{3,4}) {ReflectedMemory memory;memory.set(ReflectedMemory::scalar_class,type);reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x120,std::uint64_t{11});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x144,std::int32_t{0});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x134,std::int32_t{8});reject("unsupported_linear_color_layout",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+0x130,std::int32_t{2});reject("unsupported_linear_color_layout",[&]{memory.capture();});}

        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::outer_class,std::uint64_t{101});});
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::scalar_class,std::uint64_t{102});});
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::outer+0x20,std::uint64_t{190});});
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::head+0x20,std::uint64_t{111});});
        for(const auto field:std::array<std::uintptr_t,2>{ReflectedMemory::outer,ReflectedMemory::head}) {
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x10,std::uint64_t{0x9991});});
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x38,std::uint64_t{0x81});});
            changed_metadata([=](ReflectedMemory& memory){memory.set(field+0x28,std::int32_t{2});});
        }
        changed_metadata([](ReflectedMemory& memory){memory.set(ReflectedMemory::outer+0x18,std::uintptr_t{0x7000});});
        changed_metadata([](ReflectedMemory& memory){memory.identity.index=5;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.name=110;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.serial=1;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_index=8;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_name=41;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.class_serial=1;});
        changed_metadata([](ReflectedMemory& memory){memory.identity.object_class+=8;memory.valid_classes.insert(memory.identity.object_class);});
        changed_metadata([](ReflectedMemory& memory){const auto copy=memory.storage;for(const auto& [address,value]:copy)
                if(address>=ReflectedMemory::structure && address<ReflectedMemory::structure+0x100)memory.storage[address+0x8000]=value;
            memory.identity.object+=0x8000;memory.set(ReflectedMemory::outer+0x70,memory.identity.object);});

        for(const auto address:std::array<std::uintptr_t,4>{ReflectedMemory::outer+8,ReflectedMemory::outer+0x70,ReflectedMemory::structure+0x58,ReflectedMemory::head+0x34}) {
            ReflectedMemory memory;memory.failing_address=address;reject("invalid_memory",[&]{memory.capture();});
        }
        {ReflectedMemory memory;memory.set(ReflectedMemory::outer+8,std::uintptr_t{});reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::head+8,std::uintptr_t{});reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;memory.set(ReflectedMemory::structure+0x50,std::numeric_limits<std::uintptr_t>::max()-3);reject("invalid_memory",[&]{memory.capture();});}
        {ReflectedMemory memory;reject("invalid_memory",[&]{memory.capture(16,1,0);});require(memory.read_count==0,"Null field не передаётся Reader");}
        {ReflectedMemory memory;reject("invalid_memory",[&]{memory.capture(16,1,std::numeric_limits<std::uintptr_t>::max()-3);});require(memory.read_count==0,"Field overflow не передаётся Reader");}
        {using linear_color_layout::checked_address;const auto maximum=std::numeric_limits<std::uintptr_t>::max();
            require(checked_address(maximum-7,0,8)==maximum-7,"Диапазон метаданных может завершаться последним адресуемым байтом");
            reject("invalid_memory",[&]{checked_address(maximum-7,1,8);});reject("invalid_memory",[&]{checked_address(maximum,1,0);});}
        std::cout<<"linear_color_layout: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
