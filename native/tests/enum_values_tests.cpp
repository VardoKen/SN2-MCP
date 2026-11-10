#include <g1r_mcp/enum_values.hpp>
#include <array>
#include <iostream>
#include <limits>
#include <string>

using namespace g1r::enum_values;

static std::size_t checks = 0;
static void require(bool condition, const char* reason) {
    if (!condition) throw std::runtime_error(reason);
    ++checks;
}
template<class Callable> static void rejects(Callable&& call, const char* reason) {
    try { call(); } catch (const std::invalid_argument&) { ++checks; return; }
    throw std::runtime_error(reason);
}
template<class T> static std::span<const std::byte> bytes_of(const T& value) {
    return std::as_bytes(std::span(&value, 1));
}

int main() {
    try {
        const std::int32_t negative = -17;
        require(std::get<std::int64_t>(decode_integer("IntProperty", 4, bytes_of(negative))) == -17, "signed int32");
        const std::int64_t signed_min = std::numeric_limits<std::int64_t>::min();
        require(std::get<std::int64_t>(decode_integer("Int64Property", 8, bytes_of(signed_min))) == signed_min, "signed int64 min");
        const std::uint32_t unsigned_max = std::numeric_limits<std::uint32_t>::max();
        require(std::get<std::uint64_t>(decode_integer("UInt32Property", 4, bytes_of(unsigned_max))) == unsigned_max, "unsigned int32");
        const std::uint64_t large = std::numeric_limits<std::uint64_t>::max();
        const auto unrepresentable = decode_integer("UInt64Property", 8, bytes_of(large));
        require(std::get<std::uint64_t>(unrepresentable) == large, "unsigned int64 preserved");
        const std::uint8_t byte = 255;
        require(std::get<std::uint64_t>(decode_integer("ByteProperty", 1, bytes_of(byte))) == 255, "byte unsigned");
        rejects([&] { decode_integer("IntProperty", 8, bytes_of(negative)); }, "width mismatch must reject");
        rejects([&] { decode_integer("Int16Property", 2, bytes_of(negative)); }, "unknown type must reject");
        rejects([&] { decode_integer("FloatProperty", 4, bytes_of(negative)); }, "float enum must reject");
        rejects([&] { decode_integer("IntProperty", 4, bytes_of(byte)); }, "short value must reject");
        rejects([&] { integer_layout("IntProperty", -4); }, "negative size must reject");

        // Смещённая копия проверяет отсутствие требований к выравниванию исходного диапазона.
        std::array<std::byte, 5> unaligned{};
        std::memcpy(unaligned.data() + 1, &negative, sizeof(negative));
        require(std::get<std::int64_t>(decode_integer("IntProperty", 4, std::span(unaligned).subspan(1))) == -17, "unaligned int32");

        const std::array<Entry, 4> original{{{101, -17}, {202, 7}, {303, 7}, {404, 42}}};
        const auto entries = decode_entries(std::as_bytes(std::span(original)), 4, 8);
        require(entries.size() == 4 && entries[3].value == 42, "table copied");
        require(table_byte_size(0, 0, false) == 0 && decode_entries({}, 0, 9).empty(), "empty table");
        require(table_byte_size(1, std::numeric_limits<std::int32_t>::max(), true) == sizeof(Entry), "capacity is not read bound");
        rejects([] { table_byte_size(-1, 0, false); }, "negative count");
        rejects([] { table_byte_size(3, 2, true); }, "count above capacity");
        rejects([] { table_byte_size(1, 1, false); }, "missing data");
        require(table_byte_size(4097, 4097, true) == 4097 * sizeof(Entry), "entry count above old ceiling");
        require(table_byte_size(std::numeric_limits<std::int32_t>::max(), std::numeric_limits<std::int32_t>::max(), true)
                == static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()) * sizeof(Entry), "native int32 count range preserved");
        rejects([] { table_byte_size(0, -1, false); }, "negative capacity");
        rejects([&] { decode_entries(std::as_bytes(std::span(original)), 3, 4); }, "extra copied bytes");
        rejects([&] { decode_entries(std::as_bytes(std::span(original)).first(8), 1, 1); }, "short entry bytes");

        int calls = 0;
        auto decoder = [&](std::uint64_t name) { ++calls; return name == 202 ? std::string("Режим::Обычный") : std::string("Mode::Other"); };
        const auto match = find_label(Integer{std::int64_t{7}}, entries, decoder);
        require(match.status == "matched" && match.name == "Режим::Обычный" && match.matched_count == 2 && match.ambiguous, "aliases first label");
        require(calls == 1, "only chosen label decoded");
        const auto absent = find_label(Integer{std::int64_t{1}}, entries, decoder);
        require(absent.status == "not_found" && calls == 1, "numeric values are not table indices");
        const auto overflow = find_label(unrepresentable, entries, decoder);
        require(overflow.status == "value_unrepresentable" && calls == 1, "unsigned cannot wrap to signed name");
        require(find_label(Integer{std::int64_t{-17}}, entries, decoder).status == "matched", "negative label exact match");
        require(find_label(Integer{std::uint64_t{42}}, entries, decoder).status == "matched", "unsigned in signed range");
        const auto failed = find_label(Integer{std::int64_t{7}}, entries, [](auto) -> std::string { throw std::runtime_error("name decode"); });
        require(failed.status == "unavailable" && failed.reason == "enum_name_decode_failed" && failed.matched_count == 2, "callback failure preserves match information");
        const auto invalid = find_label(Integer{std::int64_t{7}}, entries, [](auto) { return std::string("\xC0\xAF"); });
        require(invalid.status == "unavailable" && invalid.reason == "invalid_enum_label", "invalid utf8 rejected");

        const std::string long_label(4097, 'a');
        require(valid_label("Имя::値😀") && valid_label(long_label), "valid unicode and label above old ceiling");
        require(!valid_label(""), "empty label rejected");
        require(!valid_label(std::string("a\0b", 3)) && !valid_label("\xED\xA0\x80"), "NUL and surrogate rejected");
        require(!valid_label("\xF4\x90\x80\x80") && !valid_label("\xE2\x82") && !valid_label("\x80"), "unicode bounds and truncated encoding");
        const std::array<Entry, 2> flags{{{1, 1}, {2, 2}}};
        require(find_label(Integer{std::int64_t{3}}, flags, decoder).status == "not_found", "flags are not decomposed");
        std::vector<Entry> extended(4097);
        for(std::size_t index=0;index<extended.size();++index)
            extended[index]={static_cast<std::uint64_t>(index+1),static_cast<std::int64_t>(index)};
        const auto extended_copy=decode_entries(std::as_bytes(std::span(extended)), 4097, 4097);
        extended.back().value=-1;
        int long_calls=0;
        const auto extended_match=find_label(Integer{std::int64_t{4096}}, extended_copy, [&](std::uint64_t name) {
            ++long_calls;require(name==4097,"label lookup did not reach last copied entry");return long_label;
        });
        require(extended_match.status=="matched" && extended_match.name==long_label
                && extended_match.matched_count==1 && !extended_match.ambiguous && long_calls==1,
                "last entry or long label was lost above old ceiling");
        auto aliased=extended_copy;aliased.front().value=4096;
        const auto extended_alias=find_label(Integer{std::int64_t{4096}},aliased,[&](auto name) {
            require(name==1,"alias lookup changed first matching entry");return long_label;
        });
        require(extended_alias.status=="matched" && extended_alias.matched_count==2 && extended_alias.ambiguous,
                "aliases lost beyond old entry ceiling");
        std::cout << "enum_values: " << checks << " проверок прошли\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
