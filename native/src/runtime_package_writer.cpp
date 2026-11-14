#include "sn2_mcp/runtime_package_writer.hpp"

#include <algorithm>
#include <limits>
#include <type_traits>
#include <utility>

namespace sn2_mcp::runtime_package {
PackageError::PackageError(std::string value,std::string message)
    : std::runtime_error(std::move(message)),code(std::move(value)) {}
namespace {
[[noreturn]] void fail(const char* code,const char* message) {throw PackageError(code,message);}
std::int32_t count(std::size_t value) {
    if(value>static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()))
        fail("package_extent_overflow","A package count or header offset exceeds the signed 32-bit wire field");
    return static_cast<std::int32_t>(value);
}
std::int64_t extent(std::size_t value) {
    if(value>static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max()))
        fail("package_extent_overflow","A serialized export exceeds the signed 64-bit wire field");
    return static_cast<std::int64_t>(value);
}
struct Writer {
    std::vector<std::uint8_t> data;
    void bytes(std::span<const std::uint8_t> value) {data.insert(data.end(),value.begin(),value.end());}
    template<class T> void scalar(T value) {
        using U=std::make_unsigned_t<T>;
        const auto bits=static_cast<U>(value);
        for(std::size_t i=0;i<sizeof(T);++i)data.push_back(static_cast<std::uint8_t>(bits>>(i*8)));
    }
    void boolean(bool value) {scalar<std::int32_t>(value?1:0);}
    void name(NameRef value) {scalar(value.index);scalar(value.number);}
};
struct Reader {
    std::span<const std::uint8_t> data;
    std::size_t position{};
    void require(std::size_t size) const {
        if(position>data.size() || size>data.size()-position)
            fail("invalid_package_extent","A serialized package field exceeds its input boundary");
    }
    void seek(std::int64_t offset) {
        if(offset<0 || static_cast<std::uint64_t>(offset)>data.size())
            fail("invalid_package_extent","A package table offset exceeds the header boundary");
        position=static_cast<std::size_t>(offset);
    }
    template<class T> T scalar() {
        require(sizeof(T));
        using U=std::make_unsigned_t<T>;
        U result{};
        for(std::size_t i=0;i<sizeof(T);++i)result|=static_cast<U>(data[position++])<<(i*8);
        return static_cast<T>(result);
    }
    bool boolean() {
        const auto value=scalar<std::int32_t>();
        if(value!=0 && value!=1)fail("invalid_package_boolean","A package Boolean is not encoded as zero or one");
        return value!=0;
    }
    NameRef name() {return {scalar<std::int32_t>(),scalar<std::int32_t>()};}
    std::int32_t elements(std::size_t minimum_size) {
        const auto value=scalar<std::int32_t>();
        if(value<0 || (minimum_size && static_cast<std::uint64_t>(value)>(data.size()-position)/minimum_size))
            fail("invalid_package_count","A package array count exceeds its available serialized bytes");
        return value;
    }
};
std::vector<std::uint16_t> utf16(const std::string& text) {
    std::vector<std::uint16_t> result;
    for(std::size_t i=0;i<text.size();) {
        const auto lead=static_cast<std::uint8_t>(text[i++]);
        std::uint32_t code{};std::uint32_t minimum{};std::size_t continuation{};
        if(lead<0x80)code=lead;
        else if((lead&0xe0)==0xc0){code=lead&0x1f;minimum=0x80;continuation=1;}
        else if((lead&0xf0)==0xe0){code=lead&0xf;minimum=0x800;continuation=2;}
        else if((lead&0xf8)==0xf0){code=lead&7;minimum=0x10000;continuation=3;}
        else fail("invalid_package_string","A package string is not valid UTF-8");
        if(continuation>text.size()-i)fail("invalid_package_string","A package string ends inside a UTF-8 character");
        for(std::size_t j=0;j<continuation;++j){const auto value=static_cast<std::uint8_t>(text[i++]);
            if((value&0xc0)!=0x80)fail("invalid_package_string","A package string contains an invalid UTF-8 continuation");
            code=(code<<6)|(value&0x3f);}
        if(code<minimum || code>0x10ffff || (code>=0xd800 && code<=0xdfff) || code==0)
            fail("invalid_package_string","A package string contains an invalid character or embedded terminator");
        if(code<0x10000)result.push_back(static_cast<std::uint16_t>(code));
        else {code-=0x10000;result.push_back(static_cast<std::uint16_t>(0xd800+(code>>10)));
            result.push_back(static_cast<std::uint16_t>(0xdc00+(code&0x3ff)));}
    }
    return result;
}
void append_utf8(std::string& text,std::uint32_t code) {
    if(code<0x80)text.push_back(static_cast<char>(code));
    else if(code<0x800){text.push_back(static_cast<char>(0xc0|(code>>6)));text.push_back(static_cast<char>(0x80|(code&0x3f)));}
    else if(code<0x10000){text.push_back(static_cast<char>(0xe0|(code>>12)));text.push_back(static_cast<char>(0x80|((code>>6)&0x3f)));text.push_back(static_cast<char>(0x80|(code&0x3f)));}
    else {text.push_back(static_cast<char>(0xf0|(code>>18)));text.push_back(static_cast<char>(0x80|((code>>12)&0x3f)));
        text.push_back(static_cast<char>(0x80|((code>>6)&0x3f)));text.push_back(static_cast<char>(0x80|(code&0x3f)));}
}
void string(Writer& writer,const std::string& text) {
    if(text.empty()){writer.scalar<std::int32_t>(0);return;}
    const auto units=utf16(text);
    const bool ansi=std::all_of(units.begin(),units.end(),[](auto value){return value<0x80;});
    if(ansi){writer.scalar(count(units.size()+1));for(auto value:units)writer.scalar(static_cast<std::uint8_t>(value));writer.scalar<std::uint8_t>(0);}
    else {writer.scalar(-count(units.size()+1));for(auto value:units)writer.scalar(value);writer.scalar<std::uint16_t>(0);}
}
std::string string(Reader& reader) {
    const auto length=reader.scalar<std::int32_t>();
    if(!length)return {};
    if(length==std::numeric_limits<std::int32_t>::min())fail("invalid_package_string","A serialized string count cannot be negated");
    std::string result;
    if(length>0) {
        reader.require(static_cast<std::size_t>(length));
        for(std::int32_t i=0;i<length;++i){const auto value=reader.scalar<std::uint8_t>();
            if(i==length-1){if(value)fail("invalid_package_string","A serialized ANSI string has no terminator");}
            else {if(!value)fail("invalid_package_string","A serialized string has an embedded terminator");append_utf8(result,value);}}
    } else {
        const auto size=static_cast<std::size_t>(-length);
        reader.require(size*2);
        for(std::size_t i=0;i<size;++i){auto value=reader.scalar<std::uint16_t>();
            if(i==size-1){if(value)fail("invalid_package_string","A serialized UTF-16 string has no terminator");break;}
            if(!value)fail("invalid_package_string","A serialized string has an embedded terminator");
            if(value>=0xd800 && value<=0xdbff){if(i+2>=size)fail("invalid_package_string","A serialized UTF-16 string has an incomplete surrogate");
                const auto low=reader.scalar<std::uint16_t>();++i;if(low<0xdc00 || low>0xdfff)fail("invalid_package_string","A serialized UTF-16 string has an invalid surrogate");
                append_utf8(result,0x10000+((value-0xd800)<<10)+(low-0xdc00));}
            else {if(value>=0xdc00 && value<=0xdfff)fail("invalid_package_string","A serialized UTF-16 string has an unmatched surrogate");append_utf8(result,value);}}
    }
    return result;
}
void utf8_string(Writer& writer,const std::string& text) {
    (void)utf16(text);writer.scalar(count(text.size()));
    writer.bytes({reinterpret_cast<const std::uint8_t*>(text.data()),text.size()});
}
std::string utf8_string(Reader& reader) {
    const auto size=reader.elements(1);reader.require(static_cast<std::size_t>(size));
    std::string text(reinterpret_cast<const char*>(reader.data.data()+reader.position),static_cast<std::size_t>(size));
    reader.position+=static_cast<std::size_t>(size);(void)utf16(text);return text;
}
bool utf8_subpaths(const Package& package) {
    if(package.unversioned_file)return true;
    constexpr std::array<std::uint32_t,4> fortnite_guid={0x601d1886,0xac644f84,0xaa16d3de,0x0deac7d6};
    for(const auto& version:package.custom_versions)if(version.guid==fortnite_guid)return version.version>=192;
    // Ar.CustomVer returns -1 when the version was not harvested. The Engine
    // then uses its legacy wide-string subpath branch.
    return false;
}
void engine(Writer& writer,const EngineVersion& value) {
    writer.scalar(value.major);writer.scalar(value.minor);writer.scalar(value.patch);writer.scalar(value.changelist);string(writer,value.branch);
}
EngineVersion engine(Reader& reader) {
    EngineVersion result;result.major=reader.scalar<std::uint16_t>();result.minor=reader.scalar<std::uint16_t>();
    result.patch=reader.scalar<std::uint16_t>();result.changelist=reader.scalar<std::uint32_t>();result.branch=string(reader);return result;
}
const std::array<std::uint32_t,256>& crc_table(bool deprecated) {
    static const auto normal=[] {std::array<std::uint32_t,256> values{};for(std::uint32_t i=0;i<256;++i){auto v=i;
        for(unsigned bit=0;bit<8;++bit)v=(v&1)?(v>>1)^0xedb88320u:v>>1;values[i]=v;}return values;}();
    static const auto old=[] {std::array<std::uint32_t,256> values{};for(std::uint32_t i=0;i<256;++i){auto v=i<<24;
        for(unsigned bit=0;bit<8;++bit)v=(v&0x80000000u)?(v<<1)^0x04c11db7u:v<<1;values[i]=v;}return values;}();
    return deprecated?old:normal;
}
void valid_name(const Package& package,NameRef value) {
    if(value.index<0 || static_cast<std::size_t>(value.index)>=package.names.size() || value.number<0)
        fail("invalid_package_name_reference","A serialized FName is not in the harvested package name table");
}
void valid_object(const Package& package,std::int32_t value,bool allow_null=true) {
    if((!value && !allow_null) || (value>0 && static_cast<std::uint64_t>(value)>package.exports.size())
        || (value<0 && static_cast<std::uint64_t>(-static_cast<std::int64_t>(value))>package.imports.size()))
        fail("invalid_package_object_reference","A serialized object is not in the harvested import/export tables");
}
std::array<const std::vector<std::int32_t>*,4> groups(const Dependencies& value) {
    return {&value.serialization_before_serialization,&value.create_before_serialization,
        &value.serialization_before_create,&value.create_before_create};
}
std::array<std::vector<std::int32_t>*,4> groups(Dependencies& value) {
    return {&value.serialization_before_serialization,&value.create_before_serialization,
        &value.serialization_before_create,&value.create_before_create};
}
struct ExportWire {std::int64_t size{},offset{};std::int32_t first{-1};std::array<std::int32_t,4> counts{};};
void validate(const Package& package) {
    if(package.name.empty() || package.name.front()!='/')fail("invalid_package_name","A package needs an absolute Unreal package name");
    (void)utf16(package.name);
    if((package.flags&(cooked_flag|filter_editor_only_flag))!=(cooked_flag|filter_editor_only_flag))
        fail("unsupported_package_flags","This writer requires cooked, editor-filtered serialization");
    if(package.ue4_version!=current_ue4_version || package.ue5_version!=current_ue5_version)
        fail("unsupported_package_schema","The requested file version requires a different summary/export serializer schema");
    if(package.unversioned_file && !package.custom_versions.empty())
        fail("invalid_package_versions","An unversioned package must use an empty custom-version container");
    count(package.names.size());count(package.imports.size());count(package.exports.size());
    const auto referenced=package.names_referenced_from_exports;
    if(referenced < -1 || (referenced>=0 && static_cast<std::size_t>(referenced)>package.names.size()))
        fail("invalid_package_name_count","The initial export-name range exceeds the harvested name table");
    for(const auto& name:package.names){if(name.text.empty())fail("invalid_package_name","A name table entry must contain a name");
        if(utf16(name.text).size()>=1024)fail("package_name_extent","A serialized Unreal name exceeds the target's NAME_SIZE buffer extent");}
    for(const auto& path:package.soft_object_paths){valid_name(package,path.package);valid_name(package,path.asset);(void)utf16(path.subpath);}
    for(const auto& import:package.imports){valid_name(package,import.class_package);valid_name(package,import.class_name);
        valid_name(package,import.name);valid_object(package,import.outer);}
    for(const auto& object:package.exports){valid_name(package,object.name);valid_object(package,object.class_index,false);
        valid_object(package,object.super_index);valid_object(package,object.template_index);valid_object(package,object.outer_index);
        for(const auto* group:groups(object.dependencies))for(auto index:*group)valid_object(package,index,false);
        std::vector<std::pair<std::size_t,std::size_t>> ranges;
        for(const auto& reference:object.data.references){
            if(reference.offset>object.data.bytes.size() || reference.size>object.data.bytes.size()-reference.offset)
                fail("invalid_package_reference_write","A serializer reference write exceeds its export payload");
            Writer expected;
            if(reference.kind==ReferenceKind::name){valid_name(package,reference.name);expected.name(reference.name);}
            else if(reference.kind==ReferenceKind::object){valid_object(package,reference.object);expected.scalar(reference.object);}
            else {valid_object(package,reference.object);if(!reference.object && !reference.path.empty())
                    fail("invalid_package_field_path","A persistent field path needs its owning struct");
                expected.scalar(count(reference.path.size()));for(auto name:reference.path){valid_name(package,name);expected.name(name);}expected.scalar(reference.object);}
            if(reference.size!=expected.data.size() || !std::equal(expected.data.begin(),expected.data.end(),object.data.bytes.begin()+reference.offset))
                fail("invalid_package_reference_write","A recorded serializer reference differs from its written package-local bytes");
            ranges.emplace_back(reference.offset,reference.offset+reference.size);
        }
        std::sort(ranges.begin(),ranges.end());for(std::size_t i=1;i<ranges.size();++i)
            if(ranges[i].first<ranges[i-1].second)fail("overlapping_package_reference_write","Serializer reference writes overlap");
    }
}
void summary(Writer& writer,const Package& package,const Layout& layout) {
    writer.scalar(file_tag);writer.scalar<std::int32_t>(-9);
    writer.scalar<std::int32_t>(package.unversioned_file?0:864);
    writer.scalar<std::int32_t>(package.unversioned_file?0:package.ue4_version);
    writer.scalar<std::int32_t>(package.unversioned_file?0:package.ue5_version);
    writer.scalar<std::int32_t>(package.unversioned_file?0:package.licensee_version);
    writer.bytes(package.saved_hash);writer.scalar(layout.header_size);writer.scalar(count(package.custom_versions.size()));
    for(const auto& version:package.custom_versions){for(auto word:version.guid)writer.scalar(word);writer.scalar(version.version);}
    string(writer,package.name);writer.scalar(package.flags);
    writer.scalar(count(package.names.size()));writer.scalar(layout.name_offset);
    writer.scalar(count(package.soft_object_paths.size()));writer.scalar(layout.soft_object_path_offset);
    writer.scalar<std::int32_t>(0);writer.scalar<std::int32_t>(0); // Gatherable text.
    writer.scalar(count(package.exports.size()));writer.scalar(layout.export_offset);
    writer.scalar(count(package.imports.size()));writer.scalar(layout.import_offset);
    writer.scalar<std::int32_t>(0);writer.scalar(layout.depends_offset); // Cell exports.
    writer.scalar<std::int32_t>(0);writer.scalar(layout.depends_offset); // Cell imports.
    writer.scalar<std::int32_t>(0);writer.scalar(layout.depends_offset); // Metadata, DependsMap.
    writer.scalar<std::int32_t>(0);writer.scalar<std::int32_t>(0); // Soft package refs.
    writer.scalar<std::int32_t>(0);writer.scalar<std::int32_t>(0); // Searchable names, thumbnails.
    writer.scalar<std::int32_t>(1);writer.scalar(count(package.exports.size()));writer.scalar(count(package.names.size()));
    engine(writer,package.saved_engine);engine(writer,package.compatible_engine);
    writer.scalar<std::int32_t>(0);writer.scalar<std::int32_t>(0); // Package compression/chunks.
    writer.scalar(package.package_source);writer.scalar<std::int32_t>(0); // Additional packages.
    writer.scalar(layout.asset_registry_offset);writer.scalar(layout.bulk_data_offset);writer.scalar<std::int32_t>(0);
    writer.scalar(count(package.chunk_ids.size()));for(auto value:package.chunk_ids)writer.scalar(value);
    writer.scalar(layout.preload_count);writer.scalar(layout.preload_offset);
    writer.scalar(package.names_referenced_from_exports<0?count(package.names.size()):package.names_referenced_from_exports);
    writer.scalar<std::int64_t>(-1);writer.scalar<std::int32_t>(-1); // Payload TOC, data resources.
}
void export_entry(Writer& writer,const Package& package,const Export& object,const ExportWire& wire) {
    writer.scalar(object.class_index);writer.scalar(object.super_index);writer.scalar(object.template_index);writer.scalar(object.outer_index);
    writer.name(object.name);writer.scalar(object.object_flags);writer.scalar(wire.size);writer.scalar(wire.offset);
    writer.boolean(object.forced);writer.boolean(object.not_for_client);writer.boolean(object.not_for_server);
    writer.boolean(object.inherited_instance);writer.scalar(object.package_flags);writer.boolean(object.not_always_loaded_for_editor_game);
    writer.boolean(object.is_asset);writer.boolean(object.generate_public_hash);writer.scalar(wire.first);
    for(auto value:wire.counts)writer.scalar(value);
    if(!(package.flags&unversioned_properties_flag)){writer.scalar(object.script_serialization_start);writer.scalar(object.script_serialization_end);}
}
void zero(std::int32_t value,const char* what) {if(value)fail("unsupported_package_section",what);}
} // namespace

