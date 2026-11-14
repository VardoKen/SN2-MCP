#include "sn2_mcp/runtime_pak_writer.hpp"
#include "sn2_mcp/runtime_package_writer.hpp"
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#include <bcrypt.h>
#include <algorithm>
#include <limits>
#include <map>
#include <set>
#include <type_traits>

namespace sn2_mcp::runtime_package {
namespace {
// Format adapted from the acquired custom UE 5.6.1 FPakInfo/FPakEntry,
// FSecondaryIndexWriter and FPakFile directory-index serialization. The
// original source remains governed by its recorded Unreal terms.
constexpr std::uint32_t pak_magic=0x5a6f12e1;
constexpr std::size_t footer_size=221,entry_size=53;
[[noreturn]] void fail(const char* code,const char* message){throw PackageError(code,message);}
std::int32_t count(std::size_t value){if(value>static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()-1))fail("pak_extent_overflow","A Pak array exceeds its signed 32-bit index field");return static_cast<std::int32_t>(value);}
std::int64_t extent(std::size_t value){if(value>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))fail("pak_extent_overflow","A Pak byte extent exceeds its signed 64-bit wire field");return static_cast<std::int64_t>(value);}
struct Writer {
    std::vector<std::uint8_t> data;
    template<class T>void scalar(T value){using U=std::make_unsigned_t<T>;const auto v=static_cast<U>(value);for(std::size_t i=0;i<sizeof(T);++i)data.push_back(static_cast<std::uint8_t>(v>>(8*i)));}
    void bytes(std::span<const std::uint8_t> value){data.insert(data.end(),value.begin(),value.end());}
    void string(const std::string& value){bytes(encode_string(value));}
};
struct Reader {
    std::span<const std::uint8_t> data;std::size_t position{};
    void require(std::size_t size)const{if(position>data.size()||size>data.size()-position)fail("invalid_pak_extent","A Pak field exceeds its bounded input");}
    template<class T>T scalar(){require(sizeof(T));using U=std::make_unsigned_t<T>;U value{};for(std::size_t i=0;i<sizeof(T);++i)value|=static_cast<U>(data[position++])<<(8*i);return static_cast<T>(value);}
    std::span<const std::uint8_t> bytes(std::size_t size){require(size);const auto v=data.subspan(position,size);position+=size;return v;}
    std::int32_t elements(std::size_t minimum){const auto n=scalar<std::int32_t>();if(n<0||(minimum&&static_cast<std::uint64_t>(n)>(data.size()-position)/minimum))fail("invalid_pak_count","A Pak array count exceeds available bytes");return n;}
    bool boolean(){const auto value=scalar<std::int32_t>();if(value!=0&&value!=1)fail("invalid_pak_boolean","A Pak Boolean is not zero or one");return value!=0;}
};
void utf8(std::string& value,std::uint32_t cp){if(cp<0x80)value.push_back(static_cast<char>(cp));else if(cp<0x800){value.push_back(static_cast<char>(0xc0|(cp>>6)));value.push_back(static_cast<char>(0x80|(cp&63)));}
    else if(cp<0x10000){value.push_back(static_cast<char>(0xe0|(cp>>12)));value.push_back(static_cast<char>(0x80|((cp>>6)&63)));value.push_back(static_cast<char>(0x80|(cp&63)));}
    else{value.push_back(static_cast<char>(0xf0|(cp>>18)));value.push_back(static_cast<char>(0x80|((cp>>12)&63)));value.push_back(static_cast<char>(0x80|((cp>>6)&63)));value.push_back(static_cast<char>(0x80|(cp&63)));}}
std::string string(Reader& reader){const auto length=reader.scalar<std::int32_t>();if(!length)return {};
    if(length==std::numeric_limits<std::int32_t>::min())fail("invalid_pak_string","A Pak string count cannot be negated");std::string result;
    if(length>0){const auto input=reader.bytes(static_cast<std::size_t>(length));if(input.back()!=0)fail("invalid_pak_string","An ANSI Pak string has no terminator");for(std::size_t i=0;i+1<input.size();++i){if(input[i]==0)fail("invalid_pak_string","A Pak string contains an embedded terminator");utf8(result,input[i]);}}
    else{const auto size=static_cast<std::size_t>(-length);reader.require(size*2);for(std::size_t i=0;i<size;++i){const auto unit=reader.scalar<std::uint16_t>();if(i+1==size){if(unit)fail("invalid_pak_string","A Unicode Pak string has no terminator");break;}
        if(!unit)fail("invalid_pak_string","A Pak string contains an embedded terminator");if(unit>=0xd800&&unit<=0xdbff){if(i+2>=size)fail("invalid_pak_string","A Pak string has an incomplete surrogate");const auto low=reader.scalar<std::uint16_t>();++i;if(low<0xdc00||low>0xdfff)fail("invalid_pak_string","A Pak string has an invalid surrogate");utf8(result,0x10000+((unit-0xd800)<<10)+(low-0xdc00));}
        else{if(unit>=0xdc00&&unit<=0xdfff)fail("invalid_pak_string","A Pak string has an unmatched surrogate");utf8(result,unit);}}}return result;}
