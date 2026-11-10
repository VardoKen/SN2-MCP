#include <sn2_mcp/scalar_outputs.hpp>

#include <array>
#include <bit>
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
using sn2_mcp::scalar_outputs::Layout;
int checks{};
void require(bool good,const char* message) {
    ++checks;
    if(!good)throw std::runtime_error(message);
}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный код отказа скалярного out");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Ошибка чистого декодера должна явно исключать исполнение");
        return;
    }
    throw std::runtime_error("Ожидался отказ скалярного out");
}
template<class T>json copied(const Layout& layout,T value) {
    std::array<std::byte,sizeof(T)+2> storage{};
    storage.fill(std::byte{0xa5});
    std::memcpy(storage.data()+1,&value,sizeof(value));
    const auto before=storage;
    const auto decoded=sn2_mcp::scalar_outputs::decode(layout,std::span(storage).subspan(1,sizeof(value)));
    require(storage==before,"Decode должен читать собственную копию без изменения окружающих байтов");
    return decoded;
}
Layout numeric(const std::string& type,int size) {return {type,size,1};}
Layout boolean(std::uint8_t mask) {return {"BoolProperty",1,1,1,0,mask,mask};}
}

int main() {
    using namespace sn2_mcp;
    try {
        const auto int32=numeric("IntProperty",4),int64=numeric("Int64Property",8);
        const auto uint32=numeric("UInt32Property",4),uint64=numeric("UInt64Property",8);
        const auto float32=numeric("FloatProperty",4),float64=numeric("DoubleProperty",8);
        for(const auto value:std::vector<std::int32_t>{std::numeric_limits<std::int32_t>::min(),-1,0,1,
                std::numeric_limits<std::int32_t>::max()}) {
            const auto result=copied(int32,value);
            require(result.is_number_integer() && !result.is_number_unsigned(),"IntProperty out должен сохранять signed JSON-домен");
            require(result.get<std::int64_t>()==value,"IntProperty out должен сохранять все 32 бита");
        }
        for(const auto value:std::vector<std::int64_t>{std::numeric_limits<std::int64_t>::min(),-9007199254740993LL,
                -1,0,1,9007199254740992LL,9007199254740993LL,std::numeric_limits<std::int64_t>::max()}) {
            const auto result=copied(int64,value);
            require(result.is_number_integer() && !result.is_number_unsigned(),"Int64 out должен сохранять signed JSON-домен");
            require(result.get<std::int64_t>()==value,"Int64 out должен сохранять все 64 бита");
            require(json::parse(result.dump()).get<std::int64_t>()==value,"Текстовый JSON roundtrip не должен переводить Int64 в double");
        }
        for(const auto value:std::vector<std::uint32_t>{0,1,2147483648U,std::numeric_limits<std::uint32_t>::max()}) {
            const auto result=copied(uint32,value);
            require(result.is_number_unsigned(),"UInt32 out должен сохранять unsigned JSON-домен");
            require(result.get<std::uint64_t>()==value,"UInt32 out должен сохранять старший бит");
        }
        for(const auto value:std::vector<std::uint64_t>{0,1,4294967296ULL,9007199254740992ULL,9007199254740993ULL,
                9223372036854775808ULL,0xfedcba9876543210ULL,std::numeric_limits<std::uint64_t>::max()}) {
            const auto result=copied(uint64,value);
            require(result.is_number_unsigned(),"UInt64 out должен сохранять unsigned JSON-домен");
            require(result.get<std::uint64_t>()==value,"UInt64 out должен сохранять все 64 бита");
            require(json::parse(result.dump()).get<std::uint64_t>()==value,"Текстовый JSON roundtrip не должен переводить UInt64 в double");
        }
        require(copied(uint64,std::uint64_t{9007199254740992ULL})!=copied(uint64,std::uint64_t{9007199254740993ULL}),
                "Соседние out-значения выше 2^53 должны различаться");

        for(const auto value:std::vector<float>{-0.0F,0.0F,-1.25F,3.75F,
                std::numeric_limits<float>::denorm_min(),std::numeric_limits<float>::max(),-std::numeric_limits<float>::max()}) {
            const auto result=copied(float32,value);
            require(result.is_number_float() && result.get<double>()==static_cast<double>(value),
                    "Float out должен сохранять конечное числовое значение");
            if(value==0.0F)require(std::signbit(result.get<double>())==std::signbit(value),"Float out должен сохранять знак нуля");
        }
        for(const auto value:std::vector<double>{-0.0,0.0,-1.25,3.75,std::numeric_limits<double>::denorm_min(),
                std::numeric_limits<double>::max(),-std::numeric_limits<double>::max()}) {
            const auto result=copied(float64,value);
            require(result.is_number_float() && result.get<double>()==value,"Double out должен сохранять конечное числовое значение");
            if(value==0.0)require(std::signbit(result.get<double>())==std::signbit(value),"Double out должен сохранять знак нуля");
        }
        for(const auto value:std::vector<float>{std::numeric_limits<float>::infinity(),-std::numeric_limits<float>::infinity(),
                std::numeric_limits<float>::quiet_NaN()})
            reject("invalid_scalar_value",[&]{copied(float32,value);});
        for(const auto value:std::vector<double>{std::numeric_limits<double>::infinity(),-std::numeric_limits<double>::infinity(),
                std::numeric_limits<double>::quiet_NaN()})
            reject("invalid_scalar_value",[&]{copied(float64,value);});

        // В packed Bool значение определяется своим битом, а не любыми соседними битами байта.
        for(const auto mask:std::vector<std::uint8_t>{1,2,4,8,16,32,64,128}) {
            const auto layout=boolean(mask);
            require(copied(layout,std::uint8_t{0})==false,"Пустой packed Bool должен быть false");
            require(copied(layout,mask)==true,"Собственный packed Bool бит должен быть true");
            require(copied(layout,static_cast<std::uint8_t>(~mask))==false,"Чужие packed Bool биты не должны давать true");
            require(copied(layout,std::uint8_t{0xff})==true,"Packed Bool должен читать свой бит среди остальных");
        }
        for(const auto value:std::vector<std::uint8_t>{0,1,2,128,255})
            require(copied(boolean(0xff),value)==json(value!=0),"Native Bool маска FF должна читать любой ненулевой байт");
        // Native bool может писаться одиночным ByteMask, но читаться всем FieldMask=FF.
        // Значение 2 или 128 обязано быть true и при ByteMask=1: setter-mask не заменяет read-mask.
        for(const auto byte_mask:std::vector<std::uint8_t>{1,2,4,8,16,32,64,128,0xff}) {
            const Layout native_bool{"BoolProperty",1,1,1,0,byte_mask,0xff};
            scalar_outputs::validate_layout(native_bool);++checks;
            for(const auto value:std::vector<std::uint8_t>{0,1,2,128,255})
                require(copied(native_bool,value)==json(value!=0),
                        "Native Bool должен читать FieldMask=FF независимо от одиночного ByteMask записи");
        }
        for(const auto byte_mask:std::vector<std::uint8_t>{0,3,0xfe}) {
            const Layout invalid_native_bool{"BoolProperty",1,1,1,0,byte_mask,0xff};
            reject("unsupported_scalar_output_layout",[&]{scalar_outputs::decode(invalid_native_bool,std::span<const std::byte>{});});
        }

        const std::vector<Layout> supported={int32,int64,uint32,uint64,float32,float64,boolean(0xff)};
        std::array<std::byte,16> wrong{};wrong.fill(std::byte{0x7f});
        for(const auto& layout:supported) {
            scalar_outputs::validate_layout(layout);++checks;
            for(const auto length:std::vector<std::size_t>{0,static_cast<std::size_t>(layout.element_size-1),
                    static_cast<std::size_t>(layout.element_size+1),wrong.size()}) {
                reject("invalid_scalar_copy",[&]{scalar_outputs::decode(layout,std::span(wrong).first(length));});
            }
            for(const auto dim:std::vector<int>{0,-1,2,32}) {
                auto invalid=layout;invalid.array_dim=dim;
                reject("unsupported_scalar_output_layout",[&]{scalar_outputs::decode(invalid,std::span<const std::byte>{});});
            }
            for(const auto size:std::vector<int>{0,-1,layout.element_size-1,layout.element_size+1,16}) {
                auto invalid=layout;invalid.element_size=size;
                reject("unsupported_scalar_output_layout",[&]{scalar_outputs::decode(invalid,std::span<const std::byte>{});});
            }
        }
        for(const std::string& type:std::vector<std::string>{"ByteProperty","EnumProperty","Int8Property","Int16Property",
                "NameProperty","StrProperty","StructProperty","ObjectProperty","ClassProperty","ArrayProperty",""}) {
            reject("unsupported_scalar_output_layout",[&]{scalar_outputs::decode({type,8,1},std::span<const std::byte>{});});
        }
        for(const auto invalid:std::vector<Layout>{{"BoolProperty",1,1,0,0,1,1},
                {"BoolProperty",1,1,2,0,1,1},{"BoolProperty",1,1,1,1,1,1},
                {"BoolProperty",1,1,1,255,1,1},{"BoolProperty",1,1,1,0,0,0},
                {"BoolProperty",1,1,1,0,1,2},{"BoolProperty",1,1,1,0,3,3},
                {"BoolProperty",1,1,1,0,0xfe,0xfe},{"BoolProperty",4,1,1,0,0xff,0xff}}) {
            reject("unsupported_scalar_output_layout",[&]{scalar_outputs::decode(invalid,std::span<const std::byte>{});});
        }
        // Одинаковые биты должны интерпретироваться в домене отражённого типа, без unsigned-сдвига signed значения.
        const std::uint64_t highest=std::uint64_t{1}<<63;
        require(copied(uint64,highest).get<std::uint64_t>()==highest,"UInt64 старший бит не должен стать отрицательным");
        require(copied(int64,std::bit_cast<std::int64_t>(highest)).get<std::int64_t>()==std::numeric_limits<std::int64_t>::min(),
                "Int64 старший бит должен декодироваться как INT64_MIN");
        std::cout<<"scalar_outputs: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
