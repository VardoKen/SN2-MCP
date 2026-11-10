#pragma once

#include <sn2_mcp/transport.hpp>
#include <cstdint>
#include <exception>
#include <functional>
#include <limits>
#include <string>
#include <utility>

namespace sn2_mcp {
// Copied FString/TArray<TCHAR> layout for the current x64 build.
// This validation does not establish allocator ownership; ownership follows
// from the successfully executed StrProperty return contract of ProcessEvent.
struct StringHeader {
    std::uintptr_t data;
    std::int32_t count;
    std::int32_t capacity;
};
static_assert(sizeof(StringHeader)==16);

inline void validate_string_header(const StringHeader& header) {
    if(header.count<0 || header.capacity<0 || header.count>header.capacity)
        throw HandlerError("invalid_string","Invalid FString sizes");
    if(!header.data) {
        if(header.count || header.capacity)
            throw HandlerError("invalid_string","FString without a pointer has a nonzero size or capacity");
        return;
    }
    if(!header.capacity || header.data%alignof(char16_t))
        throw HandlerError("invalid_string","Invalid FString pointer or capacity");
    const auto bytes=static_cast<std::uintptr_t>(header.capacity)*sizeof(char16_t);
    if(header.data>std::numeric_limits<std::uintptr_t>::max()-bytes)
        throw HandlerError("invalid_string","The FString address range overflows");
}

inline nlohmann::json string_cleanup_details(bool attempted,bool confirmed,
                                             const std::string& cause) {
    return {{"executed",true},{"may_have_executed",true},{"do_not_retry",true},
            {"cause_code",cause},{"cleanup_attempted",attempted},
            {"cleanup_confirmed",confirmed},{"cleanup_state",confirmed?"released":"unknown"}};
}

// Decoder only reads the header; Releaser runs exactly once for a valid
// nonzero pointer. A failed release must not be retried.
template<class Decoder,class Releaser>
auto copied_owned_string_return(const StringHeader& header,Decoder&& decoder,Releaser&& releaser) {
    try {validate_string_header(header);}
    catch(const HandlerError& error) {
        throw HandlerError("game_result_error","The call executed but FString has an invalid layout; cleanup is unconfirmed",
                           string_cleanup_details(false,false,error.code()));
    }
    using Result=std::invoke_result_t<Decoder,const StringHeader&>;
    Result result{};
    std::exception_ptr decoding_error;
    std::string cause="result_processing_failed";
    try {result=std::invoke(std::forward<Decoder>(decoder),header);}
    catch(const HandlerError& error) {cause=error.code();decoding_error=std::current_exception();}
    catch(...) {decoding_error=std::current_exception();}
    bool cleanup_confirmed=true;
    if(header.data) {
        try {cleanup_confirmed=std::invoke(std::forward<Releaser>(releaser),header.data);}
        catch(...) {cleanup_confirmed=false;}
    }
    if(!cleanup_confirmed) {
        auto details=string_cleanup_details(true,false,"string_cleanup_fault");
        if(decoding_error)details["result_cause_code"]=cause;
        throw HandlerError("game_result_error","The call executed; FString release is unconfirmed; do not repeat the request",
                           std::move(details));
    }
    if(decoding_error)
        throw HandlerError("game_result_error","The call executed and FString was released, but its result could not be read; do not repeat the request",
                           string_cleanup_details(header.data!=0,true,cause));
    return result;
}
} // namespace sn2_mcp