std::span<const std::uint8_t> section(std::span<const std::uint8_t> bytes,std::int64_t offset,std::int64_t size){if(offset<0||size<0||static_cast<std::uint64_t>(offset)>bytes.size()||static_cast<std::uint64_t>(size)>bytes.size()-static_cast<std::uint64_t>(offset))fail("invalid_pak_extent","A Pak section exceeds the file boundary");return bytes.subspan(static_cast<std::size_t>(offset),static_cast<std::size_t>(size));}
std::array<std::uint8_t,20> digest(Reader& reader){std::array<std::uint8_t,20> hash{};const auto bytes=reader.bytes(hash.size());std::copy(bytes.begin(),bytes.end(),hash.begin());return hash;}
void valid_mount(const std::string& path){if(path.empty()||path.back()!='/'||path.find('\\')!=std::string::npos)fail("invalid_pak_mount","A Pak mount point must use forward slashes and end with slash");(void)string_utf16(path);}
void valid_file(const std::string& path){if(path.empty()||path.front()=='/'||path.back()=='/'||path.find('\\')!=std::string::npos)fail("invalid_pak_path","Pak filenames must be mount-relative forward-slash paths");(void)string_utf16(path);
    std::size_t start{};while(start<path.size()){const auto end=path.find('/',start),size=(end==std::string::npos?path.size():end)-start;const auto part=path.substr(start,size);if(part.empty()||part=="."||part=="..")fail("invalid_pak_path","Pak filenames contain an empty or traversing path component");if(end==std::string::npos)break;start=end+1;}}
std::string folded(std::string path){for(auto& c:path)if(c>='A'&&c<='Z')c=static_cast<char>(c+32);return path;}
void entry(Writer& writer,const PakEntryLayout& value,bool index){writer.scalar(index?value.header_offset:std::int64_t{0});writer.scalar(value.size);writer.scalar(value.size);writer.scalar<std::uint32_t>(0);
    if(index)writer.bytes(std::array<std::uint8_t,20>{});else writer.bytes(value.hash);writer.scalar<std::uint8_t>(0);writer.scalar<std::uint32_t>(0);}
PakEntryLayout entry(Reader& reader){PakEntryLayout result;result.header_offset=reader.scalar<std::int64_t>();result.size=reader.scalar<std::int64_t>();const auto raw=reader.scalar<std::int64_t>();
    if(result.size<0||raw!=result.size||reader.scalar<std::uint32_t>()!=0)fail("unsupported_pak_entry","This Pak parser requires an uncompressed entry");result.hash=digest(reader);
    if(reader.scalar<std::uint8_t>()!=0||reader.scalar<std::uint32_t>()!=0)fail("unsupported_pak_entry","Encrypted/deleted/compression-block entries need a different parser");return result;}
} // namespace

std::array<std::uint8_t,20> pak_sha1(std::span<const std::uint8_t> bytes){BCRYPT_ALG_HANDLE algorithm{};BCRYPT_HASH_HANDLE handle{};std::vector<std::uint8_t> storage;std::array<std::uint8_t,20> result{};
    auto cleanup=[&]{if(handle)BCryptDestroyHash(handle);if(algorithm)BCryptCloseAlgorithmProvider(algorithm,0);};
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA1_ALGORITHM,nullptr,0)<0)fail("pak_hash_unavailable","Windows SHA-1 provider could not be opened");
    DWORD object_size{},returned{};if(BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&object_size),sizeof(object_size),&returned,0)<0){cleanup();fail("pak_hash_failed","Windows SHA-1 object size is unavailable");}
    storage.resize(object_size);if(BCryptCreateHash(algorithm,&handle,storage.data(),object_size,nullptr,0,0)<0){cleanup();fail("pak_hash_failed","Windows SHA-1 hash could not be created");}
    std::size_t position{};while(position<bytes.size()){const auto size=static_cast<ULONG>(std::min<std::size_t>(bytes.size()-position,std::numeric_limits<ULONG>::max()));
        if(BCryptHashData(handle,const_cast<PUCHAR>(bytes.data()+position),size,0)<0){cleanup();fail("pak_hash_failed","Windows SHA-1 hashing failed");}position+=size;}
    if(BCryptFinishHash(handle,result.data(),static_cast<ULONG>(result.size()),0)<0){cleanup();fail("pak_hash_failed","Windows SHA-1 finalization failed");}cleanup();return result;}

