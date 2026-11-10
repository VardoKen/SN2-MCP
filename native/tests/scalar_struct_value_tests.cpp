#include <g1r_mcp/scalar_struct_values.hpp>
#include <g1r_mcp/scalar_struct_arguments.hpp>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <iostream>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using nlohmann::json;
using g1r_mcp::scalar_struct_values::Component;
using g1r_mcp::scalar_struct_values::Layout;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action> void reject(const char* code,Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный код отказа декодера скалярного DTO");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Декодер собственной копии не исполняет игровую функцию");return;
    }
    throw std::runtime_error("Ожидался отказ декодера скалярного DTO");
}
Component numeric(std::string name,int offset,std::string type,int width) {
    return {std::move(name),offset,{std::move(type),width,1}};
}
Component boolean(std::string name,int offset,std::uint8_t mask) {
    return {std::move(name),offset,{"BoolProperty",1,1,1,0,mask,mask}};
}
template<class T> void put(std::span<std::byte> bytes,std::size_t offset,T value) {
    if(offset>bytes.size() || sizeof(value)>bytes.size()-offset)throw std::runtime_error("Неверный offset изолированного DTO");
    std::memcpy(bytes.data()+offset,&value,sizeof(value));
}
Layout mixed() {
    return {64,8,false,{numeric("Signed32",0,"IntProperty",4),boolean("BitOne",4,1),boolean("BitSeven",4,128),
        numeric("Unsigned32",8,"UInt32Property",4),numeric("Signed64",16,"Int64Property",8),
        numeric("Unsigned64",24,"UInt64Property",8),numeric("Float",32,"FloatProperty",4),
        numeric("Double",40,"DoubleProperty",8),boolean("NativeBool",48,0xff)}};
}
struct CaptureFixture {
    std::array<std::byte,4096> memory{};
    std::vector<std::string> names{"None"};
    g1r_mcp::ObjectSerialIdentity identity{0x200,0x300,4,0,23,5,0,29};
    Layout layout;
    explicit CaptureFixture(Layout value):layout(std::move(value)) {
        auto named=[&](std::string name) {names.push_back(std::move(name));return std::uint64_t(names.size()-1);};
        identity.name=named("SyntheticStruct");identity.class_name=named("ScriptStruct");
        put(std::span(memory),0x88,std::uintptr_t{0x340});
        put(std::span(memory),0x90,std::uint64_t{0x301});
        put(std::span(memory),0xa0,named("StoredValue"));
        put(std::span(memory),0xb0,std::int32_t{1});put(std::span(memory),0xb4,std::int32_t{layout.properties_size});
        put(std::span(memory),0xb8,std::uint64_t{0x80});put(std::span(memory),0xc4,std::int32_t{32});
        put(std::span(memory),0xf0,std::uintptr_t{0x200});
        put(std::span(memory),0x340,named("StructProperty"));
        put(std::span(memory),0x250,std::uintptr_t{0x400});
        put(std::span(memory),0x258,std::int32_t{layout.properties_size});
        put(std::span(memory),0x25c,std::int32_t{layout.min_alignment});
        for(std::size_t index=0;index<layout.components.size();++index) {
            const auto field=0x400+index*0x100,type=0xc00+index*0x20;
            const auto& component=layout.components[index];
            put(std::span(memory),field+8,std::uintptr_t{type});
            put(std::span(memory),field+0x10,std::uint64_t{0x201});
            put(std::span(memory),field+0x18,index+1<layout.components.size()?std::uintptr_t{field+0x100}:std::uintptr_t{});
            put(std::span(memory),field+0x20,named(component.name));put(std::span(memory),type,named(component.layout.type));
            put(std::span(memory),field+0x30,std::int32_t{component.layout.array_dim});
            put(std::span(memory),field+0x34,std::int32_t{component.layout.element_size});
            put(std::span(memory),field+0x44,std::int32_t{component.offset});
            if(component.layout.type=="BoolProperty") {
                put(std::span(memory),field+0x70,component.layout.field_size);
                put(std::span(memory),field+0x71,component.layout.byte_offset);
                put(std::span(memory),field+0x72,component.layout.byte_mask);
                put(std::span(memory),field+0x73,component.layout.field_mask);
            }
        }
    }
    auto capture() {
        return g1r_mcp::scalar_struct_values::capture(0x80,layout.properties_size,1,
            [this]<class Value>(std::uintptr_t address) {
                if(address>memory.size() || sizeof(Value)>memory.size()-address)throw std::runtime_error("Чтение вне fake memory");
                Value value{};std::memcpy(&value,memory.data()+address,sizeof(value));return value;
            },
            [this](std::uint64_t name) {return names.at(static_cast<std::size_t>(name));},
            [this](std::uintptr_t address) {require(address==identity.object,"Capture читает собственный fake UScriptStruct");return identity;},
            [this](std::uintptr_t address) {require(address==identity.object_class,"Capture проверяет класс fake UScriptStruct");},
            [](std::uintptr_t) {return std::string("/Synthetic/SyntheticStruct");});
    }
};
void captured_property_copies() {
    using namespace g1r_mcp;
    const std::array<Layout,3> layouts{{
        {16,8,false,{numeric("X",0,"DoubleProperty",8),numeric("Y",8,"DoubleProperty",8)}},
        {24,8,false,{numeric("X",0,"DoubleProperty",8),numeric("Y",8,"DoubleProperty",8),numeric("Z",16,"DoubleProperty",8)}},
        {24,8,false,{numeric("Count",0,"IntProperty",4),boolean("Enabled",4,1),numeric("Ratio",8,"DoubleProperty",8),numeric("Wide",16,"Int64Property",8)}}
    }};
    const std::array<json,3> expected{{json{{"X",13.25},{"Y",-27.5}},json{{"X",1.25},{"Y",-2.5},{"Z",3.75}},
        json{{"Count",17},{"Enabled",true},{"Ratio",2.5},{"Wide",std::int64_t{9007199254740993LL}}}}};
    for(std::size_t index=0;index<layouts.size();++index) {
        CaptureFixture fixture(layouts[index]);
        std::vector<std::byte> source(static_cast<std::size_t>(layouts[index].properties_size));
        if(index<2) {put(std::span(source),0,index==0?13.25:1.25);put(std::span(source),8,index==0?-27.5:-2.5);if(index==1)put(std::span(source),16,3.75);}
        else {put(std::span(source),0,std::int32_t{17});put(std::span(source),4,std::uint8_t{1});put(std::span(source),8,2.5);put(std::span(source),16,std::int64_t{9007199254740993LL});}
        const auto before=fixture.capture();const auto copied=source;
        std::fill(source.begin(),source.end(),std::byte{});
        require(before.layout==layouts[index],"Capture восстанавливает layout 2D/3D/DTO из fake reflection");
        require(before.offset==32 && before.path=="/Synthetic/SyntheticStruct" && before.identity==fixture.identity,"Capture сохраняет внешнее поле и identity");
        const auto decoded=scalar_struct_values::decode(before.layout,copied);
        require(decoded==expected[index],"Capture→копия→decode сохраняет точные значения 2D/3D/DTO");
        require(before==fixture.capture(),"Неизменённые reflection metadata совпадают до и после copy");
        put(std::span(fixture.memory),0x438,std::uint64_t{0x200});
        require(!(before==fixture.capture()),"Изменённые flags поля обнаруживаются при повторном capture");
        if(index==2) {
            require(decoded.at("Enabled").is_boolean() && decoded.at("Wide").is_number_integer() && !decoded.at("Wide").is_number_unsigned()
                && json::parse(decoded.dump()).at("Wide").get<std::int64_t>()==9007199254740993LL,"Captured DTO сохраняет Bool и Int64 больше 2^53 в JSON");
            put(std::span(fixture.memory),0x438,std::uint64_t{});fixture.identity.serial++;
            require(!(before==fixture.capture()),"Смена serial структуры обнаруживается повторным capture");
        }
    }
}
}

