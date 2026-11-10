#include <Windows.h>
#include <memory>
#include <fstream>
#include "sn2_mcp/game_access.hpp"
#include "sn2_mcp/transport.hpp"

namespace {
sn2_mcp::GameAccess* game{};
sn2_mcp::Transport* transport{};
HMODULE module{};
void phase(const char* text) {
    wchar_t path[32768]{};GetModuleFileNameW(module,path,32768);
    std::wstring file(path);file=file.substr(0,file.find_last_of(L"\\/"))+L"\\native-phase.log";
    std::ofstream output(file,std::ios::app);output<<text<<"\n";
}
LONG WINAPI exception_log(EXCEPTION_POINTERS* info) {
    auto code=info->ExceptionRecord->ExceptionCode;
    if(code==EXCEPTION_ACCESS_VIOLATION || code==EXCEPTION_ILLEGAL_INSTRUCTION || code==EXCEPTION_STACK_OVERFLOW) {
        char buffer[160]{};sprintf_s(buffer,"exception code=%08lx address=%p thread=%lu",code,info->ExceptionRecord->ExceptionAddress,GetCurrentThreadId());phase(buffer);
    }
    return EXCEPTION_CONTINUE_SEARCH;
}
DWORD WINAPI initialize(void*) {
    // Objects live until process exit; the DLL is not unloaded while Tick runs.
    phase("init_enter");
    AddVectoredExceptionHandler(1,exception_log);
    try {
        game=new sn2_mcp::GameAccess();
        transport=new sn2_mcp::Transport([](const nlohmann::json& request){
            nlohmann::json result;
            if(request.at("op")=="enable_game")phase("enable_game_enter");
            result=game->request(request);
            if(request.at("op")=="enable_game")phase("enable_game_return");
            if(request.at("op")=="status" || request.at("op")=="enable_game") {
                result["session_id"]=request.at("session_id");
                result["protocol"]=1;
            }
            return result;
        }, sn2_mcp::DefaultPort, std::chrono::milliseconds{5000},
        [](const std::string& session) { game->disconnect(session); });
        phase("transport_created");transport->start();phase("transport_started");
    } catch(const std::exception& error) {phase(error.what());}
    catch(...) {phase("initialization_exception");}
    return 0;
}
}
extern "C" __declspec(dllexport) DWORD SN2MCPProtocolVersion() {return 1;}
BOOL WINAPI DllMain(HINSTANCE instance,DWORD reason,LPVOID) {
    if(reason==DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(instance);
        module=instance;
        auto thread=CreateThread(nullptr,0,initialize,nullptr,0,nullptr);if(thread)CloseHandle(thread);
    }
    return TRUE;
}
