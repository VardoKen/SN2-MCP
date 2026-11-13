#pragma once

#include <sn2_mcp/aligned_parameter_buffer.hpp>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>

namespace sn2_mcp::asset_path {

struct Parsed {
    std::string package_name;
    std::string asset_name;
    bool operator==(const Parsed&) const=default;
};

inline bool valid_utf8(std::string_view text) noexcept {
    for(std::size_t index=0;index<text.size();) {
        const auto first=static_cast<unsigned char>(text[index]);
        if(first<0x80) {++index;continue;}
        const std::size_t count=first>=0xc2 && first<=0xdf?2:
            first>=0xe0 && first<=0xef?3:first>=0xf0 && first<=0xf4?4:0;
        if(!count || count>text.size()-index)return false;
        for(std::size_t part=1;part<count;++part) {
            const auto value=static_cast<unsigned char>(text[index+part]);
            if(value<0x80 || value>0xbf)return false;
        }
        const auto second=static_cast<unsigned char>(text[index+1]);
        if((first==0xe0 && second<0xa0) || (first==0xed && second>0x9f)
           || (first==0xf0 && second<0x90) || (first==0xf4 && second>0x8f))return false;
        index+=count;
    }
    return true;
}

inline bool contains_unicode_separator(std::string_view text) noexcept {
    // Called only after UTF-8 validation; preserve ordinary non-ASCII names.
    for(std::size_t index=0;index<text.size();) {
        const auto first=static_cast<unsigned char>(text[index]);
        const std::size_t count=first<0x80?1:first<=0xdf?2:first<=0xef?3:4;
        std::uint32_t point=first&(count==1?0x7f:count==2?0x1f:count==3?0x0f:0x07);
        for(std::size_t part=1;part<count;++part)
            point=(point<<6)|(static_cast<unsigned char>(text[index+part])&0x3f);
        if((point>=0x80 && point<=0xa0) || point==0x1680 || (point>=0x2000 && point<=0x200a)
           || point==0x2028 || point==0x2029 || point==0x202f || point==0x205f || point==0x3000)return true;
        index+=count;
    }
    return false;
}

inline Parsed parse(const std::string& path) {
    if(!valid_utf8(path))
        throw HandlerError("invalid_asset_path","The asset path must contain valid UTF-8");
    if(contains_unicode_separator(path))
        throw HandlerError("invalid_asset_path","The asset path must not contain whitespace or control characters");
    for(const unsigned char value:path) {
        if(value<=0x20 || value==0x7f || std::string_view("\\:*?\"<>|' ,&!~@#;$^").find(value)!=std::string_view::npos)
            throw HandlerError("invalid_asset_path","The asset path contains an invalid character or a subobject delimiter");
    }
    if(path.empty() || path.front()!='/')
        throw HandlerError("invalid_asset_path","A canonical asset path must start with a mount root such as /Game/");
    const auto dot=path.find('.');
    const auto slash=path.rfind('/');
    if(dot==std::string::npos || dot<=slash || dot+1==path.size()
       || path.find('.',dot+1)!=std::string::npos)
        throw HandlerError("invalid_asset_path","A canonical top-level path requires exactly /Mount/Package.Asset");
    const auto package=std::string_view(path).substr(0,dot);
    if(slash==0 || slash+1==dot || package.find("//")!=std::string_view::npos)
        throw HandlerError("invalid_asset_path","The package path requires a mount root and nonempty package components");
    return {std::string(package),path.substr(dot+1)};
}

inline constexpr std::size_t parameter_extent=48;
inline constexpr std::size_t return_offset=40;

// The inspected top-level soft-reference ABI has no owned subpath allocation.
// The engine weak cache, empty UTF-8 subpath header and return slot stay zero.
inline aligned_parameter_buffer::Buffer parameter_frame(std::uint64_t package_name,std::uint64_t asset_name) {
    aligned_parameter_buffer::Buffer result(parameter_extent);
    std::memcpy(result.data()+8,&package_name,sizeof(package_name));
    std::memcpy(result.data()+16,&asset_name,sizeof(asset_name));
    return result;
}

} // namespace sn2_mcp::asset_path