NameEntry make_name(const std::string& text) {
    if(text.empty())fail("invalid_package_name","A name entry cannot be empty");
    const auto units=utf16(text);if(units.size()>=1024)fail("package_name_extent","A serialized Unreal name exceeds the target's NAME_SIZE buffer extent");
    const bool ansi=std::all_of(units.begin(),units.end(),[](auto c){return c<0x80;});
    std::uint32_t folded{},exact=0xffffffffu;
    for(auto unit:units){const auto upper=static_cast<std::uint16_t>(unit-((unit>='a' && unit<='z')?32:0));
        for(unsigned b=0;b<(ansi?1u:2u);++b)folded=((folded>>8)&0x00ffffffu)^crc_table(true)[(folded^(upper>>(b*8)))&0xff];
        for(unsigned b=0;b<4;++b)exact=(exact>>8)^crc_table(false)[(exact^((b<2)?(unit>>(b*8)):0))&0xff];}
    return {text,static_cast<std::uint16_t>(folded),static_cast<std::uint16_t>(~exact)};
}
std::vector<std::uint16_t> string_utf16(const std::string& text){return utf16(text);}
std::vector<std::uint8_t> encode_string(const std::string& text){Writer writer;string(writer,text);return std::move(writer.data);}
void ExportSink::bytes(std::span<const std::uint8_t> value) {
    if(value.size()>std::numeric_limits<std::size_t>::max()-position_)
        fail("package_extent_overflow","A serializer write exceeds the addressable byte extent");
    const auto end=position_+value.size();
    for(const auto& reference:data_.references)
        if(position_<reference.offset+reference.size && end>reference.offset
           && !(position_<=reference.offset && end>=reference.offset+reference.size))
            fail("partial_package_reference_overwrite","A serializer overwrite splits a recorded package reference");
    std::erase_if(data_.references,[&](const auto& reference){return position_<reference.offset+reference.size && end>reference.offset;});
    if(end>data_.bytes.size())data_.bytes.resize(end);
    std::copy(value.begin(),value.end(),data_.bytes.begin()+position_);position_=end;
}
void ExportSink::u8(std::uint8_t value){bytes(std::span<const std::uint8_t>(&value,1));}
void ExportSink::u16(std::uint16_t value){Writer w;w.scalar(value);bytes(w.data);}
void ExportSink::u32(std::uint32_t value){Writer w;w.scalar(value);bytes(w.data);}
void ExportSink::i32(std::int32_t value){u32(static_cast<std::uint32_t>(value));}
void ExportSink::u64(std::uint64_t value){Writer w;w.scalar(value);bytes(w.data);}
void ExportSink::i64(std::int64_t value){u64(static_cast<std::uint64_t>(value));}
void ExportSink::string(const std::string& text){Writer w;runtime_package::string(w,text);bytes(w.data);}
void ExportSink::name(NameRef value){const auto start=tell();Writer written;written.name(value);bytes(written.data);
    data_.references.push_back({start,8,ReferenceKind::name,value,0,{}});}
