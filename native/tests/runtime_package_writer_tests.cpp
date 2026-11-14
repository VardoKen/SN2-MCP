#include "sn2_mcp/runtime_package_writer.hpp"
#include "sn2_mcp/runtime_iostore_writer.hpp"

#include <algorithm>
#include <fstream>
#include <iostream>
#include <iterator>
#include <cstring>
#include <iomanip>
#include <sstream>
#include <utility>

namespace {
using namespace sn2_mcp::runtime_package;
int checks{};
void require(bool value,const char* message){++checks;if(!value)throw std::runtime_error(message);}
template<class F> void rejects(F operation,const std::string& code){try{operation();}
    catch(const PackageError& error){require(error.code==code,"The rejected package has the wrong diagnostic");return;}
    throw std::runtime_error("Malformed package data was accepted");}
std::vector<std::uint8_t> load(const char* path){std::ifstream file(path,std::ios::binary);
    if(!file)throw std::runtime_error("The supplied cooked golden file is unavailable");
    return {std::istreambuf_iterator<char>(file),std::istreambuf_iterator<char>()};}
void save(const std::string& path,const std::vector<std::uint8_t>& bytes){std::ofstream file(path,std::ios::binary);if(!file)throw std::runtime_error("The requested local wire artifact cannot be written");file.write(reinterpret_cast<const char*>(bytes.data()),static_cast<std::streamsize>(bytes.size()));if(!file)throw std::runtime_error("Writing the local wire artifact failed");}
std::string json_string(const std::string& text){
    std::ostringstream output;output<<'"';
    for(const unsigned char value:text){
        if(value=='"'||value=='\\')output<<'\\'<<static_cast<char>(value);
        else if(value<32)output<<"\\u00"<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(value)<<std::dec;
        else output<<static_cast<char>(value);
    }
    output<<'"';return output.str();
}
template<class Range>void json_numbers(std::ostream& output,const Range& values){
    output<<'[';bool first=true;for(const auto value:values){if(!first)output<<',';first=false;output<<value;}output<<']';
}
void json_name(std::ostream& output,const Package& package,NameRef name){
    output<<"{\"index\":"<<name.index<<",\"number\":"<<name.number
          <<",\"text\":"<<json_string(package.names.at(name.index).text)<<'}';
}
std::string hex_hash(std::span<const std::uint8_t> bytes){
    std::ostringstream output;for(auto value:io_hash(bytes))output<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<unsigned>(value);return output.str();
}
void inspect_package(const char* header_path,const char* payload_path){
    const auto header=load(header_path),payload=load(payload_path);const auto parsed=read(header,payload);
    const auto again=write(parsed.package);const bool roundtrip=again.uasset==header&&again.uexp==payload;
    if(!roundtrip)throw PackageError("package_roundtrip_mismatch","The parsed package does not reproduce its original byte pair");
    const auto& package=parsed.package;const auto& layout=parsed.layout;
    std::cout<<"{\"schema\":1,\"kind\":\"runtime_package_inspection\",\"state\":\"succeeded\",\"byte_roundtrip\":true"
             <<",\"uasset\":{\"path\":"<<json_string(header_path)<<",\"bytes\":"<<header.size()<<",\"io_hash\":"<<json_string(hex_hash(header))<<'}'
             <<",\"uexp\":{\"path\":"<<json_string(payload_path)<<",\"bytes\":"<<payload.size()<<",\"io_hash\":"<<json_string(hex_hash(payload))<<'}'
             <<",\"package_name\":"<<json_string(package.name)<<",\"unversioned_file\":"<<(package.unversioned_file?"true":"false")
             <<",\"flags\":"<<package.flags<<",\"ue4_version\":"<<package.ue4_version<<",\"ue5_version\":"<<package.ue5_version
             <<",\"licensee_version\":"<<package.licensee_version<<",\"names_referenced_from_exports\":"<<package.names_referenced_from_exports;
    std::cout<<",\"layout\":{\"summary_size\":"<<layout.summary_size<<",\"header_size\":"<<layout.header_size
             <<",\"name_offset\":"<<layout.name_offset<<",\"soft_object_path_offset\":"<<layout.soft_object_path_offset
             <<",\"import_offset\":"<<layout.import_offset<<",\"export_offset\":"<<layout.export_offset
             <<",\"depends_offset\":"<<layout.depends_offset<<",\"asset_registry_offset\":"<<layout.asset_registry_offset
             <<",\"preload_offset\":"<<layout.preload_offset<<",\"preload_count\":"<<layout.preload_count
             <<",\"bulk_data_offset\":"<<layout.bulk_data_offset<<'}';
    std::cout<<",\"names\":[";
    for(std::size_t i=0;i<package.names.size();++i){if(i)std::cout<<',';const auto& name=package.names[i];
        std::cout<<"{\"index\":"<<i<<",\"text\":"<<json_string(name.text)<<",\"non_case_hash\":"<<name.non_case_hash<<",\"case_hash\":"<<name.case_hash<<'}';}
    std::cout<<"],\"imports\":[";
    for(std::size_t i=0;i<package.imports.size();++i){if(i)std::cout<<',';const auto& import=package.imports[i];
        std::cout<<"{\"package_index\":"<<-static_cast<std::int64_t>(i)-1<<",\"class_package\":";json_name(std::cout,package,import.class_package);
        std::cout<<",\"class_name\":";json_name(std::cout,package,import.class_name);std::cout<<",\"name\":";json_name(std::cout,package,import.name);
        std::cout<<",\"outer_index\":"<<import.outer<<",\"optional\":"<<(import.optional?"true":"false")<<'}';}
    std::cout<<"],\"exports\":[";
    for(std::size_t i=0;i<package.exports.size();++i){if(i)std::cout<<',';const auto& object=package.exports[i];
        std::cout<<"{\"package_index\":"<<i+1<<",\"name\":";json_name(std::cout,package,object.name);
        std::cout<<",\"class_index\":"<<object.class_index<<",\"super_index\":"<<object.super_index
                 <<",\"template_index\":"<<object.template_index<<",\"outer_index\":"<<object.outer_index
                 <<",\"object_flags\":"<<object.object_flags<<",\"package_flags\":"<<object.package_flags
                 <<",\"is_asset\":"<<(object.is_asset?"true":"false")<<",\"generate_public_hash\":"<<(object.generate_public_hash?"true":"false")
                 <<",\"serial_offset\":"<<layout.export_offsets[i]<<",\"serial_size\":"<<layout.export_sizes[i]
                 <<",\"script_serialization_start\":"<<object.script_serialization_start<<",\"script_serialization_end\":"<<object.script_serialization_end
                 <<",\"payload_io_hash\":"<<json_string(hex_hash(object.data.bytes))<<",\"dependencies\":{\"serialization_before_serialization\":";
        json_numbers(std::cout,object.dependencies.serialization_before_serialization);std::cout<<",\"create_before_serialization\":";
        json_numbers(std::cout,object.dependencies.create_before_serialization);std::cout<<",\"serialization_before_create\":";
        json_numbers(std::cout,object.dependencies.serialization_before_create);std::cout<<",\"create_before_create\":";
        json_numbers(std::cout,object.dependencies.create_before_create);std::cout<<"}}";}
    std::cout<<"],\"soft_object_paths\":[";
    for(std::size_t i=0;i<package.soft_object_paths.size();++i){if(i)std::cout<<',';const auto& path=package.soft_object_paths[i];
        std::cout<<"{\"package\":";json_name(std::cout,package,path.package);std::cout<<",\"asset\":";json_name(std::cout,package,path.asset);
        std::cout<<",\"subpath\":"<<json_string(path.subpath)<<'}';}
    std::cout<<"]}\n";
}
Package fixture(bool tagged=false) {
    Package p;p.name="/Game/RuntimeAuthoring/Fixture";
    if(!tagged)p.flags|=unversioned_properties_flag;
    p.names={make_name("None"),make_name("/Script/CoreUObject"),make_name("Package"),
        make_name("Class"),make_name("Object"),make_name("Fixture_C"),make_name("GetValue"),make_name("ReturnValue")};
    p.imports={{{1,0},{2,0},{1,0},0,false},{{1,0},{3,0},{4,0},-1,false}};
    Export type;type.name={5,0};type.class_index=-2;type.object_flags=9;
    ExportSink class_data;class_data.object(2);class_data.name({6,0});type.data=class_data.take();
    type.dependencies.create_before_serialization={2};
    Export function;function.name={6,0};function.class_index=-2;function.outer_index=1;function.object_flags=1;
    function.dependencies.create_before_create={1};
    ExportSink bytes;bytes.u8(0x0f);const std::array<NameRef,1> path={NameRef{7,0}};
    bytes.field_path(path,2);bytes.u8(0x48);bytes.field_path(path,2);bytes.u8(0x1f);bytes.bytes(std::array<std::uint8_t,4>{'o','k',0,0x53});
    function.data=bytes.take();p.exports={type,function};return p;
}
void portable_checks() {
    require(make_name("None")==NameEntry{"None",1012,3525},"UE None serialized hashes do not match the engine");
    require(make_name("GetServiceVersion")==NameEntry{"GetServiceVersion",59886,35754},"UE case-sensitive and deprecated name hashes do not match");
    require(make_name("BP_DevelopmentServices_C")==NameEntry{"BP_DevelopmentServices_C",10192,39519},"UE generated-class hashes do not match");
    require(make_name(std::string(1023,'x')).text.size()==1023,"The maximum valid target FName extent must remain usable");
    rejects([&]{make_name(std::string(1024,'x'));},"package_name_extent");
    for(bool tagged:{false,true}) {
        auto p=fixture(tagged);const auto written=write(p);const auto parsed=read(written.uasset,written.uexp);
        require(parsed.package.name==p.name && parsed.package.names==p.names && parsed.package.imports==p.imports,"Package identity/name/import tables were not preserved");
        require(parsed.package.exports.size()==2 && parsed.package.exports[1].outer_index==1,"Export outer/class references were not preserved");
        require(parsed.package.exports[1].data.bytes==p.exports[1].data.bytes,"The serialized field-path script payload changed");
        require(parsed.package.exports[1].dependencies==p.exports[1].dependencies,"Preload phase dependencies were not preserved");
        const auto again=write(parsed.package);require(again.uasset==written.uasset && again.uexp==written.uexp,"The current no-bulk cooked wire format failed byte roundtrip");
        require(parsed.layout.export_offsets.front()==parsed.layout.header_size,"Export offsets must be absolute across .uasset and .uexp");
        require(parsed.layout.bulk_data_offset==static_cast<std::int64_t>(written.uasset.size()+written.uexp.size()-4),"Bulk offset must precede the .uexp file-tag footer");
    }
    auto unicode=fixture();unicode.name="/Game/\xd0\x9c\xd0\xbe\xd0\xb4/\xf0\x9f\xa7\xaa";
    unicode.names.push_back(make_name("\xd0\x9c\xd0\xbe\xd0\xb4_\xf0\x9f\xa7\xaa"));
    unicode.soft_object_paths.push_back({{1,0},{5,0},"\xe5\xad\x90"});
    const auto unicode_wire=write(unicode);const auto unicode_read=read(unicode_wire.uasset,unicode_wire.uexp);
    require(unicode_read.package.name==unicode.name && unicode_read.package.names==unicode.names && unicode_read.package.soft_object_paths==unicode.soft_object_paths,"UTF-16 serialized strings must preserve Unicode and surrogate pairs");
    const auto subpath_offset=static_cast<std::size_t>(unicode_wire.layout.soft_object_path_offset)+16;
    const std::array<std::uint8_t,7> subpath_wire={3,0,0,0,0xe5,0xad,0x90};
    require(std::equal(subpath_wire.begin(),subpath_wire.end(),unicode_wire.uasset.begin()+subpath_offset),"Current soft subpaths must use UTF-8 lengths without a NUL terminator");
    auto versioned=fixture();versioned.unversioned_file=false;versioned.custom_versions={{{1,2,3,4},7}};
    versioned.saved_engine={5,6,1,131439,"branch"};versioned.compatible_engine=versioned.saved_engine;
    auto versioned_wire=write(versioned);auto versioned_read=read(versioned_wire.uasset,versioned_wire.uexp);
    require(versioned_read.package.custom_versions==versioned.custom_versions && versioned_read.package.saved_engine==versioned.saved_engine,"Explicit file/custom/engine versions must survive their source-defined summary fields");
    auto bad=fixture();bad.exports[1].class_index=-3;rejects([&]{write(bad);},"invalid_package_object_reference");
    bad=fixture();bad.exports[1].data.references.front().object=1;rejects([&]{write(bad);},"invalid_package_reference_write");
    bad=fixture();bad.exports[1].data.references.front().path[0].index=999;rejects([&]{write(bad);},"invalid_package_name_reference");
    bad=fixture();bad.flags=0;rejects([&]{write(bad);},"unsupported_package_flags");
    bad=fixture();bad.ue5_version=1016;rejects([&]{write(bad);},"unsupported_package_schema");
    const auto good=write(fixture());auto truncated=good.uasset;truncated.pop_back();
    rejects([&]{read(truncated,good.uexp);},"invalid_package_extent");
    auto corrupt=good.uexp;corrupt.back()=0;rejects([&]{read(good.uasset,corrupt);},"invalid_package_trailer");
    ExportSink overwrite;overwrite.name({2,0});overwrite.seek(1);
    rejects([&]{overwrite.u8(1);},"partial_package_reference_overwrite");
    overwrite.seek(0);overwrite.bytes(std::array<std::uint8_t,8>{});require(overwrite.take().references.empty(),"An overwritten reference must not retain a stale fixup record");
    ExportSink replaced;replaced.name({1,0});replaced.seek(0);replaced.name({2,3});const auto replaced_name=replaced.take();
    require(replaced_name.references.size()==1 && replaced_name.references[0].name==NameRef{2,3},"A complete typed name rewrite must replace its reference record");
    ExportSink patched;patched.i32(0);patched.bytes(std::array<std::uint8_t,3>{1,2,3});patched.patch_i32(0,3);
    require(patched.tell()==7 && patched.size()==7 && patched.take().bytes==std::vector<std::uint8_t>{3,0,0,0,1,2,3},"A storage-size patch must preserve the serializer cursor");
}
void golden_checks(const char* header_path,const char* payload_path,const char* artifact_prefix=nullptr) {
    const auto header=load(header_path),payload=load(payload_path);const auto parsed=read(header,payload);
    require(parsed.package.name=="/SN2MCPDevelopmentServices/BP_DevelopmentServices","The external golden is not the service cooked package");
    require(parsed.layout.summary_size==290 && parsed.layout.header_size==7011,"The exact custom UE summary layout changed");
    require(parsed.package.flags==0x80002200 && parsed.package.names.size()==125 && parsed.package.imports.size()==43 && parsed.package.exports.size()==16,"The golden package tables were not decoded with their source-defined format");
    require(parsed.layout.import_offset==3691 && parsed.layout.export_offset==5067 && parsed.layout.preload_offset==6671 && parsed.layout.preload_count==85,"Golden table/dependency offsets do not match the cooked asset");
    for(const auto& name:parsed.package.names)require(make_name(name.text)==name,"A generated name hash differs from the actual cooked golden");
    const auto& function=parsed.package.exports[6];require(parsed.package.names[function.name.index].text=="GetServiceVersion" && function.data.bytes.size()==129,"The golden UFunction export is not the expected serialized function");
    require(function.data.bytes[65]==28 && function.data.bytes[69]==44,"Runtime bytecode size and persistent storage size must remain distinct");
    const auto again=write(parsed.package);require(again.uasset==header && again.uexp==payload,"Writing the parsed real cooked service must reproduce both files byte for byte");
    const auto io=write_io_store(parsed.package,again,{"SN2MCPDevelopmentServicesSubnautica2-Windows","../../../Subnautica2/Mods/SN2MCPDevelopmentServices/Content/BP_DevelopmentServices.uasset"});
    require(io.id==0x0022aed07386c53c,"Golden IoStore package ID differs from the actual UToc");
    require(io.public_export_hashes[0]==0x0436d8914db13966,"Golden generated-class public export hash differs from the engine source calculation");
    if(artifact_prefix){const std::string path=artifact_prefix;save(path+".zen",io.zen_package);save(path+".container-header",io.container_header);save(path+".utoc",io.utoc);save(path+".ucas",io.ucas);}
}
void retained_zen_checks(const char* path,const char* header_path,const char* payload_path){
    const auto previous=load(path),header=load(header_path),payload=load(payload_path);
    const auto parsed=read(header,payload);const auto io=write_io_store(parsed.package,write(parsed.package),
        {"SN2MCPDevelopmentServicesSubnautica2-Windows","../../../Subnautica2/Mods/SN2MCPDevelopmentServices/Content/BP_DevelopmentServices.uasset"});
    require(previous.size()>=52,"Retained Zen header is truncated");
    const auto old32=[&](std::size_t pos){std::uint32_t value{};for(unsigned i=0;i<4;++i)value|=static_cast<std::uint32_t>(previous[pos+i])<<(8*i);return value;};
    const auto new32=[&](std::size_t pos){std::uint32_t value{};for(unsigned i=0;i<4;++i)value|=static_cast<std::uint32_t>(io.zen_package[pos+i])<<(8*i);return value;};
    require(old32(4)==previous.size() && new32(4)==previous.size(),"The generated Zen header must match the retained actual header size");
    require(std::equal(previous.begin(),previous.begin()+32,io.zen_package.begin()),"Zen summary/name/import-prefix fields changed from the retained actual cooked header");
    const auto export_start=old32(32),bundle_start=old32(36);
    require(std::equal(previous.begin()+52,previous.begin()+export_start,io.zen_package.begin()+52),"Generated Zen name batch and script import IDs differ from the real engine container");
    require((bundle_start-export_start)/72==16,"Retained export-map size must match the current 16 source exports");
    for(std::size_t i=0;i<16;++i){const auto pos=export_start+i*72;
        require(std::equal(previous.begin()+pos+16,previous.begin()+pos+72,io.zen_package.begin()+pos+16),"Generated Zen export names/reference indexes/public hashes differ from the real container");}
    require(std::equal(previous.begin()+bundle_start,previous.end(),io.zen_package.begin()+bundle_start),"Generated export/dependency bundles differ from the actual engine container");
}
std::uint32_t read32(const std::vector<std::uint8_t>& bytes,std::size_t offset){require(offset<=bytes.size()&&4<=bytes.size()-offset,"Test parser field exceeds its input");
    std::uint32_t v{};for(unsigned i=0;i<4;++i)v|=static_cast<std::uint32_t>(bytes[offset+i])<<(8*i);return v;}
std::uint64_t read_compact(const std::vector<std::uint8_t>& bytes,std::size_t offset,unsigned count,bool big){require(offset<=bytes.size()&&count<=bytes.size()-offset,"Test parser compact field exceeds its input");
    std::uint64_t v{};for(unsigned i=0;i<count;++i)v|=static_cast<std::uint64_t>(bytes[offset+i])<<(8*(big?count-1-i:i));return v;}
void iostore_checks(){
    require(package_id("/SN2MCPDevelopmentServices/BP_DevelopmentServices")==0x0022aed07386c53c,"Target package IDs require lower UTF-16LE CityHash");
    require(zen_name_hash("None")==0x75370f5ba6352dc2 && zen_name_hash("GetServiceVersion")==0xd67362492a285820,"Zen ANSI names require the encoding-specific CityHash algorithm");
    require(script_import_index("/Script/Engine.Actor")==script_import_index("/script/engine/actor"),"Script imports must normalize separators and ASCII case");
    require(public_export_hash("/BP_DevelopmentServices_C")==0x0436d8914db13966,"Public export hashes require package-relative UTF-16 paths");
    const auto empty_hash=io_hash({});require(empty_hash==std::array<std::uint8_t,20>{0xaf,0x13,0x49,0xb9,0xf5,0xf9,0xa1,0xa6,0xa0,0x40,0x4d,0xea,0x36,0xdc,0xc9,0x49,0x9b,0xcb,0x25,0xc9},"IoStore hashes must be BLAKE3 truncated to 20 bytes");
    auto p=fixture();const auto cooked=write(p);IoStoreOptions options{"Fixture-Windows","../../../Subnautica2/Mods/Fixture/Content/Fixture.uasset"};
    const auto io=write_io_store(p,cooked,options);
    require(io.utoc.size()>260 && io.utoc[16]==8 && read32(io.utoc,20)==144 && read32(io.utoc,24)==2 && read32(io.utoc,28)==2,"Uncompressed indexed UToc must contain its real two chunks and blocks");
    require(io.utoc[80]==8 && read32(io.utoc,36)==0 && read32(io.utoc,84)==0 && read32(io.utoc,96)==0,"This container must use uncompressed, unencrypted indexed fallback lookup");
    require(io.utoc[155]==1 && io.utoc[167]==6,"ExportBundleData and ContainerHeader must use their actual chunk-type identifiers");
    const auto first_length=read_compact(io.utoc,173,5,true),second_offset=read_compact(io.utoc,178,5,true),second_length=read_compact(io.utoc,183,5,true);
    require(first_length==io.zen_package.size() && second_offset==65536 && second_length==io.container_header.size(),"Virtual offsets must be big-endian and aligned by compression blocks");
    const auto physical_first=read_compact(io.utoc,188,5,false),physical_second=read_compact(io.utoc,200,5,false);
    require(physical_first==0 && physical_second==(io.zen_package.size()+15)/16*16,"Physical block offsets must use little-endian padded disk positions");
    require(std::equal(io.zen_package.begin(),io.zen_package.end(),io.ucas.begin()),"The first disk chunk must contain the complete Zen package");
    require(std::equal(io.container_header.begin(),io.container_header.end(),io.ucas.begin()+physical_second),"The second disk chunk must contain the real package-store header");
    require(read32(io.container_header,0)==0x496f436e && read32(io.container_header,4)==5 && read32(io.container_header,16)==1,"The mounted container requires a current package-store header");
    const auto metadata=io.utoc.size()-48;const auto first_hash=io_hash(io.zen_package);
    require(std::equal(first_hash.begin(),first_hash.end(),io.utoc.begin()+metadata),"TOC metadata must hash the exact uncompressed chunk bytes");
    require(io.export_bundle.size()==4 && io.export_bundle.front()[1]==0,"Every export must have create and serialize commands in a valid topological order");
    p.exports[1].dependencies.serialization_before_create={1};p.exports[0].dependencies.create_before_serialization={2};
    const auto bad=write(p);rejects([&]{write_io_store(p,bad,options);},"cyclic_package_dependencies");
    options.script_import_exists=[](const auto&,auto){return false;};rejects([&]{write_io_store(fixture(),cooked,options);},"missing_iostore_script_import");
    auto large=fixture();large.exports[0].data.bytes.resize(70000);large.exports[0].data.references.clear();
    options.script_import_exists={};const auto large_io=write_io_store(large,write(large),options);
    require(read32(large_io.utoc,28)==3,"Large packages must produce multiple physical compression-block entries");
    auto imported=fixture();imported.names.push_back(make_name("/Game/First"));imported.names.push_back(make_name("/Game/Second"));
    imported.imports.push_back({{1,0},{2,0},{8,0},0,false});imported.imports.push_back({{1,0},{2,0},{9,0},0,false});
    const auto imported_io=write_io_store(imported,write(imported),options);
    require(imported_io.imported_package_ids.size()==2&&imported_io.imported_package_ids[0]<imported_io.imported_package_ids[1],"Imported package IDs must follow the Engine optimizer's sorted ID order");
    imported.names.push_back(make_name("/game/first"));imported.names.push_back(make_name("ExternalObject"));
    imported.imports.push_back({{1,0},{2,0},{10,0},0,false});imported.imports.push_back({{1,0},{3,0},{11,0},-5,false});
    const auto case_io=write_io_store(imported,write(imported),options);
    require(case_io.imported_package_ids.size()==2 && (case_io.import_map.back()>>62)==2,"Imported FName package identity must remain insensitive to ASCII case after deduplication");
}
}
int main(int argc,char** argv){try{
    if(argc>1&&std::string(argv[1])=="--inspect"){
        if(argc!=4)throw std::runtime_error("Pass --inspect followed by the uasset and uexp paths");
        inspect_package(argv[2],argv[3]);return 0;
    }
    portable_checks();iostore_checks();if(argc>=3&&argc<=5){golden_checks(argv[1],argv[2],argc==5?argv[4]:nullptr);if(argc>=4)retained_zen_checks(argv[3],argv[1],argv[2]);}
    else if(argc!=1)throw std::runtime_error("Pass both cooked files, an optional retained Zen header, and an optional local artifact prefix");
    std::cout<<"runtime_package_writer: "<<checks<<" checks passed"<<(argc==3?" (including actual cooked golden)":"")<<"\n";return 0;
}catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}}
