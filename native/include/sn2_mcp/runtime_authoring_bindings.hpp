#pragma once

#include <sn2_mcp/image_patterns.hpp>
#include <sn2_mcp/runtime_bindings.hpp>
#include <array>
#include <map>
#include <string>

namespace sn2_mcp::runtime_authoring {
// The signatures below identify exact source-correlated Shipping operations.
// Discovery is lazy for authoring and never gates unrelated runtime tools.
struct Bindings {
    std::map<std::string,RuntimeBinding> entries;
    std::uintptr_t address(std::uintptr_t base,const std::string& name) const {
        const auto found=entries.find(name);
        if(found==entries.end() || !found->second.available())
            throw HandlerError("runtime_authoring_binding_unavailable",name+" is unavailable",
                {{"binding",name},{"discovery",found==entries.end()?nlohmann::json(nullptr):found->second.describe()}});
        return base+found->second.rva;
    }
    nlohmann::json describe() const {
        auto result=nlohmann::json::object();
        for(const auto& [name,binding]:entries)result[name]=binding.describe();
        return result;
    }
};

inline Bindings discover(std::uintptr_t base) {
    image_patterns::Image image(base);
    Bindings result;
    const auto function=[&](const std::string& name,std::string_view pattern,std::size_t offset=0) {
        RuntimeBinding binding;
        try {
            const auto match=image.find_unique(name,pattern);
            if(match<offset)throw HandlerError("binding_unwind_mismatch","The matched body precedes its expected entry");
            const auto entry=match-offset;
            DWORD64 owner{};
            const auto unwind=RtlLookupFunctionEntry(base+match,&owner,nullptr);
            DWORD64 entry_owner{};
            const auto entry_unwind=RtlLookupFunctionEntry(base+entry,&entry_owner,nullptr);
            if(!unwind || owner!=base || !entry_unwind || entry_owner!=base
               || entry_unwind->BeginAddress!=entry || unwind->EndAddress<=match)
                throw HandlerError("binding_unwind_mismatch","The signature does not match its expected unwind function");
            binding.rva=entry;
            binding.discovery={{"method","unique_function_body_and_pe_unwind"},{"body_rva",match},
                {"body_offset",offset},{"function_end_rva",unwind->EndAddress}};
        }catch(const HandlerError& error){binding.error=error.code()+": "+error.what();}
        result.entries.emplace(name,std::move(binding));
    };
    function("construct_parameters",
        "40 53 48 83 EC 20 48 8B D9 48 89 11 E8 ?? ?? ?? ?? 48 89 43 08 33 C0 48 89 43 10 48 89 43 18 66 89 43 20");
    function("construct_object",
        "4C 8B DC 55 53 41 56 49 8D AB 28 FE FF FF 48 81 EC C0 02 00 00 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 85 A0 01 00 00 8B 41 70");
    function("field_try_construct",
        "48 8B 40 30 44 8B C6 48 8B D5 49 8B CE FF D0",0xb3);
    function("static_link",
        "44 0F B6 C3 48 89 44 24 20 48 8D 54 24 20 48 8B 07 48 8B CF FF 90 C8 02 00 00",0x24);
    function("class_set_super","48 8D 4B 30 48 89 7B 40",0x33);
    function("function_bind",
        "48 89 5C 24 18 48 89 74 24 20 57 48 83 EC 40 33 F6 48 8B F9 48 8B D9 48 85 C9");
    function("blueprint_class_bind",
        "40 53 48 83 EC 20 48 8B D9 E8 ?? ?? ?? ?? E8 ?? ?? ?? ?? 84 C0 74 ?? 48 83 BB 80 02 00 00 00");
    function("link_functions","49 8B 6F 08 41 FF 17 49 8B 4D 48 4C 8B F0 48 89 48 28",0x53);
    function("set_root_flags",
        "40 53 56 48 83 EC 48 48 8B D9");
    function("clear_root_flags",
        "8B 4B 08 0F BA E7 1D 73 0B 8B 43 14 85 C0 74 04 0F BA F7 1D F7 C1 00 00 50 6E",0x21);
    function("create_default_object",
        "40 57 B8 F0 10 00 00 E8 ?? ?? ?? ?? 48 2B E0 48");
    function("collect_bytecode_references",
        "48 89 5C 24 10 57 48 81 EC C0 00 00 00 48 8B D9");
    function("archive_state_constructor",
        "48 89 5C 24 08 48 89 74 24 10 57 48 83 EC 20 33 F6 48 8D 05 ?? ?? ?? ?? 48 89 01 48 8B D9 48 89 71 40 48 8D 41 10");
    function("archive_state_destructor",
        "48 89 5C 24 08 57 48 83 EC 20 48 8B 79 58 48 8D 05 ?? ?? ?? ?? 48 89 01 48 8B D9 48 85 FF");
    function("serialize_default_object",
        "48 8B 01 48 8B F2 48 8B F9 49 8B D0 48 8D 4C 24 28 48 8B 98 B8 03 00 00",0x1d);
    function("class_preload_dependencies",
        "48 89 5C 24 18 55 56 57 48 81 EC 60 07 00 00 48 8B 05 ?? ?? ?? ?? 48 33 C4 48 89 84 24 58 07 00 00 48 8B FA 48 8B E9");
    function("blueprint_class_preload_dependencies",
        "48 89 5C 24 10 48 89 6C 24 18 48 89 74 24 20 57 48 83 EC 50 48 8B DA 48 8B F9 E8 ?? ?? ?? ?? 48 8B 77 40");
    // UObject::GetArchetype is a leaf wrapper that sets Policy=nullptr and
    // jumps to the source-correlated cached-archetype implementation.
    {
        RuntimeBinding binding;
        try {
            const auto body=image.find_unique("get_archetype_body",
                "48 89 54 24 10 57 48 83 EC 60 80 3D ?? ?? ?? ?? 00 48 8B F9");
            DWORD64 owner{};
            const auto unwind=RtlLookupFunctionEntry(base+body,&owner,nullptr);
            if(!unwind || owner!=base || unwind->BeginAddress!=body || unwind->EndAddress<=body
               || body<0x30 || game_memory::read<std::uint8_t>(base+body-0x30)!=0x33
               || game_memory::read<std::uint8_t>(base+body-0x2f)!=0xd2
               || game_memory::read<std::uint8_t>(base+body-0x2e)!=0xe9
               || image.resolve_rip(body-0x2e,1,5)!=body)
                throw HandlerError("binding_archetype_entry_mismatch","The archetype implementation does not have the expected public leaf entry");
            binding.rva=body-0x30;
            binding.discovery={{"method","unique_cached_archetype_body_unwind_and_public_leaf"},
                {"body_rva",body},{"function_end_rva",unwind->EndAddress},{"policy","null"}};
        }catch(const HandlerError& error){binding.error=error.code()+": "+error.what();}
        result.entries.emplace("get_archetype",std::move(binding));
    }
    // The public MarkAsReachable entry is a leaf jump with no unwind record.
    // Resolve it from its unique underlying registry/GC body instead.
    {
        RuntimeBinding binding;
        try {
            const auto match=image.find_unique("mark_as_reachable_body",
                "8B 41 0C 45 33 C9 3B 05 ?? ?? ?? ?? 7D ?? 8B C8 0F B7 C0 48 C1 E9 10 48 8D 14 40 48 8B 05");
            if(match<0x10 || game_memory::read<std::uint8_t>(base+match-0x10)!=0xe9
               || image.resolve_rip(match-0x10,1,5)!=match)
                throw HandlerError("binding_jump_mismatch","The reachability helper has an unexpected entry");
            binding.rva=match-0x10;binding.discovery={{"method","unique_gc_body_and_relative_entry"},{"body_rva",match}};
        }catch(const HandlerError& error){binding.error=error.code()+": "+error.what();}
        result.entries.emplace("mark_as_reachable",std::move(binding));
    }
    {
        RuntimeBinding binding;
        try {
            const auto ctor=result.address(base,"archive_state_constructor")-base;
            constexpr std::array<std::uint8_t,47> pattern{
                0xe8,0,0,0,0,0x48,0x8d,0x05,0,0,0,0,0x48,0x8d,0x0d,0,0,0,0,
                0x48,0x89,0x05,0,0,0,0,0xe8,0,0,0,0,0x48,0x8d,0x0d,0,0,0,0,
                0x48,0x83,0xc4,0x28,0xe9,0,0,0,0};
            const auto candidates=image.scan(pattern,"x????xxx????xxx????xxx????x????xxx????xxxxx????");
            std::vector<std::uintptr_t> matches;
            for(const auto candidate:candidates)
                if(image.resolve_rip(candidate,1,5)==ctor)matches.push_back(candidate);
            if(matches.size()!=1)
                throw HandlerError("binding_archive_constructor_not_unique",
                    "The full archive references have "+std::to_string(matches.size())+" calls to the discovered state constructor");
            const auto match=matches.front();
            if(image.resolve_rip(match,1,5)!=ctor)
                throw HandlerError("binding_archive_constructor_mismatch","The full archive initializer does not call the discovered state constructor");
            const auto table=image.resolve_rip(match+5,3,7);
            if(!image.contains(table,76*sizeof(std::uintptr_t)))
                throw HandlerError("binding_outside_image","The full archive table exceeds the image");
            binding.rva=table;binding.discovery={{"method","archive_reference_signature_and_unique_state_constructor_call"},
                {"signature_candidates",candidates.size()},{"body_rva",match}};
        }catch(const HandlerError& error){binding.error=error.code()+": "+error.what();}
        result.entries.emplace("full_archive_vtable",std::move(binding));
    }
    return result;
}
} // namespace sn2_mcp::runtime_authoring
