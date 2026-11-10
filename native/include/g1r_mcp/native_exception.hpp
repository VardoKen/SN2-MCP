#pragma once

#include <Windows.h>
#include <cstdint>
#include <iomanip>
#include <sstream>
#include <string>
#include <nlohmann/json.hpp>

namespace g1r_mcp::native_exception {

struct Diagnostic {
    std::uint32_t code;
    std::uint32_t parameter_count;
    std::uintptr_t exception_address;
    bool has_access_violation;
    std::uintptr_t access_operation;
    std::uintptr_t access_address;
};

inline int capture(EXCEPTION_POINTERS* information,Diagnostic* result) noexcept {
    const auto* record=information->ExceptionRecord;
    *result={};
    result->code=record->ExceptionCode;
    result->exception_address=reinterpret_cast<std::uintptr_t>(record->ExceptionAddress);
    result->parameter_count=record->NumberParameters;
    if(record->ExceptionCode==EXCEPTION_ACCESS_VIOLATION && record->NumberParameters>=2) {
        result->has_access_violation=true;
        result->access_operation=record->ExceptionInformation[0];
        result->access_address=record->ExceptionInformation[1];
    }
    return EXCEPTION_EXECUTE_HANDLER;
}

inline std::string hexadecimal(std::uintptr_t value,int width=0) {
    std::ostringstream output;
    output<<"0x"<<std::hex<<std::setfill('0')<<std::setw(width)<<value;
    return output.str();
}

inline nlohmann::json describe(const Diagnostic& diagnostic) {
    nlohmann::json result={{"code",hexadecimal(diagnostic.code,8)},
        {"exception_address",hexadecimal(diagnostic.exception_address)},
        {"parameter_count",diagnostic.parameter_count}};
    if(diagnostic.has_access_violation) {
        const char* operation="unknown";
        if(diagnostic.access_operation==0)operation="read";
        else if(diagnostic.access_operation==1)operation="write";
        else if(diagnostic.access_operation==8)operation="execute";
        result["access_violation"]={{"operation",operation},
            {"operation_code",diagnostic.access_operation},{"address",hexadecimal(diagnostic.access_address)}};
    }
    return result;
}

} // namespace g1r_mcp::native_exception