int main() {
    using namespace g1r_mcp;
    try {
        captured_property_copies();
        const auto reflected=mixed();
        std::array<std::byte,66> storage{};storage.fill(std::byte{0xa5});
        auto body=std::span(storage).subspan(1,64);
        put(body,0,std::numeric_limits<std::int32_t>::min());put(body,4,std::uint8_t{0x81});
        put(body,8,std::numeric_limits<std::uint32_t>::max());put(body,16,std::int64_t{-9007199254740993LL});
        put(body,24,std::numeric_limits<std::uint64_t>::max());put(body,32,-3.75F);put(body,40,1.0+std::numeric_limits<double>::epsilon());
        put(body,48,std::uint8_t{2});
        const auto before=storage;const auto decoded=scalar_struct_values::decode(reflected,body);
        require(decoded.is_object() && decoded.size()==9,"Смешанный DTO возвращает только свои именованные поля");
        require(decoded.at("Signed32").is_number_integer() && !decoded.at("Signed32").is_number_unsigned() &&
            decoded.at("Signed32").get<std::int64_t>()==std::numeric_limits<std::int32_t>::min(),"IntProperty сохраняет signed-домен");
        require(decoded.at("Unsigned32").is_number_unsigned() && decoded.at("Unsigned32").get<std::uint64_t>()==0xffffffffULL,
                "UInt32Property сохраняет unsigned-домен и старший бит");
        require(decoded.at("Signed64").get<std::int64_t>()==-9007199254740993LL && !decoded.at("Signed64").is_number_unsigned(),
                "Int64 вне точного диапазона double не проходит через double");
        require(decoded.at("Unsigned64").is_number_unsigned() && decoded.at("Unsigned64").get<std::uint64_t>()==std::numeric_limits<std::uint64_t>::max(),
                "UInt64 сохраняет все 64 бита");
        require(decoded.at("Float").is_number_float() && decoded.at("Float").get<double>()==-3.75 &&
            decoded.at("Double").get<double>()==1.0+std::numeric_limits<double>::epsilon(),"Скалярные дробные поля сохраняют свою точность");
        require(decoded.at("BitOne")==true && decoded.at("BitSeven")==true && decoded.at("NativeBool")==true,
                "Packed bool читают свои биты, native bool читает весь байт");
        const auto serialized=json::parse(decoded.dump());
        require(serialized.at("Signed64").get<std::int64_t>()==-9007199254740993LL &&
            serialized.at("Unsigned64").get<std::uint64_t>()==std::numeric_limits<std::uint64_t>::max(),"JSON roundtrip сохраняет большие целые DTO");
        require(storage==before,"Невыровненная копия, padding и соседние байты не изменяются");
        {auto layout=mixed();layout.components.back().layout.byte_mask=1;
            const json input{{"Signed32",std::numeric_limits<std::int32_t>::min()},
                {"BitOne",true},{"BitSeven",true},{"Unsigned32",std::numeric_limits<std::uint32_t>::max()},
                {"Signed64",std::numeric_limits<std::int64_t>::min()},{"Unsigned64",std::numeric_limits<std::uint64_t>::max()},
                {"Float",-3.75},{"Double",1.0+std::numeric_limits<double>::epsilon()},{"NativeBool",true}};
            const auto encoded=scalar_struct_arguments::encode(layout,input);
            std::array<std::byte,64> expected{};auto bytes=std::span(expected);
            put(bytes,0,std::numeric_limits<std::int32_t>::min());put(bytes,4,std::uint8_t{0x81});
            put(bytes,8,std::numeric_limits<std::uint32_t>::max());put(bytes,16,std::numeric_limits<std::int64_t>::min());
            put(bytes,24,std::numeric_limits<std::uint64_t>::max());put(bytes,32,-3.75F);
            put(bytes,40,1.0+std::numeric_limits<double>::epsilon());put(bytes,48,std::uint8_t{1});
            require(encoded.size()==expected.size() && std::equal(encoded.begin(),encoded.end(),expected.begin()),
                    "Encoder сохраняет граничные signed/unsigned и float/double, объединяет bitfields в 81, пишет native bool как 01 и обнуляет padding");
            require(scalar_struct_values::decode(layout,encoded)==input,
                    "Mixed DTO encode/decode возвращает точные входные значения без потери 64-bit границ и общего bool-байта");}

        {auto reverse=reflected;std::reverse(reverse.components.begin(),reverse.components.end());
            require(scalar_struct_values::decode(reverse,body)==decoded,"Порядок цепочки свойств не меняет именованные значения DTO");}
        {const Layout noncanonical{4,4,false,{numeric("Ordinary",0,"IntProperty",4)}};
            std::array<std::byte,4> bytes{};put(std::span(bytes),0,std::int32_t{17});
            require(scalar_struct_values::decode(noncanonical,bytes)==json{{"Ordinary",17}},"Пользовательский DTO не требует канонического имени структуры");}

        {Layout packed{1,1,false,{}};for(int i=0;i<8;++i)packed.components.push_back(boolean("Bit"+std::to_string(i),0,static_cast<std::uint8_t>(1U<<i)));
            for(const auto value:std::array<std::uint8_t,5>{0,1,0x55,0x80,0xff}) {
                const std::array<std::byte,1> bytes{static_cast<std::byte>(value)};const auto result=scalar_struct_values::decode(packed,bytes);
                for(int i=0;i<8;++i)require(result.at("Bit"+std::to_string(i))==json((value&(1U<<i))!=0),"Разные BoolProperty совместно используют один байт без смешения битов");
            }
        }
        {Layout native{1,1,false,{{"Native",0,{"BoolProperty",1,1,1,0,1,0xff}}}};
            const std::array<std::byte,1> bytes{std::byte{0x80}};
            require(scalar_struct_values::decode(native,bytes).at("Native")==true,"FieldMask=FF чтения не заменяется одиночным ByteMask записи");}
        {Layout large{160,4,false,{}};std::array<std::byte,160> bytes{};
            for(int i=0;i<40;++i) {large.components.push_back(numeric("Field"+std::to_string(i),i*4,"IntProperty",4));
                put(std::span(bytes),static_cast<std::size_t>(i)*4,static_cast<std::int32_t>(1000+i));}
            const auto result=scalar_struct_values::decode(large,bytes);
            require(result.size()==40,"Число полей DTO не ограничено искусственным порогом 32");
            for(int i=0;i<40;++i)require(result.at("Field"+std::to_string(i))==1000+i,"Все поля большой плоской структуры доступны");}

        for(const auto type:std::array<std::string,2>{"Int64Property","UInt64Property"}) {
            const Layout one{8,8,false,{numeric("Wide",0,type,8)}};std::array<std::byte,8> bytes{};
            for(const auto raw:std::array<std::uint64_t,5>{9007199254740992ULL,9007199254740993ULL,0x8000000000000000ULL,
                    0xfedcba9876543210ULL,std::numeric_limits<std::uint64_t>::max()}) {
                put(std::span(bytes),0,raw);const auto result=scalar_struct_values::decode(one,bytes).at("Wide");
                if(type=="UInt64Property")require(result.is_number_unsigned() && result.get<std::uint64_t>()==raw,"UInt64 DTO сохраняет точные исходные биты");
                else {std::int64_t signed_value{};std::memcpy(&signed_value,&raw,sizeof(raw));
                    require(!result.is_number_unsigned() && result.get<std::int64_t>()==signed_value,"Int64 DTO интерпретирует старший бит как знак");}
            }
        }
        for(const float value:std::array<float,5>{-0.0F,-std::numeric_limits<float>::denorm_min(),std::numeric_limits<float>::max(),-2.5F,3.75F}) {
            const Layout one{4,4,false,{numeric("Value",0,"FloatProperty",4)}};std::array<std::byte,4> bytes{};put(std::span(bytes),0,value);
            const auto result=scalar_struct_values::decode(one,bytes).at("Value").get<double>();
            require(result==static_cast<double>(value) && std::signbit(result)==std::signbit(value),"Float DTO сохраняет конечное значение и знак нуля без clamp");
        }
        for(const double value:std::array<double,4>{-0.0,std::numeric_limits<double>::denorm_min(),std::numeric_limits<double>::max(),1.0+std::numeric_limits<double>::epsilon()}) {
            const Layout one{8,8,false,{numeric("Value",0,"DoubleProperty",8)}};std::array<std::byte,8> bytes{};put(std::span(bytes),0,value);
            const auto result=scalar_struct_values::decode(one,bytes).at("Value").get<double>();
            require(result==value && std::signbit(result)==std::signbit(value),"Double DTO сохраняет полную дробную точность");
        }
        for(const auto type:std::array<std::string,2>{"FloatProperty","DoubleProperty"}) {
            const int width=type=="FloatProperty"?4:8;const Layout one{width,width,false,{numeric("Value",0,type,width)}};
            for(const double value:std::array<double,3>{std::numeric_limits<double>::quiet_NaN(),std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity()}) {
                std::array<std::byte,8> bytes{};if(width==4)put(std::span(bytes),0,static_cast<float>(value));else put(std::span(bytes),0,value);
                const auto old=bytes;reject("invalid_scalar_value",[&]{scalar_struct_values::decode(one,std::span(bytes).first(static_cast<std::size_t>(width)));});
                require(bytes==old,"Отказ нечислового поля также сохраняет исходную копию DTO");
            }
        }

        for(const auto type:std::array<std::string,7>{"StructProperty","StrProperty","ArrayProperty","ObjectProperty","ClassProperty","NameProperty","EnumProperty"}) {
            const Layout unsupported{16,8,false,{numeric("Unsupported",0,type,8)}};
            reject("unsupported_scalar_output_layout",[&]{scalar_struct_values::decode(unsupported,{});});
        }
        for(const auto bytes:std::array<std::size_t,3>{0,63,65})
            reject("invalid_struct_copy",[&]{scalar_struct_values::decode(reflected,std::span(storage).first(bytes));});
        for(const int dim:{0,2}) {auto bad=reflected;bad.components.front().layout.array_dim=dim;
            reject("unsupported_scalar_output_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {auto bad=reflected;bad.has_super=true;reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        for(const int size:{0,-1,63}) {auto bad=reflected;bad.properties_size=size;reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        for(const int alignment:{0,3}) {auto bad=reflected;bad.min_alignment=alignment;reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {auto bad=reflected;bad.components.clear();reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {auto bad=reflected;bad.components[1].name=bad.components[0].name;reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {auto bad=reflected;bad.components[0].name="";reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        for(const int offset:{-1,61,std::numeric_limits<int>::max()}) {auto bad=reflected;bad.components[0].offset=offset;
            reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {const Layout too_wide{4,4,false,{numeric("Wide",0,"Int64Property",8)}};
            reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(too_wide,{});});}
        {auto bad=reflected;bad.components[3].offset=2;reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(bad,{});});}
        {const Layout overlap{1,1,false,{boolean("First",0,1),boolean("Second",0,1)}};
            reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(overlap,{});});}
        {const Layout overlap{1,1,false,{boolean("Native",0,0xff),boolean("Bit",0,1)}};
            reject("unsupported_scalar_struct_layout",[&]{scalar_struct_values::decode(overlap,{});});}
        require(storage==before,"Успешные и отвергнутые раскладки не меняют исходный mixed DTO");
        std::cout<<"scalar_struct_values: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
