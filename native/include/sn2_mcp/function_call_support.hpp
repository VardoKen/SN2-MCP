#pragma once

#include <sn2_mcp/transport.hpp>
#include <algorithm>
#include <array>
#include <string_view>

namespace sn2_mcp {
inline void require_flat_parameter_call(std::string_view owner_path,std::string_view function_name) {
    // CL131439 KismetArrayLibrary registration RVA 0x40c2d20/table 0xa299bd0.
    // These individually inspected exec wrappers obtain wildcard properties
    // and addresses from FFrame.Step, not just the parameter buffer.
    constexpr std::array<std::string_view,24> wildcard_functions={
        "Array_Add","Array_AddUnique","Array_Append","Array_Clear","Array_Contains","Array_Find","Array_Get",
        "Array_Identical","Array_Insert","Array_IsEmpty","Array_IsNotEmpty","Array_IsValidIndex",
        "Array_LastIndex","Array_Length","Array_Random","Array_RandomFromStream","Array_Remove","Array_RemoveItem",
        "Array_Resize","Array_Reverse","Array_Set","Array_Shuffle","Array_ShuffleFromStream","Array_Swap"};
    if(owner_path=="/Script/Engine.KismetArrayLibrary"
       && std::find(wildcard_functions.begin(),wildcard_functions.end(),function_name)!=wildcard_functions.end())
        throw HandlerError("custom_thunk_requires_vm_stack",
            "This array function requires the VM stack's wildcard property and address; a flat ProcessEvent parameter buffer is insufficient",
            {{"executed",false},{"may_have_executed",false},{"process_event_returned",false},
             {"owner",owner_path},{"function",function_name},{"required_context","FFrame.Step wildcard property and address"}});
}
} // namespace sn2_mcp