PakArchive write_pak(const PakOptions& options,std::span<const PakFile> files){valid_mount(options.mount_point);count(files.size());PakArchive result;Writer file;
    std::map<std::string,std::map<std::string,std::int32_t>> directories;std::set<std::string> names;directories["/"];
    for(const auto& payload:files){valid_file(payload.relative_path);if(!names.insert(folded(payload.relative_path)).second)fail("duplicate_pak_path","Two Pak filenames resolve to the same case-insensitive path");
        PakEntryLayout value;value.relative_path=payload.relative_path;value.header_offset=extent(file.data.size());value.payload_offset=value.header_offset+entry_size;value.size=extent(payload.bytes.size());value.hash=pak_sha1(payload.bytes);
        entry(file,value,false);file.bytes(payload.bytes);const auto slash=payload.relative_path.find_last_of('/');const auto dir=slash==std::string::npos?std::string("/"):payload.relative_path.substr(0,slash+1);const auto leaf=slash==std::string::npos?payload.relative_path:payload.relative_path.substr(slash+1);
        directories[dir][leaf]=-count(result.entries.size())-1;std::size_t cursor{};while((cursor=dir.find('/',cursor))!=std::string::npos){directories[dir.substr(0,cursor+1)];++cursor;}result.entries.push_back(std::move(value));}
    result.index_offset=extent(file.data.size());Writer directory;directory.scalar(count(directories.size()));for(const auto& [path,entries]:directories){directory.string(path);directory.scalar(count(entries.size()));for(const auto& [name,location]:entries){directory.string(name);directory.scalar(location);}}
    Writer primary;primary.string(options.mount_point);primary.scalar(count(files.size()));primary.scalar(options.path_hash_seed);primary.scalar<std::int32_t>(0);primary.scalar<std::int32_t>(1);
    // Full-directory offset follows the final primary size. Serialize a size-
    // stable placeholder then write the same record with its actual offset.
    const auto offset_position=primary.data.size();primary.scalar<std::int64_t>(0);primary.scalar(extent(directory.data.size()));primary.bytes(pak_sha1(directory.data));primary.scalar<std::int32_t>(0);primary.scalar(count(files.size()));
    for(const auto& value:result.entries)entry(primary,value,true);result.index_size=extent(primary.data.size());result.directory_offset=result.index_offset+result.index_size;result.directory_size=extent(directory.data.size());
    for(unsigned i=0;i<8;++i)primary.data[offset_position+i]=static_cast<std::uint8_t>(static_cast<std::uint64_t>(result.directory_offset)>>(8*i));const auto index_hash=pak_sha1(primary.data);
    file.bytes(primary.data);file.bytes(directory.data);file.bytes(std::array<std::uint8_t,16>{});file.scalar<std::uint8_t>(0);file.scalar(pak_magic);file.scalar<std::int32_t>(11);file.scalar(result.index_offset);file.scalar(result.index_size);file.bytes(index_hash);file.bytes(std::array<std::uint8_t,160>{});result.bytes=std::move(file.data);return result;}

