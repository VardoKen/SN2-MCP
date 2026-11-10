#include <g1r_mcp/string_arguments.hpp>

#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using nlohmann::json;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Action>void reject(Action&& action) {
    try {action();}
    catch(const g1r_mcp::HandlerError& error) {
        require(error.code()=="invalid_argument","Неверный код отказа строки");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Ошибка строки должна явно исключать исполнение");return;
    }
    throw std::runtime_error("Ожидался отказ строки");
}
std::string bytes(std::initializer_list<unsigned char> values) {
    std::string result;for(const auto value:values)result.push_back(static_cast<char>(value));return result;
}
}

int main() {
    using namespace g1r_mcp;
    try {
        require(string_arguments::encode(json(""))==u"","Пустая строка должна сохраняться");
        require(string_arguments::encode(json("ASCII 123\n\t"))==u"ASCII 123\n\t","ASCII и управляющие символы кроме NUL должны сохраняться");
        require(string_arguments::encode(json("Привет Ёж"))==u"Привет Ёж","Кириллица должна преобразовываться в UTF-16");
        require(string_arguments::encode(json("e\xCC\x81"))==u"e\u0301","Комбинирующий символ не должен нормализоваться");
        require(string_arguments::encode(json(bytes({0xf0,0x9f,0x98,0x80})))==u"\U0001F600","Emoji должен стать surrogate pair");
        require(string_arguments::encode(json(bytes({0xf4,0x8f,0xbf,0xbf})))==u"\U0010FFFF","Последняя Unicode scalar value должна поддерживаться");
        require(string_arguments::encode(json(bytes({0xc2,0x80})))==u"\u0080","Минимальный корректный двухбайтовый UTF-8 должен поддерживаться");
        require(string_arguments::encode(json(bytes({0x7f})))==u"\u007F","Последний ASCII scalar должен поддерживаться");
        require(string_arguments::encode(json(bytes({0xdf,0xbf})))==u"\u07FF","Последний двухбайтовый символ должен поддерживаться");
        require(string_arguments::encode(json(bytes({0xe0,0xa0,0x80})))==u"\u0800","Минимальный трёхбайтовый символ должен поддерживаться");
        require(string_arguments::encode(json(bytes({0xef,0xbf,0xbf})))==u"\uFFFF","Последняя BMP codepoint должна поддерживаться");
        require(string_arguments::encode(json(bytes({0xed,0x9f,0xbf})))==u"\uD7FF","Scalar перед surrogate hole должен поддерживаться");
        require(string_arguments::encode(json(bytes({0xee,0x80,0x80})))==u"\uE000","Scalar после surrogate hole должен поддерживаться");
        require(string_arguments::encode(json(bytes({0xf0,0x90,0x80,0x80})))==u"\U00010000","Первый supplementary символ должен стать surrogate pair");
        const auto original=string_arguments::encode(json("Привет"));auto copy=original;copy[0]=u'X';
        require(original==u"Привет" && copy!=original,"Результат должен владеть собственной UTF-16 копией");

        const std::vector<std::string> malformed={bytes({0x80}),bytes({0xbf}),bytes({0xc0,0x80}),bytes({0xc1,0xbf}),
            bytes({0xc2}),bytes({0xc2,0x20}),bytes({0xc2,0xc2}),bytes({0xdf}),bytes({0xe0}),bytes({0xe0,0xa0}),
            bytes({0xe0,0x80,0x80}),bytes({0xe0,0x9f,0xbf}),bytes({0xe1,0x80}),bytes({0xe1,0x80,0x20}),
            bytes({0xed,0xa0,0x80}),bytes({0xed,0xaf,0xbf}),bytes({0xed,0xb0,0x80}),bytes({0xed,0xbf,0xbf}),
            bytes({0xf0}),bytes({0xf0,0x90}),bytes({0xf0,0x90,0x80}),bytes({0xf0,0x80,0x80,0x80}),
            bytes({0xf0,0x8f,0xbf,0xbf}),bytes({0xf1,0x80,0x80,0x20}),bytes({0xf4,0x90,0x80,0x80}),
            bytes({0xf5,0x80,0x80,0x80}),bytes({0xf7,0xbf,0xbf,0xbf}),bytes({0xf8,0x88,0x80,0x80,0x80}),
            bytes({0xfc,0x84,0x80,0x80,0x80,0x80}),bytes({0xfe}),bytes({0xff})};
        for(const auto& bad:malformed) {
            reject([&]{string_arguments::encode(json(bad));});
            reject([&]{string_arguments::encode(json("prefix"+bad+"suffix"));});
        }
        for(const auto& nul:std::vector<std::string>{std::string(1,'\0'),std::string("a\0b",3),std::string("tail\0",5)})
            reject([&]{string_arguments::encode(json(nul));});
        for(const auto& wrong:std::vector<json>{nullptr,true,false,0,-1,1.0,json::array(),json::object(),json{{"text","str"}}})
            reject([&]{string_arguments::encode(wrong);});

        static_assert(string_arguments::max_utf16_units==static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max())-1);
        constexpr std::size_t length=65536;
        require(string_arguments::encode(json(std::string(length,'a'))).size()==length,
                "Строка длиннее прежнего ограничения должна сохраняться без усечения");
        std::string pairs;
        for(std::size_t i=0;i<length/2;++i)pairs+=bytes({0xf0,0x9f,0x98,0x80});
        const auto accepted_pairs=string_arguments::encode(json(pairs));
        require(accepted_pairs.size()==length && accepted_pairs.front()==0xd83d && accepted_pairs.back()==0xde00,
                "Длинная Unicode строка должна сохранять surrogate pairs");
        require(string_arguments::encode(json(pairs+"a")).size()==length+1,
                "Дополнение длинной строки не должно возвращать искусственный предел");
        std::string cyrillic;
        for(std::size_t i=0;i<length;++i)cyrillic+="я";
        require(string_arguments::encode(json(cyrillic)).size()==length,
                "Длинная кириллица должна сохраняться независимо от размера UTF-8");
        std::string three_bytes;
        for(std::size_t i=0;i<length;++i)three_bytes+=bytes({0xe0,0xa0,0x80});
        require(string_arguments::encode(json(three_bytes)).size()==length,"Длинные BMP символы не должны усекаться");
        std::cout<<"string_arguments: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
