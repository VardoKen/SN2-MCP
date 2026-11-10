#include <sn2_mcp/class_arguments.hpp>
#include <iostream>
#include <stdexcept>

using nlohmann::json;
namespace {
void require(bool condition,const char* message) {if(!condition)throw std::runtime_error(message);}
template<class Operation>void rejects(const char* code,Operation operation) {
    try {operation();} catch(const sn2_mcp::HandlerError& error) {
        require(error.code()==code,"Неожиданный код классового отказа");return;
    }
    throw std::runtime_error("Ожидался отказ");
}
}
int main() {
    try {
        // Представляемые игровые классы: Object(1) <- Actor(2) <- Pawn(3).
        // Метаклассы: Class(10) <- BlueprintGeneratedClass(11).
        const auto parent=[](std::uintptr_t klass)->std::uintptr_t {
            if(klass==3)return 2;if(klass==2)return 1;if(klass==11)return 10;return 0;
        };
        require(sn2_mcp::compatible_class_argument(3,11,10,2,parent),"Наследник с допустимым метаклассом отклонён");
        require(sn2_mcp::compatible_class_argument(2,10,10,2,parent),"Точные оба класса отклонены");
        require(!sn2_mcp::compatible_class_argument(1,11,10,2,parent),"UObject принят вместо Actor подкласса");
        require(!sn2_mcp::compatible_class_argument(3,10,11,2,parent),"PropertyClass метакласс не проверен");
        require(!sn2_mcp::compatible_class_argument(3,11,10,10,parent),"Проверка MetaClass ошибочно использовала метакласс");
        require(sn2_mcp::class_argument_handle(json(nullptr)).empty(),"Null класса отклонён");
        require(sn2_mcp::class_argument_handle(json{{"handle","obj-1"}})=="obj-1","Handle класса отклонён");
        rejects("invalid_class_argument",[]{sn2_mcp::class_argument_handle(json(10));});
        rejects("invalid_class_argument",[]{sn2_mcp::class_argument_handle(json{{"handle","obj-1"},{"pointer",10}});});
        rejects("invalid_object_class",[]{sn2_mcp::compatible_class_argument(1,10,10,2,[](std::uintptr_t value){return value;});});
        const auto long_parent=[](std::uintptr_t value){return value<200?value+1:std::uintptr_t{0};};
        require(sn2_mcp::compatible_class_argument(1,10,10,200,long_parent),"Предок класса после 64 звеньев отклонён");
        require(!sn2_mcp::compatible_class_argument(1,10,10,201,long_parent),"Отсутствующий предок принят в длинной цепочке классов");
        rejects("invalid_object_class",[]{sn2_mcp::compatible_class_argument(1,10,10,201,
            [](std::uintptr_t value){return value<200?value+1:std::uintptr_t{65};});});
        std::cout<<"Проверки ClassProperty пройдены\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
