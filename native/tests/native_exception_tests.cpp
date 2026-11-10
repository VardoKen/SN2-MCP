#include <sn2_mcp/native_exception.hpp>

#include <array>
#include <iostream>
#include <stdexcept>
#include <type_traits>

namespace {
using sn2_mcp::native_exception::Diagnostic;
constexpr DWORD custom_code=0xe0424741;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}

int observe_record(EXCEPTION_POINTERS* information,Diagnostic* diagnostic,std::uintptr_t* observed_address) noexcept {
    *observed_address=reinterpret_cast<std::uintptr_t>(information->ExceptionRecord->ExceptionAddress);
    return sn2_mcp::native_exception::capture(information,diagnostic);
}

// SEH-кадр содержит только POD: форматирование JSON и C++-объекты находятся снаружи.
__declspec(noinline) bool raise_custom_exception(Diagnostic* diagnostic,std::uintptr_t* observed_address) noexcept {
    const ULONG_PTR parameters[3]={0x11,0x22,0x33};
    __try {
        RaiseException(custom_code,0,3,parameters);
        return false;
    } __except(observe_record(GetExceptionInformation(),diagnostic,observed_address)) {
        return true;
    }
}

Diagnostic synthetic_access_violation(ULONG_PTR operation,DWORD count) {
    EXCEPTION_RECORD record{};
    record.ExceptionCode=EXCEPTION_ACCESS_VIOLATION;
    record.ExceptionAddress=reinterpret_cast<void*>(std::uintptr_t{0x12345678});
    record.NumberParameters=count;
    record.ExceptionInformation[0]=operation;
    record.ExceptionInformation[1]=0x87654320;
    EXCEPTION_POINTERS information{&record,nullptr};
    Diagnostic diagnostic{0xffffffff,15,1,true,99,1};
    require(sn2_mcp::native_exception::capture(&information,&diagnostic)==EXCEPTION_EXECUTE_HANDLER,
            "POD-фильтр должен передать управление обработчику SEH");
    record.ExceptionCode=custom_code;record.ExceptionAddress=nullptr;record.ExceptionInformation[1]=0;
    require(diagnostic.code==EXCEPTION_ACCESS_VIOLATION && diagnostic.parameter_count==count &&
            diagnostic.exception_address==0x12345678,"Диагностика немедленно копирует код, адрес инструкции и число параметров записи");
    return diagnostic;
}
}

int main() {
    using namespace sn2_mcp::native_exception;
    static_assert(std::is_trivial_v<Diagnostic> && std::is_standard_layout_v<Diagnostic>);
    static_assert(noexcept(capture(static_cast<EXCEPTION_POINTERS*>(nullptr),static_cast<Diagnostic*>(nullptr))));
    try {
        Diagnostic actual{0xffffffff,15,1,true,8,1};std::uintptr_t observed_address{};
        require(raise_custom_exception(&actual,&observed_address),"Настоящий RaiseException должен быть обработан локальным SEH-кадром");
        require(actual.code==custom_code && actual.parameter_count==3 && actual.exception_address!=0 &&
                actual.exception_address==observed_address,"SEH сохраняет настоящий код, адрес из EXCEPTION_RECORD и число переданных параметров");
        require(!actual.has_access_violation && actual.access_operation==0 && actual.access_address==0,
                "Пользовательское исключение не превращает параметры в данные access violation");
        const auto actual_json=describe(actual);
        require(actual_json.at("code")=="0xe0424741" && actual_json.at("parameter_count")==3 &&
                actual_json.at("exception_address")==hexadecimal(observed_address) && !actual_json.contains("access_violation"),
                "JSON описывает фактическое исключение без выдуманного access violation");

        const std::array<const char*,3> names{"read","write","execute"};
        const std::array<ULONG_PTR,3> operations{0,1,8};
        for(std::size_t i=0;i<operations.size();++i) {
            const auto diagnostic=synthetic_access_violation(operations[i],2);
            require(diagnostic.has_access_violation && diagnostic.access_operation==operations[i] && diagnostic.access_address==0x87654320,
                    "Access violation копирует операцию и адрес данных из ExceptionInformation");
            const auto formatted=describe(diagnostic);
            require(formatted.at("code")=="0xc0000005" && formatted.at("exception_address")=="0x12345678" &&
                    formatted.at("parameter_count")==2,"JSON сохраняет отдельно адрес инструкции и число параметров AV");
            require(formatted.at("access_violation")==nlohmann::json{{"operation",names[i]},
                    {"operation_code",operations[i]},{"address","0x87654320"}},"JSON различает чтение, запись и исполнение по исходной операции AV");
        }
        const auto incomplete=synthetic_access_violation(8,1);
        require(!incomplete.has_access_violation && incomplete.access_operation==0 && incomplete.access_address==0,
                "Одного параметра недостаточно для пары операция/адрес, прежняя AV-диагностика очищается");
        const auto incomplete_json=describe(incomplete);
        require(incomplete_json.at("code")=="0xc0000005" && incomplete_json.at("parameter_count")==1 &&
                !incomplete_json.contains("access_violation"),"Неполная AV-запись сохраняет подтверждённые поля и не создаёт отсутствующую пару");
        std::cout<<"native_exception: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
