#include "sn2_mcp/runtime_pak_writer.hpp"
#include "sn2_mcp/runtime_package_writer.hpp"
#include <fstream>
#include <iostream>

namespace {
using namespace sn2_mcp::runtime_package;
int checks{};
void require(bool value,const char* message){++checks;if(!value)throw std::runtime_error(message);}
template<class F>void rejects(F operation,const char* code){try{operation();}catch(const PackageError& error){require(error.code==code,"Rejected Pak has an incorrect diagnostic");return;}throw std::runtime_error("Malformed Pak was accepted");}
std::uint32_t u32(const std::vector<std::uint8_t>& bytes,std::size_t at){std::uint32_t value{};for(unsigned i=0;i<4;++i)value|=static_cast<std::uint32_t>(bytes[at+i])<<(8*i);return value;}
void checks_pak(){
    require(pak_sha1({})==std::array<std::uint8_t,20>{0xda,0x39,0xa3,0xee,0x5e,0x6b,0x4b,0x0d,0x32,0x55,0xbf,0xef,0x95,0x60,0x18,0x90,0xaf,0xd8,0x07,0x09},"SHA-1 must match the standard empty input vector");
    const PakOptions options{"../../../Subnautica2/Mods/Example/",0};
    const std::vector<PakFile> files={{"Config/mod-build.json",{'{','}','\n'}},{"Content/metadata.bin",{0,1,2,3}},{"readme.txt",{'m','o','d'}}};
    const auto archive=write_pak(options,files);const auto parsed=read_pak(archive.bytes);
    require(parsed.files.size()==3&&parsed.files[1].bytes==files[1].bytes,"A real Pak file must preserve each payload");
    require(parsed.options.mount_point==options.mount_point,"Pak mount FString must preserve its virtual root");
    const auto roundtrip=write_pak(parsed.options,parsed.files);require(roundtrip.bytes==archive.bytes,"The actual Pak full-directory wire should roundtrip byte for byte");
    require(archive.index_offset==53*3+10,"Payload entry headers must be 53 bytes and exclude compression blocks");
    const auto footer=archive.bytes.size()-221;require(u32(archive.bytes,footer+17)==0x5a6f12e1&&u32(archive.bytes,footer+21)==11,"Pak v11 footer requires its fixed source-defined magic/version offsets");
    require(archive.directory_offset==archive.index_offset+archive.index_size,"Full directory data must follow the primary index");
    require(archive.directory_offset+archive.directory_size==static_cast<std::int64_t>(footer),"Footer must directly follow the full directory index");
    std::vector<PakFile> unicode={{"\xd0\xbc\xd0\xbe\xd0\xb4/\xf0\x9f\xa7\xaa.json",{'{','}'}}};
    const auto unicode_pak=write_pak(options,unicode);require(read_pak(unicode_pak.bytes).files[0].relative_path==unicode[0].relative_path,"Pak filenames must preserve UTF-16 and surrogate pairs");
    std::vector<PakFile> duplicates={{"A.json",{}},{"a.json",{}}};rejects([&]{write_pak(options,duplicates);},"duplicate_pak_path");
    std::vector<PakFile> traversal={{"../A.json",{}}};rejects([&]{write_pak(options,traversal);},"invalid_pak_path");
    auto corrupt=archive.bytes;corrupt[static_cast<std::size_t>(archive.index_offset)]^=1;rejects([&]{read_pak(corrupt);},"invalid_pak_index_hash");
    corrupt=archive.bytes;corrupt[static_cast<std::size_t>(archive.directory_offset)]^=1;rejects([&]{read_pak(corrupt);},"invalid_pak_directory_hash");
    corrupt=archive.bytes;corrupt[53]^=1;rejects([&]{read_pak(corrupt);},"invalid_pak_payload_hash");
    corrupt=archive.bytes;corrupt[footer+21]=10;rejects([&]{read_pak(corrupt);},"unsupported_pak_version");
}
}
int main(int argc,char** argv){try{checks_pak();if(argc==2){const PakOptions options{"../../../Subnautica2/Mods/RuntimeWriterFixture/",0};const std::string metadata="{\"kind\":\"runtime_writer_fixture\",\"format\":11}\n";const std::vector<PakFile> files={{"Config/mod-build.json",{metadata.begin(),metadata.end()}}};const auto file=write_pak(options,files);std::ofstream output(argv[1],std::ios::binary);output.write(reinterpret_cast<const char*>(file.bytes.data()),file.bytes.size());if(!output)throw std::runtime_error("Could not write the explicitly requested local fixture Pak");}
    else if(argc!=1)throw std::runtime_error("Optionally pass one output path for the neutral local fixture Pak");std::cout<<"runtime_pak_writer: "<<checks<<" checks passed\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}}
