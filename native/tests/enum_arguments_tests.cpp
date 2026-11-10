#include "sn2_mcp/enum_arguments.hpp"

#include <array>
#include <iostream>
#include <limits>
#include <string>
#include <vector>

namespace {
using sn2_mcp::enum_values::Integer;
using sn2_mcp::enum_values::Entry;
using sn2_mcp::enum_arguments::encode_integer;
using sn2_mcp::enum_arguments::contains_value;
using json = nlohmann::json;
std::size_t checks{};

void require(bool condition, const char* reason) {
    if (!condition) throw std::runtime_error(reason);
    ++checks;
}
template<class Operation> void rejects(Operation&& operation, const char* code = "invalid_argument") {
    try { operation(); }
    catch (const sn2_mcp::HandlerError& error) {
        require(error.code() == code, "Неверный код отказа enum-аргумента");
        require(error.details().at("executed") == false && error.details().at("may_have_executed") == false,
                "Отказ аргумента объявлен выполненным");
        return;
    }
    throw std::runtime_error("Ожидался отказ enum-аргумента");
}
void round_trip(const char* type, int width, const json& value, const Integer& expected) {
    const auto result = encode_integer(type, width, value);
    require(result.size == static_cast<std::size_t>(width) && result.value == expected, "Неверный canonical integer");
    require(sn2_mcp::enum_values::decode_integer(type, width, std::span(result.bytes).first(result.size)) == expected,
            "Число изменилось при round-trip enum");
    for (std::size_t index = result.size; index < result.bytes.size(); ++index)
        require(result.bytes[index] == std::byte{}, "Байты вне размера enum не обнулены");
    std::array<std::byte, 9> unaligned{};
    std::memcpy(unaligned.data() + 1, result.bytes.data(), result.size);
    require(sn2_mcp::enum_values::decode_integer(type, width, std::span(unaligned).subspan(1, result.size)) == expected,
            "Невыровненная копия enum повреждена");
}
} // namespace

int main() {
    try {
        round_trip("ByteProperty", 1, 0, std::uint64_t{0});
        round_trip("ByteProperty", 1, std::uint64_t{255}, std::uint64_t{255});
        round_trip("IntProperty", 4, std::numeric_limits<std::int32_t>::min(), std::int64_t{std::numeric_limits<std::int32_t>::min()});
        round_trip("IntProperty", 4, std::uint64_t{std::numeric_limits<std::int32_t>::max()}, std::int64_t{std::numeric_limits<std::int32_t>::max()});
        round_trip("Int64Property", 8, std::numeric_limits<std::int64_t>::min(), std::numeric_limits<std::int64_t>::min());
        round_trip("Int64Property", 8, std::uint64_t{std::numeric_limits<std::int64_t>::max()}, std::int64_t{std::numeric_limits<std::int64_t>::max()});
        round_trip("UInt32Property", 4, std::uint64_t{std::numeric_limits<std::uint32_t>::max()}, std::uint64_t{std::numeric_limits<std::uint32_t>::max()});
        round_trip("UInt64Property", 8, std::numeric_limits<std::uint64_t>::max(), std::numeric_limits<std::uint64_t>::max());
        round_trip("UInt64Property", 8, std::int64_t{0}, std::uint64_t{0});

        rejects([] { encode_integer("ByteProperty", 1, -1); });
        rejects([] { encode_integer("ByteProperty", 1, 256); });
        rejects([] { encode_integer("IntProperty", 4, std::int64_t{std::numeric_limits<std::int32_t>::min()} - 1); });
        rejects([] { encode_integer("IntProperty", 4, std::uint64_t{std::numeric_limits<std::int32_t>::max()} + 1); });
        rejects([] { encode_integer("IntProperty", 4, std::numeric_limits<std::uint64_t>::max()); });
        rejects([] { encode_integer("Int64Property", 8, std::numeric_limits<std::uint64_t>::max()); });
        rejects([] { encode_integer("UInt32Property", 4, -1); });
        rejects([] { encode_integer("UInt32Property", 4, std::uint64_t{std::numeric_limits<std::uint32_t>::max()} + 1); });
        rejects([] { encode_integer("UInt64Property", 8, -1); });
        for (const auto& invalid : {json(true), json(false), json(1.0), json(0.25), json(nullptr),
                                   json("1"), json::object(), json::array()})
            rejects([&] { encode_integer("ByteProperty", 1, invalid); });
        rejects([] { encode_integer("ByteProperty", 8, 1); }, "unsupported_enum_underlying_layout");
        rejects([] { encode_integer("Int16Property", 2, 1); }, "unsupported_enum_underlying_type");
        rejects([] { encode_integer("FloatProperty", 4, 1); }, "unsupported_enum_underlying_type");

        const std::array<Entry, 4> entries{{{17, 0}, {18, 1}, {19, 7}, {20, 7}}};
        require(contains_value(Integer{std::int64_t{0}}, entries), "Объявленный ноль не найден");
        require(contains_value(Integer{std::uint64_t{7}}, entries), "Alias/non-contiguous значение не найдено");
        require(!contains_value(Integer{std::int64_t{2}}, entries), "Индекс принят вместо значения enum");
        require(!contains_value(Integer{std::int64_t{3}}, entries), "Незадекларированная flags-комбинация принята");
        require(!contains_value(Integer{std::numeric_limits<std::uint64_t>::max()}, entries), "Unsigned значение перешло в signed");
        require(!contains_value(Integer{std::int64_t{0}}, {}), "Пустая таблица приняла значение");
        const std::array<Entry, 2> signed_entries{{{0, -1}, {0, std::numeric_limits<std::int64_t>::min()}}};
        require(contains_value(Integer{std::int64_t{-1}}, signed_entries), "Отрицательный перечислитель не найден");
        require(contains_value(Integer{std::numeric_limits<std::int64_t>::min()}, signed_entries), "Int64 MIN перечислитель не найден");
        require(!contains_value(Integer{std::numeric_limits<std::uint64_t>::max()}, signed_entries), "UINT64_MAX ошибочно совпал с -1");
        std::vector<Entry> extended(4097);
        for(std::size_t index=0;index<extended.size();++index)
            extended[index]={static_cast<std::uint64_t>(index+1),static_cast<std::int64_t>(index)};
        require(contains_value(Integer{std::int64_t{4096}}, extended), "Последнее объявленное значение выше прежнего предела не найдено");
        require(contains_value(Integer{std::uint64_t{4096}}, extended), "Представимое unsigned значение выше прежнего предела не найдено");
        require(!contains_value(Integer{std::int64_t{4097}}, extended), "Индекс за таблицей принят вместо объявленного числа");
        require(!contains_value(Integer{std::numeric_limits<std::uint64_t>::max()}, extended), "Большая таблица допустила unsigned-to-signed wrap");

        std::cout << "enum_arguments: " << checks << " проверок прошли\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