void ExportSink::object(std::int32_t value){const auto start=tell();i32(value);data_.references.push_back({start,4,ReferenceKind::object,{},value,{}});}
void ExportSink::field_path(std::span<const NameRef> path,std::int32_t owner){const auto start=tell();Writer written;written.scalar(count(path.size()));
    for(auto name:path)written.name(name);written.scalar(owner);bytes(written.data);
    data_.references.push_back({start,tell()-start,ReferenceKind::field_path,{},owner,{path.begin(),path.end()}});}
std::size_t ExportSink::tell() const noexcept{return position_;}
std::size_t ExportSink::size() const noexcept{return data_.bytes.size();}
void ExportSink::seek(std::size_t position){if(position>data_.bytes.size())fail("invalid_package_seek","A serializer seek exceeds its written byte extent");position_=position;}
void ExportSink::patch_i32(std::size_t offset,std::int32_t value){if(offset>data_.bytes.size() || 4>data_.bytes.size()-offset)
        fail("invalid_package_patch","A serializer patch exceeds its export payload");
    const auto position=position_;seek(offset);i32(value);seek(position);}
ExportData ExportSink::take(){position_=0;return std::exchange(data_,{});}

CookedPackage write(const Package& package) {
    validate(package);CookedPackage result;auto& layout=result.layout;
    Writer header;summary(header,package,layout);layout.summary_size=count(header.data.size());layout.name_offset=layout.summary_size;
    for(const auto& name:package.names){string(header,name.text);header.scalar(name.non_case_hash);header.scalar(name.case_hash);}
    layout.soft_object_path_offset=count(header.data.size());
    for(const auto& path:package.soft_object_paths){header.name(path.package);header.name(path.asset);
        if(utf8_subpaths(package))utf8_string(header,path.subpath);else string(header,path.subpath);}
    layout.import_offset=count(header.data.size());
    for(const auto& import:package.imports){header.name(import.class_package);header.name(import.class_name);header.scalar(import.outer);header.name(import.name);header.boolean(import.optional);}
    layout.export_offset=count(header.data.size());
    std::vector<ExportWire> entries(package.exports.size());Writer preloads;
    std::int64_t payload_size{};
    for(std::size_t i=0;i<package.exports.size();++i){auto& wire=entries[i];const auto& object=package.exports[i];
        wire.size=extent(object.data.bytes.size());wire.offset=payload_size;
        if(wire.size>std::numeric_limits<std::int64_t>::max()-payload_size)fail("package_extent_overflow","The combined export extent exceeds its wire field");
        payload_size+=wire.size;std::size_t group_index{};
        for(const auto* group:groups(object.dependencies)){wire.counts[group_index++]=count(group->size());
            if(!group->empty() && wire.first<0)wire.first=count(preloads.data.size()/4);
            for(auto dependency:*group)preloads.scalar(dependency);}
        export_entry(header,package,object,wire);
    }
    layout.depends_offset=count(header.data.size());
    for(std::size_t i=0;i<package.exports.size();++i)header.scalar<std::int32_t>(0);
    layout.asset_registry_offset=count(header.data.size());header.scalar<std::int32_t>(0);
    layout.preload_offset=count(header.data.size());layout.preload_count=count(preloads.data.size()/4);header.bytes(preloads.data);
    layout.header_size=count(header.data.size());
    if(payload_size>std::numeric_limits<std::int64_t>::max()-layout.header_size)fail("package_extent_overflow","Header plus exports exceed the wire extent");
    layout.bulk_data_offset=layout.header_size+payload_size;
    Writer patched_summary;summary(patched_summary,package,layout);
    if(patched_summary.data.size()!=static_cast<std::size_t>(layout.summary_size))fail("invalid_package_layout","Fixing summary offsets changed its serialized size");
    std::copy(patched_summary.data.begin(),patched_summary.data.end(),header.data.begin());
    Writer patched_exports;
    for(std::size_t i=0;i<entries.size();++i){entries[i].offset+=layout.header_size;
        layout.export_offsets.push_back(entries[i].offset);layout.export_sizes.push_back(entries[i].size);
        export_entry(patched_exports,package,package.exports[i],entries[i]);}
    std::copy(patched_exports.data.begin(),patched_exports.data.end(),header.data.begin()+layout.export_offset);
    result.uasset=std::move(header.data);Writer payload;
    for(const auto& object:package.exports)payload.bytes(object.data.bytes);
    payload.scalar(file_tag);result.uexp=std::move(payload.data);return result;
}

