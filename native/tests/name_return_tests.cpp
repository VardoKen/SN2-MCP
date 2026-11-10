#include <sn2_mcp/name_return.hpp>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
void require(bool condition,const char* message) {if(!condition)throw std::runtime_error(message);}
template<class Operation>void rejects(Operation operation) {
    try {operation();} catch(const sn2_mcp::HandlerError& error) {
        require(error.code()=="unsupported_signature","Неожиданный код FName отказа");
        require(error.details().at("may_have_executed")==false,"До-вызовный отказ отмечен выполненным");
        return;
    }
    throw std::runtime_error("Ожидался отказ FName");
}
}
int main() {
    try {
        int reads=0,decodes=0;
        const std::uint64_t opaque=(std::uint64_t{17}<<32)|1234;
        const auto reader=[&]{++reads;return opaque;};
        const auto decoder=[&](std::uint64_t copied) {
            ++decodes;require(copied==opaque,"Потеря FName index/number при копировании");
            return std::string("Socket_16");
        };
        sn2_mcp::validate_name_return_layout(8,1);
        require(reads==0 && decodes==0,"Preflight прочитал pool до игрового вызова");
        require(sn2_mcp::copied_name_return(8,1,reader,decoder)=="Socket_16","Возврат decoder потерян");
        require(reads==1 && decodes==1,"Возврат прочитан больше одного раза");
        rejects([&]{sn2_mcp::copied_name_return(4,1,reader,decoder);});
        rejects([&]{sn2_mcp::copied_name_return(16,1,reader,decoder);});
        rejects([&]{sn2_mcp::copied_name_return(8,2,reader,decoder);});
        require(reads==1 && decodes==1,"Неверная раскладка вызвала read/decoder");
        rejects([]{sn2_mcp::reject_name_input();});
        std::cout<<"Проверки возврата FName пройдены\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
