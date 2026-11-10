#include "sn2_mcp/process_identity.hpp"
#include <iostream>
#include <limits>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
void rejects(std::wstring_view text) {
    bool refused{};
    try { (void)sn2::loader::parse_creation_filetime(text); }
    catch (const std::runtime_error& error) { refused = std::string(error.what()) == "invalid_creation_filetime"; }
    require(refused, "invalid FILETIME accepted");
}
}

int main() {
    try {
        using namespace sn2::loader;
        require(parse_creation_filetime(L"1") == 1, "minimum FILETIME");
        require(parse_creation_filetime(L"134378677904730371") == 134378677904730371ULL, "actual FILETIME decimal");
        require(parse_creation_filetime(L"18446744073709551615") == std::numeric_limits<std::uint64_t>::max(), "maximum FILETIME");
        for (auto invalid : {L"", L"0", L"-1", L"+1", L" 1", L"1 ", L"1e4", L"1.0", L"123x",
                             L"18446744073709551616", L"100000000000000000000", L"１２３"}) rejects(invalid);
        FILETIME packed{0x12345678U, 0xABCDEF00U};
        require(filetime_value(packed) == 0xABCDEF0012345678ULL, "FILETIME word composition");
        require(identity_matches(L"C:/Игры/G1R.exe", 17, L"c:\\игры\\g1r.exe", 17), "case/slash normalization");
        require(identity_matches(L"\\\\?\\C:\\Game\\G1R.exe", 18, L"c:\\game\\g1r.exe", 18), "extended DOS path");
        require(identity_matches(L"\\\\?\\UNC\\Server\\Share\\Game.exe", 19, L"\\\\server\\share\\game.exe", 19), "extended UNC path");
        require(!identity_matches(L"C:\\Game\\G1R.exe", 17, L"D:\\Game\\G1R.exe", 17), "different EXE accepted");
        require(!identity_matches(L"C:\\Game\\G1R.exe", 17, L"C:\\Game\\G1R.exe", 18), "different creation time accepted");
        require(!identity_matches(L"C:\\Game\\G1R.exe", 17, L"C:\\Game\\Other.exe", 17), "different name accepted");

        // Живой собственный HANDLE проверяется только на чтение, без запуска игры и загрузки DLL.
        HANDLE current = GetCurrentProcess();
        const auto first = creation_filetime(current);
        const auto image = process_image(current);
        require(first != 0 && !image.empty(), "own process identity empty");
        require(first == creation_filetime(current), "creation FILETIME unstable");
        require(identity_matches(image, first, process_image(current), creation_filetime(current)), "own HANDLE mismatch");
        require(!identity_matches(image, first, image, first + 1), "own HANDLE wrong time accepted");
        bool bad_handle{};
        try { (void)creation_filetime(nullptr); }
        catch (const std::runtime_error& error) { bad_handle = std::string(error.what()) == "process_identity_failed"; }
        require(bad_handle, "invalid HANDLE accepted");
        std::cout << "Проверки идентичности загрузчика прошли\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