ParsedPackage read(std::span<const std::uint8_t> uasset,std::span<const std::uint8_t> uexp) {
    Reader reader{uasset};ParsedPackage result;auto& package=result.package;auto& layout=result.layout;
    if(reader.scalar<std::uint32_t>()!=file_tag || reader.scalar<std::int32_t>()!=-9)
        fail("unsupported_package_schema","The input is not a current little-endian legacy cooked package");
    const auto ue3=reader.scalar<std::int32_t>();const auto ue4=reader.scalar<std::int32_t>();
    const auto ue5=reader.scalar<std::int32_t>();const auto licensee=reader.scalar<std::int32_t>();
    package.unversioned_file=!ue4&&!ue5&&!licensee;package.licensee_version=licensee;
    if(!package.unversioned_file){package.ue4_version=ue4;package.ue5_version=ue5;
        if(ue3!=864 || ue4!=current_ue4_version || ue5!=current_ue5_version)fail("unsupported_package_schema","The input requires a different versioned summary/export schema");}
    for(auto& byte:package.saved_hash)byte=reader.scalar<std::uint8_t>();layout.header_size=reader.scalar<std::int32_t>();
    const auto custom_count=reader.elements(20);for(std::int32_t i=0;i<custom_count;++i){CustomVersion v;for(auto& word:v.guid)word=reader.scalar<std::uint32_t>();v.version=reader.scalar<std::int32_t>();package.custom_versions.push_back(v);}
    package.name=string(reader);package.flags=reader.scalar<std::uint32_t>();
    const auto name_count=reader.scalar<std::int32_t>();layout.name_offset=reader.scalar<std::int32_t>();
    const auto soft_count=reader.scalar<std::int32_t>();layout.soft_object_path_offset=reader.scalar<std::int32_t>();
    zero(reader.scalar<std::int32_t>(),"Gatherable text needs an additional package serializer");zero(reader.scalar<std::int32_t>(),"Gatherable text has a nonzero offset");
    const auto export_count=reader.scalar<std::int32_t>();layout.export_offset=reader.scalar<std::int32_t>();
    const auto import_count=reader.scalar<std::int32_t>();layout.import_offset=reader.scalar<std::int32_t>();
    zero(reader.scalar<std::int32_t>(),"Verse cell exports are outside this UObject writer");const auto cell_exports_offset=reader.scalar<std::int32_t>();
    zero(reader.scalar<std::int32_t>(),"Verse cell imports are outside this UObject writer");const auto cell_imports_offset=reader.scalar<std::int32_t>();
    zero(reader.scalar<std::int32_t>(),"Metadata needs an additional package serializer");layout.depends_offset=reader.scalar<std::int32_t>();
    zero(reader.scalar<std::int32_t>(),"Soft package references need an additional package serializer");zero(reader.scalar<std::int32_t>(),"Soft package references have a nonzero offset");
    zero(reader.scalar<std::int32_t>(),"Searchable names need an additional package serializer");zero(reader.scalar<std::int32_t>(),"Thumbnails need an additional package serializer");
    const auto generations=reader.elements(8);for(std::int32_t i=0;i<generations;++i){const auto exports=reader.scalar<std::int32_t>();const auto names=reader.scalar<std::int32_t>();
        if(exports!=export_count || names!=name_count)fail("unsupported_package_generations","Historic generation maps are not represented by this writer");}
    if(generations!=1)fail("unsupported_package_generations","The current package needs one generation map");
    package.saved_engine=engine(reader);package.compatible_engine=engine(reader);
    zero(reader.scalar<std::int32_t>(),"Package-level compression is not supported");zero(reader.scalar<std::int32_t>(),"Package-level compressed chunks are not supported");
    package.package_source=reader.scalar<std::uint32_t>();zero(reader.scalar<std::int32_t>(),"Additional packages need an additional package serializer");
    layout.asset_registry_offset=reader.scalar<std::int32_t>();layout.bulk_data_offset=reader.scalar<std::int64_t>();
    zero(reader.scalar<std::int32_t>(),"World tile information needs an additional serializer");
    const auto chunks=reader.elements(4);for(std::int32_t i=0;i<chunks;++i)package.chunk_ids.push_back(reader.scalar<std::int32_t>());
    layout.preload_count=reader.scalar<std::int32_t>();layout.preload_offset=reader.scalar<std::int32_t>();
    package.names_referenced_from_exports=reader.scalar<std::int32_t>();
    if(reader.scalar<std::int64_t>()!=-1 || reader.scalar<std::int32_t>()!=-1)
        fail("unsupported_package_bulk_data","Payload TOC/data resources require their own serializer");
    layout.summary_size=count(reader.position);
    if(layout.header_size<0 || static_cast<std::size_t>(layout.header_size)!=uasset.size() || name_count<0 || import_count<0 || export_count<0 || soft_count<0 || layout.preload_count<0)
        fail("invalid_package_extent","Package header size or table counts do not match the input");
    if(cell_exports_offset!=layout.depends_offset || cell_imports_offset!=layout.depends_offset)
        fail("unsupported_package_section","Empty cell maps must point at the following DependsMap");
    reader.seek(layout.name_offset);if(static_cast<std::uint64_t>(name_count)>(uasset.size()-reader.position)/9)fail("invalid_package_count","Name entries exceed their header extent");
    for(std::int32_t i=0;i<name_count;++i){NameEntry name;name.text=string(reader);name.non_case_hash=reader.scalar<std::uint16_t>();name.case_hash=reader.scalar<std::uint16_t>();package.names.push_back(std::move(name));}
    if(reader.position!=static_cast<std::size_t>(layout.soft_object_path_offset))fail("invalid_package_layout","Names do not end at the soft-path table");
    if(static_cast<std::uint64_t>(soft_count)>(uasset.size()-reader.position)/20)fail("invalid_package_count","Soft paths exceed their header extent");
    for(std::int32_t i=0;i<soft_count;++i){SoftObjectPath p;p.package=reader.name();p.asset=reader.name();
        p.subpath=utf8_subpaths(package)?utf8_string(reader):string(reader);package.soft_object_paths.push_back(std::move(p));}
    if(reader.position!=static_cast<std::size_t>(layout.import_offset))fail("invalid_package_layout","Soft paths do not end at the import table");
    if(static_cast<std::uint64_t>(import_count)>(uasset.size()-reader.position)/32)fail("invalid_package_count","Imports exceed their header extent");
    for(std::int32_t i=0;i<import_count;++i){Import p;p.class_package=reader.name();p.class_name=reader.name();p.outer=reader.scalar<std::int32_t>();p.name=reader.name();p.optional=reader.boolean();package.imports.push_back(p);}
    if(reader.position!=static_cast<std::size_t>(layout.export_offset))fail("invalid_package_layout","Imports do not end at the export table");
    const auto export_size=(package.flags&unversioned_properties_flag)?96:112;
    if(static_cast<std::uint64_t>(export_count)>(uasset.size()-reader.position)/export_size)fail("invalid_package_count","Exports exceed their header extent");
    std::vector<ExportWire> entries;
    for(std::int32_t i=0;i<export_count;++i){Export p;ExportWire w;p.class_index=reader.scalar<std::int32_t>();p.super_index=reader.scalar<std::int32_t>();
        p.template_index=reader.scalar<std::int32_t>();p.outer_index=reader.scalar<std::int32_t>();p.name=reader.name();p.object_flags=reader.scalar<std::uint32_t>();
        w.size=reader.scalar<std::int64_t>();w.offset=reader.scalar<std::int64_t>();p.forced=reader.boolean();p.not_for_client=reader.boolean();p.not_for_server=reader.boolean();
        p.inherited_instance=reader.boolean();p.package_flags=reader.scalar<std::uint32_t>();p.not_always_loaded_for_editor_game=reader.boolean();p.is_asset=reader.boolean();
        p.generate_public_hash=reader.boolean();w.first=reader.scalar<std::int32_t>();for(auto& value:w.counts)value=reader.scalar<std::int32_t>();
        if(!(package.flags&unversioned_properties_flag)){p.script_serialization_start=reader.scalar<std::int64_t>();p.script_serialization_end=reader.scalar<std::int64_t>();}
        entries.push_back(w);package.exports.push_back(std::move(p));}
    if(reader.position!=static_cast<std::size_t>(layout.depends_offset))fail("invalid_package_layout","Exports do not end at the DependsMap");
    for(std::int32_t i=0;i<export_count;++i)zero(reader.scalar<std::int32_t>(),"Cooked DependsMap entries must be empty; dependencies belong in preload data");
    if(reader.position!=static_cast<std::size_t>(layout.asset_registry_offset))fail("invalid_package_layout","DependsMap does not end at the asset registry");
    zero(reader.scalar<std::int32_t>(),"Cooked asset registry tags are outside the no-editor package format");
    if(reader.position!=static_cast<std::size_t>(layout.preload_offset))fail("invalid_package_layout","Asset registry does not end at the preload table");
    if(static_cast<std::uint64_t>(layout.preload_count)>(uasset.size()-reader.position)/4)fail("invalid_package_count","Preload dependencies exceed the header extent");
    std::vector<std::int32_t> preloads;for(std::int32_t i=0;i<layout.preload_count;++i)preloads.push_back(reader.scalar<std::int32_t>());
    if(reader.position!=uasset.size())fail("invalid_package_layout","Bytes remain after the supported package header");
    std::int64_t next=layout.header_size;std::size_t next_dependency{};
    for(std::size_t i=0;i<entries.size();++i){const auto& entry=entries[i];auto& p=package.exports[i];
        if(entry.offset!=next || entry.size<0 || entry.size>std::numeric_limits<std::int64_t>::max()-next)
            fail("invalid_package_export_extent","Export bytes are not a contiguous bounded payload");
        const auto start=static_cast<std::uint64_t>(entry.offset-layout.header_size);const auto size=static_cast<std::uint64_t>(entry.size);
        if(start>uexp.size() || size>uexp.size()-start)fail("invalid_package_export_extent","An export extends beyond its .uexp payload");
        p.data.bytes.assign(uexp.begin()+static_cast<std::size_t>(start),uexp.begin()+static_cast<std::size_t>(start+size));
        next+=entry.size;layout.export_offsets.push_back(entry.offset);layout.export_sizes.push_back(entry.size);
        std::size_t needed{};for(auto value:entry.counts){if(value<0)fail("invalid_package_dependencies","A dependency count is negative");needed+=static_cast<std::size_t>(value);}
        if((!needed && entry.first!=-1) || (needed && (entry.first<0 || static_cast<std::size_t>(entry.first)!=next_dependency))
            || needed>preloads.size()-next_dependency)fail("invalid_package_dependencies","An export dependency range is not a contiguous valid preload range");
        std::size_t group_index{};for(auto* group:groups(p.dependencies)){const auto size_group=static_cast<std::size_t>(entry.counts[group_index++]);
            group->assign(preloads.begin()+next_dependency,preloads.begin()+next_dependency+size_group);next_dependency+=size_group;}
    }
    if(next_dependency!=preloads.size() || next!=layout.bulk_data_offset || next-layout.header_size!=extent(uexp.size())-4)
        fail("invalid_package_layout","Preload or export bytes do not end at their recorded boundary");
    Reader trailer{uexp};trailer.seek(extent(uexp.size())-4);if(trailer.scalar<std::uint32_t>()!=file_tag)
        fail("invalid_package_trailer","The .uexp file does not end with the package file tag");
    validate(package);return result;
}
} // namespace sn2_mcp::runtime_package
