#include "sn2_mcp/runtime_iostore_writer.hpp"
#include "sn2_mcp/runtime_cityhash.hpp"
#include "blake3.h"

#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <type_traits>

namespace sn2_mcp::runtime_package {
namespace {
[[noreturn]] void error(const char* code,const char* message){throw PackageError(code,message);}
std::uint32_t count(std::size_t size){if(size>std::numeric_limits<std::uint32_t>::max())error("iostore_extent_overflow","An IoStore array exceeds its 32-bit wire field");return static_cast<std::uint32_t>(size);}
std::uint64_t align(std::uint64_t size,std::uint64_t boundary){if(size>std::numeric_limits<std::uint64_t>::max()-(boundary-1))error("iostore_extent_overflow","An IoStore alignment exceeds its wire extent");return (size+boundary-1)/boundary*boundary;}
struct Bytes {
    std::vector<std::uint8_t> data;
    template<class T>void scalar(T value){using U=std::make_unsigned_t<T>;const auto v=static_cast<U>(value);for(std::size_t i=0;i<sizeof(T);++i)data.push_back(static_cast<std::uint8_t>(v>>(8*i)));}
    void bytes(std::span<const std::uint8_t> value){data.insert(data.end(),value.begin(),value.end());}
    void string(const std::string& value){bytes(encode_string(value));}
    void pad(std::uint64_t boundary){const auto size=align(data.size(),boundary);if(size>data.max_size())error("iostore_extent_overflow","An IoStore buffer exceeds the addressable extent");data.resize(static_cast<std::size_t>(size));}
    void patch32(std::size_t offset,std::uint32_t value){if(offset>data.size() || 4>data.size()-offset)error("invalid_iostore_patch","A Zen header patch exceeds its allocated bytes");for(unsigned i=0;i<4;++i)data[offset+i]=static_cast<std::uint8_t>(value>>(8*i));}
};
std::string lower(std::string value){for(auto& c:value)if(c>='A'&&c<='Z')c=static_cast<char>(c+32);return value;}
std::vector<std::uint8_t> wide_bytes(const std::string& value){Bytes bytes;for(auto unit:string_utf16(value))bytes.scalar(unit);return std::move(bytes.data);}
std::uint64_t hash(std::span<const std::uint8_t> value){return city_hash64(reinterpret_cast<const char*>(value.data()),count(value.size()));}
std::string name(const Package& package,NameRef ref){if(ref.index<0 || static_cast<std::size_t>(ref.index)>=package.names.size() || ref.number<0)error("invalid_iostore_name","A mapped name is not present in the legacy name table");
    auto text=package.names[ref.index].text;if(ref.number)text+="_"+std::to_string(ref.number-1);return text;}
struct NameMap {
    std::vector<std::string> names;
    void preserve(const std::string& value){if(names.size()>=0x40000000ull)error("iostore_extent_overflow","A mapped name exceeds its 30-bit wire index");names.push_back(value);}
    std::uint32_t add(const std::string& value){auto found=std::find(names.begin(),names.end(),value);if(found!=names.end())return static_cast<std::uint32_t>(found-names.begin());
        if(names.size()>=0x40000000ull)error("iostore_extent_overflow","A mapped name exceeds its 30-bit wire index");names.push_back(value);return static_cast<std::uint32_t>(names.size()-1);}
};
std::vector<std::uint8_t> name_batch(const std::vector<std::string>& names){Bytes writer;writer.scalar(count(names.size()));if(names.empty())return std::move(writer.data);
    Bytes hashes,headers,strings;
    for(const auto& value:names){const auto units=string_utf16(value);if(units.size()>=1024)error("iostore_name_extent","A serialized name exceeds the target's NAME_SIZE buffer extent");
        const bool wide=std::any_of(units.begin(),units.end(),[](auto c){return c>=128;});hashes.scalar(zen_name_hash(value));
        headers.scalar<std::uint8_t>(static_cast<std::uint8_t>((wide?0x80:0)|(units.size()>>8)));headers.scalar<std::uint8_t>(static_cast<std::uint8_t>(units.size()));
        for(auto c:units){if(wide)strings.scalar(c);else strings.scalar<std::uint8_t>(static_cast<std::uint8_t>(c));}}
    writer.scalar(count(strings.data.size()));writer.scalar<std::uint64_t>(0xc1640000);writer.bytes(hashes.data);writer.bytes(headers.data);writer.bytes(strings.data);return std::move(writer.data);}
struct ResolvedImport {std::string full,package;bool script{},root{};};
std::vector<ResolvedImport> imports(const Package& package){std::vector<ResolvedImport> values(package.imports.size());std::vector<unsigned> states(values.size());
    std::function<void(std::size_t)> resolve=[&](std::size_t index){if(states[index]==2)return;if(states[index]==1)error("cyclic_package_import","Import outer references form a cycle");states[index]=1;
        const auto& import=package.imports[index];auto& value=values[index];if(!import.outer){value.package=name(package,import.name);value.full=lower(value.package);value.root=true;value.script=value.full.starts_with("/script/");}
        else {if(import.outer>=0 || -static_cast<std::int64_t>(import.outer)>static_cast<std::int64_t>(values.size()))error("invalid_iostore_import_outer","An import outer must refer to another imported object");
            const auto parent=static_cast<std::size_t>(-static_cast<std::int64_t>(import.outer)-1);resolve(parent);value.package=values[parent].package;value.script=values[parent].script;value.full=values[parent].full+"/"+lower(name(package,import.name));}
        states[index]=2;};
    for(std::size_t i=0;i<values.size();++i)resolve(i);return values;}
std::vector<std::string> export_paths(const Package& package,const std::vector<ResolvedImport>& imports){std::vector<std::string> values(package.exports.size());std::vector<unsigned> states(values.size());
    std::function<void(std::size_t)> resolve=[&](std::size_t index){if(states[index]==2)return;if(states[index]==1)error("cyclic_package_export","Export outer references form a cycle");states[index]=1;
        const auto& object=package.exports[index];std::string parent=lower(package.name);
        if(object.outer_index>0){const auto outer=static_cast<std::size_t>(object.outer_index-1);if(outer>=values.size())error("invalid_iostore_export_outer","An export outer exceeds its table");resolve(outer);parent=values[outer];}
        else if(object.outer_index<0){const auto outer=static_cast<std::size_t>(-static_cast<std::int64_t>(object.outer_index)-1);if(outer>=imports.size())error("invalid_iostore_export_outer","An export outer exceeds the import table");parent=imports[outer].full;}
        values[index]=parent+"/"+lower(name(package,object.name));states[index]=2;};for(std::size_t i=0;i<values.size();++i)resolve(i);return values;}
std::uint64_t object_index(std::int32_t value,const std::vector<std::uint64_t>& imports){if(value>0)return static_cast<std::uint64_t>(value-1);if(!value)return 0xffffffffffffffffull;
    const auto index=static_cast<std::size_t>(-static_cast<std::int64_t>(value)-1);if(index>=imports.size())error("invalid_iostore_import_index","A Zen object import exceeds its table");return imports[index];}
struct DependencyHeader {std::int32_t first{-1};std::array<std::uint32_t,4> counts{};};
struct Graph {std::vector<DependencyHeader> headers;std::vector<std::int32_t> entries;std::vector<std::array<std::uint32_t,2>> bundle;};
Graph graph(const Package& package,const std::vector<std::uint64_t>& imports){Graph result;const auto nodes=package.exports.size()*2;
    std::vector<std::set<std::size_t>> edges(nodes);std::vector<std::uint32_t> incoming(nodes);
    auto edge=[&](std::size_t from,std::size_t to){if(edges[from].insert(to).second)++incoming[to];};
    for(std::size_t i=0;i<package.exports.size();++i){edge(i*2,i*2+1);const auto& deps=package.exports[i].dependencies;DependencyHeader header;
        const std::array<const std::vector<std::int32_t>*,4> groups={&deps.create_before_create,&deps.serialization_before_create,&deps.create_before_serialization,&deps.serialization_before_serialization};
        std::size_t phase{};for(const auto* group:groups){for(auto dependency:*group){const auto to=i*2+(phase>=2?1:0),from_phase=phase%2;
                if(dependency>0)edge(static_cast<std::size_t>(dependency-1)*2+from_phase,to);
                const bool script=dependency<0 && (object_index(dependency,imports)>>62)==1;
                if(!script){if(header.first<0)header.first=static_cast<std::int32_t>(count(result.entries.size()));result.entries.push_back(dependency);++header.counts[phase];}}
            ++phase;}result.headers.push_back(header);}
    auto before=[&](std::size_t a,std::size_t b){const bool ap=(package.exports[a/2].object_flags&1)||package.exports[a/2].generate_public_hash;
        const bool bp=(package.exports[b/2].object_flags&1)||package.exports[b/2].generate_public_hash;
        if(ap!=bp)return ap;if(a%2!=b%2)return a%2<b%2;return a/2<b/2;};
    std::vector<std::size_t> ready;for(std::size_t n=0;n<nodes;++n)if(!incoming[n])ready.push_back(n);
    while(!ready.empty()){const auto it=std::min_element(ready.begin(),ready.end(),before);const auto n=*it;ready.erase(it);
        result.bundle.push_back({static_cast<std::uint32_t>(n/2),static_cast<std::uint32_t>(n%2)});for(auto to:edges[n])if(!--incoming[to])ready.push_back(to);}
    if(result.bundle.size()!=nodes)error("cyclic_package_dependencies","Preload dependencies create a cycle between export creation and serialization");return result;}
std::vector<std::uint8_t> directory(const std::string& filename){const auto slash=filename.find_last_of('/');if(slash==std::string::npos || slash+1==filename.size())error("invalid_iostore_filename","An indexed package needs a virtual directory and filename");
    Bytes writer;writer.string(filename.substr(0,slash+1));writer.scalar<std::uint32_t>(1);
    writer.scalar<std::uint32_t>(0xffffffff);writer.scalar<std::uint32_t>(0xffffffff);writer.scalar<std::uint32_t>(0xffffffff);writer.scalar<std::uint32_t>(0);
    writer.scalar<std::uint32_t>(1);writer.scalar<std::uint32_t>(0);writer.scalar<std::uint32_t>(0xffffffff);writer.scalar<std::uint32_t>(0);
    writer.scalar<std::uint32_t>(1);writer.string(filename.substr(slash+1));return std::move(writer.data);}
void chunk_id(Bytes& writer,std::uint64_t id,std::uint8_t type){writer.scalar(id);writer.scalar<std::uint16_t>(0);writer.scalar<std::uint8_t>(0);writer.scalar(type);}
void compact(Bytes& writer,std::uint64_t value,unsigned bytes,bool big_endian){if(value>=(1ull<<(bytes*8)))error("iostore_extent_overflow","A compact IoStore offset or length exceeds its wire field");
    for(unsigned i=0;i<bytes;++i)writer.scalar<std::uint8_t>(static_cast<std::uint8_t>(value>>(8*(big_endian?bytes-1-i:i))));}
} // namespace
std::uint64_t package_id(const std::string& value){return hash(wide_bytes(lower(value)));}
std::uint64_t script_import_index(const std::string& value){auto text=lower(value);std::replace(text.begin(),text.end(),'.','/');std::replace(text.begin(),text.end(),':','/');return (hash(wide_bytes(text))&0x3fffffffffffffffull)|(1ull<<62);}
std::uint64_t public_export_hash(const std::string& value){if(value.size()<2||value.front()!='/')error("invalid_public_export_path","Public export hashing requires a nonempty package-relative path beginning with slash");return hash(wide_bytes(lower(value.substr(1))));}
std::uint64_t zen_name_hash(const std::string& value){const auto text=lower(value);const auto units=string_utf16(text);if(std::any_of(units.begin(),units.end(),[](auto c){return c>=128;}))return hash(wide_bytes(text));
    return city_hash64(text.data(),count(text.size()));}
std::array<std::uint8_t,20> io_hash(std::span<const std::uint8_t> bytes){blake3_hasher hasher;blake3_hasher_init(&hasher);blake3_hasher_update(&hasher,bytes.data(),bytes.size());std::array<std::uint8_t,20> result{};blake3_hasher_finalize(&hasher,result.data(),result.size());return result;}

IoStorePackage write_io_store(const Package& package,const CookedPackage& cooked,const IoStoreOptions& options){
    const auto parsed=read(cooked.uasset,cooked.uexp);const auto exact=write(package);
    if(exact.uasset!=cooked.uasset||exact.uexp!=cooked.uexp)error("iostore_package_mismatch","The serializer model differs from its cooked package bytes");
    if(options.container_name.empty()||options.package_filename.empty())error("invalid_iostore_options","Container and virtual package filenames are required");
    if(options.compression_block_size==0||options.compression_block_size>0xffffff)error("invalid_iostore_block_size","The block size cannot fit the Engine's 24-bit block extent");
    IoStorePackage result;result.id=package_id(package.name);result.container_id=package_id(options.container_name);
    const auto resolved=imports(package);const auto paths=export_paths(package,resolved);
    std::vector<std::string> imported_names;for(const auto& import:resolved)if(!import.script && import.root)imported_names.push_back(import.package);
    std::sort(imported_names.begin(),imported_names.end(),[](const auto& a,const auto& b){const auto aid=package_id(a),bid=package_id(b);return aid!=bid?aid<bid:lower(a)<lower(b);});
    imported_names.erase(std::unique(imported_names.begin(),imported_names.end(),[](const auto& a,const auto& b){return lower(a)==lower(b);}),imported_names.end());
    for(std::size_t i=1;i<imported_names.size();++i)if(package_id(imported_names[i-1])==package_id(imported_names[i]))
        error("iostore_package_hash_collision","Two imported package names have the same package ID");
    for(const auto& value:imported_names)result.imported_package_ids.push_back(package_id(value));
    std::vector<std::uint64_t> imported_hashes;
    for(const auto& import:resolved){if(import.script){const auto value=script_import_index(import.full);
            if(options.script_import_exists&&!options.script_import_exists(import.full,value))error("missing_iostore_script_import","A script import is absent from the target's global ScriptObjects registry");result.import_map.push_back(value);}
        else if(import.root)result.import_map.push_back(0xffffffffffffffffull);
        else {const auto position=std::find_if(imported_names.begin(),imported_names.end(),[&](const auto& value){return lower(value)==lower(import.package);});if(position==imported_names.end())error("invalid_iostore_package_import","An imported object has no root package");
            const auto relative=import.full.substr(lower(import.package).size());const auto public_hash=public_export_hash(relative);
            if(options.package_export_exists&&!options.package_export_exists(import.package,public_hash))error("missing_iostore_package_export","A package import is absent from the target's public export map");
            const auto index=static_cast<std::uint64_t>(position-imported_names.begin());result.import_map.push_back((2ull<<62)|(index<<32)|count(imported_hashes.size()));imported_hashes.push_back(public_hash);}}
    NameMap names;const auto referenced=package.names_referenced_from_exports<0?package.names.size():static_cast<std::size_t>(package.names_referenced_from_exports);
    // The initial names retain legacy payload indices, including a duplicate
    // spelling if one was harvested. Only additionally referenced names dedup.
    for(std::size_t i=0;i<referenced;++i)names.preserve(package.names[i].text);
    std::set<std::uint64_t> seen_hashes;Bytes exports;
    for(std::size_t i=0;i<package.exports.size();++i){const auto& object=package.exports[i];const bool is_public=(object.object_flags&1)||object.generate_public_hash;
        const auto pub=is_public?public_export_hash(paths[i].substr(lower(package.name).size())):0;
        if(is_public&&!seen_hashes.insert(pub).second)error("iostore_export_hash_collision","Two public exports have the same package-relative hash");result.public_export_hashes.push_back(pub);
        exports.scalar<std::uint64_t>(parsed.layout.export_offsets[i]-parsed.layout.header_size);exports.scalar<std::uint64_t>(parsed.layout.export_sizes[i]);
        exports.scalar(names.add(package.names[object.name.index].text));exports.scalar<std::uint32_t>(object.name.number);
        exports.scalar(object_index(object.outer_index,result.import_map));exports.scalar(object_index(object.class_index,result.import_map));exports.scalar(object_index(object.super_index,result.import_map));exports.scalar(object_index(object.template_index,result.import_map));
        exports.scalar(pub);exports.scalar(object.object_flags);exports.scalar<std::uint8_t>(object.not_for_client?1:object.not_for_server?2:0);exports.scalar<std::uint8_t>(0);exports.scalar<std::uint16_t>(0);}
    const auto mapped_package=names.add(package.name);const auto graph_data=graph(package,result.import_map);result.export_bundle=graph_data.bundle;
    Bytes zen;zen.data.resize(52);zen.patch32(0,package.unversioned_file?0:1);zen.patch32(8,mapped_package);zen.patch32(16,package.flags);zen.patch32(20,parsed.layout.header_size);
    if(!package.unversioned_file){zen.scalar<std::uint32_t>(3);zen.scalar(package.ue4_version);zen.scalar(package.ue5_version);zen.scalar(package.licensee_version);zen.scalar(count(package.custom_versions.size()));
        for(const auto& version:package.custom_versions){for(auto word:version.guid)zen.scalar(word);zen.scalar(version.version);}}
    const auto cells=zen.data.size();zen.scalar<std::uint32_t>(0);zen.scalar<std::uint32_t>(0);zen.bytes(name_batch(names.names));
    const auto pad=(8-(zen.data.size()+8)%8)%8;zen.scalar<std::uint64_t>(pad);zen.data.resize(zen.data.size()+pad);zen.scalar<std::uint64_t>(0);
    zen.patch32(24,count(zen.data.size()));for(auto value:imported_hashes)zen.scalar(value);zen.patch32(28,count(zen.data.size()));for(auto value:result.import_map)zen.scalar(value);
    zen.patch32(32,count(zen.data.size()));zen.bytes(exports.data);zen.patch32(cells,count(zen.data.size()));zen.patch32(cells+4,count(zen.data.size()));
    zen.patch32(36,count(zen.data.size()));for(auto entry:result.export_bundle){zen.scalar(entry[0]);zen.scalar(entry[1]);}
    zen.patch32(40,count(zen.data.size()));for(const auto& header:graph_data.headers){zen.scalar(header.first);for(auto value:header.counts)zen.scalar(value);}
    zen.patch32(44,count(zen.data.size()));for(auto value:graph_data.entries)zen.scalar(value);zen.patch32(48,count(zen.data.size()));zen.bytes(name_batch(imported_names));for(std::size_t i=0;i<imported_names.size();++i)zen.scalar<std::int32_t>(0);
    zen.patch32(4,count(zen.data.size()));zen.bytes({cooked.uexp.data(),cooked.uexp.size()-4});result.zen_package=std::move(zen.data);
    Bytes container;container.scalar<std::uint32_t>(0x496f436e);container.scalar<std::uint32_t>(5);container.scalar(result.container_id);container.scalar<std::uint32_t>(1);container.scalar(result.id);
    container.scalar<std::uint32_t>(16+count(result.imported_package_ids.size())*8);container.scalar(count(result.imported_package_ids.size()));container.scalar<std::uint32_t>(result.imported_package_ids.empty()?0:16);
    container.scalar<std::uint32_t>(0);container.scalar<std::uint32_t>(0);for(auto value:result.imported_package_ids)container.scalar(value);
    for(unsigned i=0;i<5;++i)container.scalar<std::uint32_t>(0);const auto soft=container.data.size()+16;container.scalar<std::uint64_t>(soft);container.scalar<std::uint64_t>(4);container.scalar<std::uint32_t>(0);result.container_header=std::move(container.data);
    const auto index=directory(options.package_filename);const std::array<std::span<const std::uint8_t>,2> chunks={result.zen_package,result.container_header};
    Bytes ucas,offsets,blocks;std::uint64_t virtual_offset{};
    for(auto chunk:chunks){compact(offsets,virtual_offset,5,true);compact(offsets,chunk.size(),5,true);
        for(std::size_t position=0;position<chunk.size();){const auto size=std::min<std::size_t>(options.compression_block_size,chunk.size()-position);
            compact(blocks,ucas.data.size(),5,false);compact(blocks,size,3,false);compact(blocks,size,3,false);blocks.scalar<std::uint8_t>(0);ucas.bytes(chunk.subspan(position,size));ucas.pad(16);position+=size;}
        virtual_offset+=align(chunk.size(),options.compression_block_size);}
    Bytes toc;toc.data.resize(144);constexpr char magic[]="-==--==--==--==-";std::copy(magic,magic+16,toc.data.begin());toc.data[16]=8;
    toc.patch32(20,144);toc.patch32(24,2);toc.patch32(28,count(blocks.data.size()/12));toc.patch32(32,12);toc.patch32(40,32);toc.patch32(44,options.compression_block_size);
    toc.patch32(48,count(index.size()));toc.patch32(52,1);for(unsigned i=0;i<8;++i)toc.data[56+i]=static_cast<std::uint8_t>(result.container_id>>(8*i));toc.data[80]=8;
    for(unsigned i=0;i<8;++i)toc.data[88+i]=0xff;chunk_id(toc,result.id,1);chunk_id(toc,result.container_id,6);toc.bytes(offsets.data);toc.bytes(blocks.data);toc.bytes(index);
    for(auto chunk:chunks){toc.bytes(io_hash(chunk));toc.scalar<std::uint32_t>(0);}result.utoc=std::move(toc.data);result.ucas=std::move(ucas.data);return result;
}
} // namespace sn2_mcp::runtime_package
