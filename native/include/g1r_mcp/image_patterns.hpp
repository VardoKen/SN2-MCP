#pragma once
#include <g1r_mcp/game_memory.hpp>
#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace g1r_mcp::image_patterns {

class Image {
    struct Section {std::uintptr_t rva{};std::size_t bytes{};bool executable{};};
    std::uintptr_t base_{};
    std::size_t size_{};
    std::vector<Section> sections_;

    static bool readable(DWORD protection) noexcept {
        if(protection&(PAGE_GUARD|PAGE_NOACCESS))return false;
        switch(protection&0xff) {
            case PAGE_READONLY:case PAGE_READWRITE:case PAGE_WRITECOPY:
            case PAGE_EXECUTE_READ:case PAGE_EXECUTE_READWRITE:case PAGE_EXECUTE_WRITECOPY:return true;
            default:return false;
        }
    }
    static bool readable_range(std::uintptr_t address,std::size_t bytes) noexcept {
        if(address>std::numeric_limits<std::uintptr_t>::max()-bytes)return false;
        const auto end=address+bytes;
        while(address<end) {
            MEMORY_BASIC_INFORMATION region{};
            if(!VirtualQuery(reinterpret_cast<const void*>(address),&region,sizeof(region))
               || region.State!=MEM_COMMIT || !readable(region.Protect))return false;
            const auto start=reinterpret_cast<std::uintptr_t>(region.BaseAddress);
            if(start>address || region.RegionSize>std::numeric_limits<std::uintptr_t>::max()-start)return false;
            const auto region_end=start+region.RegionSize;
            if(region_end<=address)return false;
            address=std::min(end,region_end);
        }
        return true;
    }
    template<class Value>Value header(std::uintptr_t address) const {
        if(!readable_range(address,sizeof(Value)))
            throw HandlerError("image_layout_invalid","PE header memory is not readable");
        return game_memory::read<Value>(address);
    }
    static int hex(char value) noexcept {
        if(value>='0' && value<='9')return value-'0';
        if(value>='a' && value<='f')return value-'a'+10;
        if(value>='A' && value<='F')return value-'A'+10;
        return -1;
    }
    static void parse(std::string_view pattern,std::vector<std::uint8_t>& bytes,std::string& mask) {
        while(!pattern.empty()) {
            const auto begin=pattern.find_first_not_of(" \t\r\n");
            if(begin==std::string_view::npos)break;
            pattern.remove_prefix(begin);
            const auto end=pattern.find_first_of(" \t\r\n");
            const auto token=pattern.substr(0,end);
            if(token=="?" || token=="??") {bytes.push_back(0);mask.push_back('?');}
            else if(token.size()==2 && hex(token[0])>=0 && hex(token[1])>=0) {
                bytes.push_back(static_cast<std::uint8_t>(hex(token[0])*16+hex(token[1])));mask.push_back('x');
            } else throw HandlerError("image_pattern_invalid","A signature requires hexadecimal bytes or ??");
            if(end==std::string_view::npos)break;
            pattern.remove_prefix(end);
        }
    }

public:
    explicit Image(std::uintptr_t base):base_(base) {
        if(!base_)throw HandlerError("image_layout_invalid","A mapped PE image is required");
        MEMORY_BASIC_INFORMATION allocation{};
        if(!VirtualQuery(reinterpret_cast<const void*>(base_),&allocation,sizeof(allocation))
           || allocation.Type!=MEM_IMAGE || allocation.AllocationBase!=reinterpret_cast<void*>(base_))
            throw HandlerError("image_layout_invalid","The PE base must identify a mapped image allocation");
        const auto dos=header<IMAGE_DOS_HEADER>(base_);
        if(dos.e_magic!=IMAGE_DOS_SIGNATURE || dos.e_lfanew<static_cast<LONG>(sizeof(dos))
           || static_cast<std::uintptr_t>(dos.e_lfanew)>std::numeric_limits<std::uintptr_t>::max()-base_)
            throw HandlerError("image_layout_invalid","Invalid mapped DOS header");
        const auto nt_address=base_+static_cast<std::uintptr_t>(dos.e_lfanew);
        const auto nt=header<IMAGE_NT_HEADERS64>(nt_address);
        if(nt.Signature!=IMAGE_NT_SIGNATURE || nt.FileHeader.Machine!=IMAGE_FILE_MACHINE_AMD64
           || nt.OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR64_MAGIC
           || nt.FileHeader.SizeOfOptionalHeader<sizeof(IMAGE_OPTIONAL_HEADER64)
           || !nt.FileHeader.NumberOfSections || !nt.OptionalHeader.SizeOfImage
           || nt.OptionalHeader.SizeOfHeaders>nt.OptionalHeader.SizeOfImage)
            throw HandlerError("image_layout_invalid","A valid mapped AMD64 PE image is required");
        size_=nt.OptionalHeader.SizeOfImage;
        if(base_>std::numeric_limits<std::uintptr_t>::max()-size_)
            throw HandlerError("image_layout_invalid","The mapped image address range overflows");
        if(static_cast<std::size_t>(dos.e_lfanew)>size_ || sizeof(nt)>size_-static_cast<std::size_t>(dos.e_lfanew))
            throw HandlerError("image_layout_invalid","PE NT headers exceed the mapped image");
        const auto section_rva=static_cast<std::size_t>(dos.e_lfanew)+sizeof(DWORD)+sizeof(IMAGE_FILE_HEADER)
            +nt.FileHeader.SizeOfOptionalHeader;
        const auto section_bytes=static_cast<std::size_t>(nt.FileHeader.NumberOfSections)*sizeof(IMAGE_SECTION_HEADER);
        if(section_rva>nt.OptionalHeader.SizeOfHeaders || section_bytes>nt.OptionalHeader.SizeOfHeaders-section_rva)
            throw HandlerError("image_layout_invalid","PE section headers exceed the image headers");
        for(std::size_t index=0;index<nt.FileHeader.NumberOfSections;++index) {
            const auto section=header<IMAGE_SECTION_HEADER>(base_+section_rva+index*sizeof(IMAGE_SECTION_HEADER));
            const auto bytes=static_cast<std::size_t>(std::max(section.Misc.VirtualSize,section.SizeOfRawData));
            if(!contains(section.VirtualAddress,bytes))
                throw HandlerError("image_layout_invalid","A PE section exceeds the mapped image");
            if(bytes && (section.Characteristics&IMAGE_SCN_MEM_READ))
                sections_.push_back({section.VirtualAddress,bytes,(section.Characteristics&IMAGE_SCN_MEM_EXECUTE)!=0});
        }
    }
    std::size_t size() const noexcept {return size_;}
    bool contains(std::uintptr_t rva,std::size_t bytes=1) const noexcept {
        return rva<=size_ && bytes<=size_-rva;
    }
    std::vector<std::uintptr_t> scan(std::span<const std::uint8_t> pattern,std::string_view mask,
                                    bool executable_only=true) const {
        if(pattern.empty() || mask.size()!=pattern.size()
           || mask.find_first_not_of("x?")!=std::string_view::npos)
            throw HandlerError("image_pattern_invalid","A signature requires matching bytes and x/? mask");
        std::size_t anchor{},anchor_size{},run{};
        for(std::size_t index=0;index<mask.size();++index) {
            run=mask[index]=='x'?run+1:0;
            if(run>anchor_size) {anchor_size=run;anchor=index+1-run;}
        }
        if(!anchor_size)throw HandlerError("image_pattern_invalid","A signature requires at least one exact byte");
        std::vector<std::uintptr_t> matches;
        const auto search=[&](std::uintptr_t buffer_rva,const std::vector<std::uint8_t>& buffer) {
            if(buffer.size()<pattern.size())return;
            auto position=buffer.begin();
            while(position!=buffer.end()) {
                position=std::search(position,buffer.end(),pattern.begin()+anchor,pattern.begin()+anchor+anchor_size);
                if(position==buffer.end())break;
                const auto anchor_position=static_cast<std::size_t>(position-buffer.begin());
                if(anchor_position>=anchor) {
                    const auto start=anchor_position-anchor;
                    if(pattern.size()<=buffer.size()-start) {
                        bool equal=true;
                        for(std::size_t index=0;index<pattern.size();++index)
                            if(mask[index]=='x' && buffer[start+index]!=pattern[index]) {equal=false;break;}
                        if(equal)matches.push_back(buffer_rva+start);
                    }
                }
                ++position;
            }
        };
        for(const auto& section:sections_) {
            if(executable_only && !section.executable)continue;
            auto address=base_+section.rva;
            const auto end=address+section.bytes;
            std::vector<std::uint8_t> buffer;
            auto buffer_rva=section.rva;
            while(address<end) {
                MEMORY_BASIC_INFORMATION region{};
                if(!VirtualQuery(reinterpret_cast<const void*>(address),&region,sizeof(region)))
                    throw HandlerError("image_scan_failed","Could not inspect a mapped image region");
                const auto start=reinterpret_cast<std::uintptr_t>(region.BaseAddress);
                if(start>address || region.RegionSize>std::numeric_limits<std::uintptr_t>::max()-start
                   || start+region.RegionSize<=address)
                    throw HandlerError("image_scan_failed","Invalid mapped image region bounds");
                const auto next=std::min(end,start+region.RegionSize);
                if(region.State==MEM_COMMIT && region.Type==MEM_IMAGE
                   && region.AllocationBase==reinterpret_cast<void*>(base_) && readable(region.Protect)) {
                    if(buffer.empty())buffer_rva=address-base_;
                    const auto previous=buffer.size();buffer.resize(previous+next-address);
                    if(!game_memory::copy(buffer.data()+previous,reinterpret_cast<const void*>(address),next-address))
                        throw HandlerError("image_scan_failed","Mapped image memory changed during signature capture");
                } else {search(buffer_rva,buffer);buffer.clear();}
                address=next;
            }
            search(buffer_rva,buffer);
        }
        std::sort(matches.begin(),matches.end());
        matches.erase(std::unique(matches.begin(),matches.end()),matches.end());
        return matches;
    }
    std::uintptr_t unique(std::string_view name,std::span<const std::uint8_t> pattern,std::string_view mask,
                           bool executable_only=true) const {
        const auto matches=scan(pattern,mask,executable_only);
        if(matches.size()!=1)
            throw HandlerError("image_pattern_not_unique",std::string(name)+" signature matched "+std::to_string(matches.size())+" locations");
        return matches.front();
    }
    std::uintptr_t unique(std::string_view name,std::string_view pattern,bool executable_only=true) const {
        std::vector<std::uint8_t> bytes;std::string mask;parse(pattern,bytes,mask);
        return unique(name,bytes,mask,executable_only);
    }
    std::uintptr_t find_unique(std::string_view name,std::string_view pattern,bool executable_only=true) const {
        return unique(name,pattern,executable_only);
    }
    std::uintptr_t resolve_rip(std::uintptr_t instruction_rva,std::size_t displacement_offset,
                               std::size_t instruction_size) const {
        if(!contains(instruction_rva,instruction_size) || displacement_offset>instruction_size
           || sizeof(std::int32_t)>instruction_size-displacement_offset)
            throw HandlerError("image_rip_invalid","A RIP displacement must lie in its confirmed instruction");
        const auto address=base_+instruction_rva+displacement_offset;
        if(!readable_range(address,sizeof(std::int32_t)))
            throw HandlerError("image_rip_invalid","RIP displacement memory is not readable");
        const auto displacement=game_memory::read<std::int32_t>(address);
        const auto target=static_cast<std::int64_t>(instruction_rva)+static_cast<std::int64_t>(instruction_size)+displacement;
        if(target<0 || static_cast<std::uint64_t>(target)>=size_)
            throw HandlerError("image_rip_invalid","RIP target lies outside the mapped image");
        return static_cast<std::uintptr_t>(target);
    }
    std::uintptr_t rip_target(std::uintptr_t match_rva,std::size_t displacement_offset,
                              std::size_t instruction_end_offset) const {
        return resolve_rip(match_rva,displacement_offset,instruction_end_offset);
    }
};

} // namespace g1r_mcp::image_patterns
