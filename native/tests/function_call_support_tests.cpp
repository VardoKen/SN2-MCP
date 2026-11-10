#include <sn2_mcp/function_call_support.hpp>
#include <cassert>

int main() {
    bool refused=false;
    try {sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetArrayLibrary","Array_Length");}
    catch(const sn2_mcp::HandlerError& error) {
        refused=true;
        assert(error.code()=="custom_thunk_requires_vm_stack");
        assert(error.details().at("executed")==false);
        assert(error.details().at("may_have_executed")==false);
        assert(error.details().at("process_event_returned")==false);
    }
    assert(refused);
    sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetMathLibrary","MinOfIntArray");
    sn2_mcp::require_flat_parameter_call("/Script/Niagara.NiagaraDataInterfaceArrayFunctionLibrary","GetNiagaraArrayInt32");
    sn2_mcp::require_flat_parameter_call("/Script/Test.TypedArrayLibrary","Array_Length");
    sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetArrayLibrary","UnverifiedOtherFunction");
    sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetArrayLibrary","SortIntArray");
    sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetArrayLibrary","FilterArray");
    for(const auto function:{"Array_Add","Array_AddUnique","Array_Append","Array_Clear","Array_Contains","Array_Find","Array_Get",
        "Array_Identical","Array_Insert","Array_IsEmpty","Array_IsNotEmpty","Array_IsValidIndex","Array_LastIndex",
        "Array_Random","Array_RandomFromStream","Array_Remove","Array_RemoveItem","Array_Resize","Array_Reverse","Array_Set",
        "Array_Shuffle","Array_ShuffleFromStream","Array_Swap"}) {
        bool blocked=false;
        try {sn2_mcp::require_flat_parameter_call("/Script/Engine.KismetArrayLibrary",function);}
        catch(const sn2_mcp::HandlerError& error){blocked=error.code()=="custom_thunk_requires_vm_stack";}
        assert(blocked);
    }
}