ParsedPak read_pak(std::span<const std::uint8_t> bytes){if(bytes.size()<footer_size)fail("invalid_pak_extent","The Pak is too small for a version-11 footer");ParsedPak result;Reader footer{bytes.subspan(bytes.size()-footer_size)};
    for(auto value:footer.bytes(16))if(value)fail("unsupported_pak_encryption","The Pak footer contains a nonzero encryption GUID");if(footer.scalar<std::uint8_t>()!=0)fail("unsupported_pak_encryption","Encrypted Pak indexes are not supported");
    if(footer.scalar<std::uint32_t>()!=pak_magic||footer.scalar<std::int32_t>()!=11)fail("unsupported_pak_version","The Pak does not contain a version-11 footer");
    auto& layout=result.layout;layout.index_offset=footer.scalar<std::int64_t>();layout.index_size=footer.scalar<std::int64_t>();const auto index_hash=digest(footer);for(auto value:footer.bytes(160))if(value)fail("unsupported_pak_compression","This Pak has compression methods outside the uncompressed writer");
    const auto primary_bytes=section(bytes,layout.index_offset,layout.index_size);if(pak_sha1(primary_bytes)!=index_hash)fail("invalid_pak_index_hash","The Pak primary index SHA-1 does not match its footer");Reader primary{primary_bytes};result.options.mount_point=string(primary);valid_mount(result.options.mount_point);
    const auto entries=primary.elements(entry_size);result.options.path_hash_seed=primary.scalar<std::uint64_t>();if(primary.boolean())fail("unsupported_pak_index","This parser requires the full-directory-only index variant");if(!primary.boolean())fail("invalid_pak_index","The Pak has no full directory index");
    layout.directory_offset=primary.scalar<std::int64_t>();layout.directory_size=primary.scalar<std::int64_t>();const auto directory_hash=digest(primary);if(primary.elements(1)!=0)fail("unsupported_pak_entries","Encoded Pak entries need a different parser");const auto list_count=primary.elements(entry_size);if(entries!=list_count)fail("invalid_pak_count","The Pak entry count differs from its serialized unencoded list");
    for(std::int32_t i=0;i<entries;++i){auto value=entry(primary);for(auto v:value.hash)if(v)fail("invalid_pak_index_entry","Unencoded index entries must omit payload hashes");layout.entries.push_back(value);}if(primary.position!=primary.data.size())fail("invalid_pak_index","Unrecognized bytes remain in the Pak primary index");
    if(layout.directory_offset!=layout.index_offset+layout.index_size||layout.directory_size<0||static_cast<std::uint64_t>(layout.directory_offset)+static_cast<std::uint64_t>(layout.directory_size)!=bytes.size()-footer_size)fail("invalid_pak_extent","The directory index is not between the primary index and footer");
    const auto directory_bytes=section(bytes,layout.directory_offset,layout.directory_size);if(pak_sha1(directory_bytes)!=directory_hash)fail("invalid_pak_directory_hash","The Pak full directory index SHA-1 does not match");Reader directory{directory_bytes};const auto dirs=directory.elements(8);std::set<std::string> paths;std::vector<bool> mapped(entries);
    for(std::int32_t d=0;d<dirs;++d){const auto dir=string(directory);if(dir!="/"&&(dir.empty()||dir.back()!='/'))fail("invalid_pak_directory","A directory key is not in Pak directory format");const auto file_count=directory.elements(8);for(std::int32_t f=0;f<file_count;++f){const auto leaf=string(directory);if(leaf.empty()||leaf.find('/')!=std::string::npos)fail("invalid_pak_path","A directory filename is not a leaf name");const auto location=directory.scalar<std::int32_t>();if(location>=0||location==std::numeric_limits<std::int32_t>::min())fail("unsupported_pak_entries","A directory entry does not reference the unencoded list");const auto index=static_cast<std::size_t>(-static_cast<std::int64_t>(location)-1);if(index>=layout.entries.size()||mapped[index])fail("invalid_pak_entry_location","A Pak directory points outside the list or maps an entry twice");
        auto& value=layout.entries[index];value.relative_path=(dir=="/"?"":dir)+leaf;valid_file(value.relative_path);if(!paths.insert(folded(value.relative_path)).second)fail("duplicate_pak_path","The Pak directory contains a duplicate filename");mapped[index]=true;}}
    if(directory.position!=directory.data.size()||std::find(mapped.begin(),mapped.end(),false)!=mapped.end())fail("invalid_pak_directory","The Pak directory does not map every file exactly once");
    std::int64_t next{};for(auto& value:layout.entries){if(value.header_offset!=next)fail("invalid_pak_entry_extent","File headers do not form a contiguous payload region");Reader header{section(bytes,value.header_offset,entry_size)};const auto actual=entry(header);if(actual.header_offset!=0||actual.size!=value.size)fail("invalid_pak_entry_header","A Pak file header differs from its index entry");value.payload_offset=value.header_offset+entry_size;const auto payload=section(bytes,value.payload_offset,value.size);value.hash=actual.hash;if(pak_sha1(payload)!=value.hash)fail("invalid_pak_payload_hash","A Pak payload SHA-1 does not match its header");result.files.push_back({value.relative_path,{payload.begin(),payload.end()}});if(value.size>std::numeric_limits<std::int64_t>::max()-value.payload_offset)fail("pak_extent_overflow","A Pak payload end exceeds the signed wire extent");next=value.payload_offset+value.size;}
    if(next!=layout.index_offset)fail("invalid_pak_entry_extent","File payloads do not end at the primary index");layout.bytes.assign(bytes.begin(),bytes.end());return result;}
} // namespace sn2_mcp::runtime_package
