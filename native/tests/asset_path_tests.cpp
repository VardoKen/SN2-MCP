#include <sn2_mcp/asset_path.hpp>

#include <array>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using sn2_mcp::asset_path::Parsed;
using sn2_mcp::asset_path::parse;
int checks{};

void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}

void reject(const std::string& path) {
    try {parse(path);}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()=="invalid_asset_path","Malformed paths require an explicit invalid_asset_path error");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
            "Path validation must reject before any engine operation");
        return;
    }
    throw std::runtime_error("A malformed top-level asset path was accepted");
}

void check_frame(std::uint64_t package,std::uint64_t asset) {
    const auto frame=sn2_mcp::asset_path::parameter_frame(package,asset);
    require(frame.size()==48 && frame.bytes().size()==48,"The reflected soft-load parameter frame requires exactly 48 bytes");
    require(sn2_mcp::asset_path::return_offset==40,"The UObject return slot must begin at offset 40");
    require(reinterpret_cast<std::uintptr_t>(frame.data())%16==0,"The real parameter address must be aligned to 16 bytes");
    std::uint64_t copied_package{},copied_asset{};
    std::memcpy(&copied_package,frame.data()+8,8);
    std::memcpy(&copied_asset,frame.data()+16,8);
    require(copied_package==package && copied_asset==asset,"Both complete FNames must occupy the inspected soft-path offsets");
    for(std::size_t offset=0;offset<48;++offset) {
        if(offset>=8 && offset<24)continue;
        require(frame.data()[offset]==0,"Weak cache, subpath owner fields and return slot must all start at zero");
    }
}
}

int main() {
    try {
        require(parse("/Game/Mods/Example/ModActor.ModActor_C")==Parsed{"/Game/Mods/Example/ModActor","ModActor_C"},
            "Blueprint generated-class names must be independent of the package basename");
        require(parse("/PluginName/Folder/Asset.Asset")==Parsed{"/PluginName/Folder/Asset","Asset"},
            "Plugin mount paths must retain their package root");
        require(parse("/Script/Engine.World")==Parsed{"/Script/Engine","World"},
            "Canonical script package paths must split the same way as content paths");
        require(parse("/Game/Folder-Dash/Asset2.Asset_3")==Parsed{"/Game/Folder-Dash/Asset2","Asset_3"},
            "Valid identifier punctuation must not be replaced by an ASCII-only policy");
        const std::string unicode="/Game/\xe6\xb5\x8b\xe8\xaf\x95/\xd0\x90\xd0\xba\xd1\x82\xd0\xbe\xd1\x80.\xd0\x90\xd0\xba\xd1\x82\xd0\xbe\xd1\x80_C";
        require(parse(unicode).asset_name=="\xd0\x90\xd0\xba\xd1\x82\xd0\xbe\xd1\x80_C","Valid UTF-8 asset names must remain unchanged");
        const auto long_package="/Game/"+std::string(4096,'a')+".Asset";
        require(parse(long_package).package_name.size()==4102,"The path parser must not impose an arbitrary length ceiling");
        for(const auto& path:std::vector<std::string>{"","Game/Asset.Asset","/","/Game.Asset","/Game/Asset",
            "/Game/Asset.","/Game/.Asset","/Game//Asset.Asset","//Game/Asset.Asset","/Game/Asset/.Asset",
            "/Game/./Asset.Asset","/Game/../Asset.Asset","/Game/Folder.Name/Asset.Asset","/Game/A.A.B",
            "/Game/A.A/Child","/Game/A.A:Child","/Game/A.A:","Blueprint'/Game/A.A'","/Game\\A.A",
            "/Game/A A.A"," /Game/A.A","/Game/A.A ","/Game/A\tA.A","/Game/A\nA.A","/Game/A\rA.A",
            "/Game/A.A?","/Game/A*.A","/Game/A|A.A","/Game/A<A.A","/Game/A>A.A","/Game/A\"A.A",
            "/Game/A,A.A","/Game/A&A.A","/Game/A!A.A","/Game/A~A.A","/Game/A@A.A","/Game/A#A.A",
            "/Game/A;A.A","/Game/A$A.A","/Game/A^A.A"})reject(path);
        auto nul=std::string("/Game/A.A");nul.insert(7,1,'\0');reject(nul);
        auto control=std::string("/Game/A.A");control.insert(7,1,'\x7f');reject(control);
        for(const auto& bytes:std::vector<std::string>{"\x80","\xc0\xaf","\xc2","\xe0\x80\x80","\xed\xa0\x80",
            "\xf0\x80\x80\x80","\xf4\x90\x80\x80","\xf5\x80\x80\x80","\xc2\x20"})reject("/Game/"+bytes+".A");
        for(const auto& bytes:std::vector<std::string>{"\xc2\x85","\xc2\xa0","\xe1\x9a\x80","\xe2\x80\x82",
            "\xe2\x80\xa8","\xe2\x80\xaf","\xe2\x81\x9f","\xe3\x80\x80"})reject("/Game/A"+bytes+"A.A");
        check_frame(0x0123456789abcdefull,0xfedcba9876543210ull);
        check_frame(0,0);
        auto first=sn2_mcp::asset_path::parameter_frame(11,12);
        auto second=sn2_mcp::asset_path::parameter_frame(21,22);
        first.data()[40]=0x7f;
        require(second.data()[40]==0,"Separate calls must own independent host parameter frames");
        std::cout<<"asset_path: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
