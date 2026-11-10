#include <g1r_mcp/array_page.hpp>
#include <iostream>
#include <cstring>
#include <stdexcept>
#include <vector>

namespace {
void require(bool value,const char* message){if(!value)throw std::runtime_error(message);}
template<class Operation>void rejects(const char* code,Operation operation) {
    try {operation();} catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неожиданный код ошибки массива");return;
    }
    throw std::runtime_error("Ожидался отказ массива");
}
}
int main() {
    try {
        const g1r_mcp::ArrayHeader header{0x1000,205,256};
        const auto first=g1r_mcp::array_page(header,4,1,0,200);
        require(first.begin==0x1000 && first.bytes==800 && first.count==200 && first.has_more,"Неверная первая страница");
        const auto over_old_limit=g1r_mcp::array_page(header,4,1,0,201);
        require(over_old_limit.begin==0x1000 && over_old_limit.bytes==804 && over_old_limit.count==201
                && over_old_limit.next_offset==201 && over_old_limit.has_more,"Запрошенные 201 элемент не сокращены до прежнего лимита");
        const auto tail=g1r_mcp::array_page(header,4,1,200,200);
        require(tail.begin==0x1320 && tail.count==5 && tail.bytes==20 && !tail.has_more,"Неверная последняя страница");
        const auto eof=g1r_mcp::array_page(header,4,1,205,64);
        require(eof.count==0 && eof.bytes==0 && !eof.has_more,"EOF не пустой");
        require(g1r_mcp::array_page({},8,1,0,64).count==0,"Пустой массив отклонён");
        require(g1r_mcp::array_page({0x1000,0,8},8,1,0,64).count==0,"Пустой массив с резервом отклонён");
        rejects("invalid_array_page",[&]{g1r_mcp::array_page(header,4,1,-1,64);});
        rejects("invalid_array_page",[&]{g1r_mcp::array_page(header,4,1,206,64);});
        rejects("invalid_array_page",[&]{g1r_mcp::array_page(header,4,1,0,0);});
        rejects("unsupported_array_layout",[&]{g1r_mcp::array_page(header,4,2,0,64);});
        rejects("unsupported_array_layout",[&]{g1r_mcp::array_page(header,0,1,0,64);});
        rejects("invalid_array_header",[]{g1r_mcp::array_page({0x1000,-1,1},4,1,0,64);});
        rejects("invalid_array_header",[]{g1r_mcp::array_page({0x1000,2,1},4,1,0,64);});
        rejects("invalid_array_header",[]{g1r_mcp::array_page({0,1,1},4,1,0,64);});
        const auto reserved=g1r_mcp::array_page({0x1000,1,1000001},4,1,0,64);
        require(reserved.count==1 && reserved.bytes==4 && !reserved.has_more,"Большая ёмкость не меняет одну существующую запись");
        const auto million=g1r_mcp::array_page({0x1000,1000001,1000001},4,1,1000000,10);
        require(million.begin==0x1000+4000000 && million.count==1 && million.bytes==4
                && million.next_offset==1000001 && !million.has_more,"Хвост массива больше миллиона доступен без выделения полного массива");
        const auto maximum=g1r_mcp::array_page({0x1000,std::numeric_limits<std::int32_t>::max(),std::numeric_limits<std::int32_t>::max()},
                8,1,std::numeric_limits<std::int32_t>::max()-1,1);
        require(maximum.count==1 && maximum.bytes==8 && maximum.next_offset==std::numeric_limits<std::int32_t>::max()
                && !maximum.has_more,"Последняя запись int32 массива сохраняет представимое продолжение");
        rejects("invalid_array_header",[]{g1r_mcp::array_page({std::numeric_limits<std::uintptr_t>::max()-8,2,2},8,1,0,64);});
        const auto wide=g1r_mcp::array_page({0x1000,2,2},65537,1,0,1);
        require(wide.count==1 && wide.bytes==65537 && wide.next_offset==1 && wide.has_more,"Положительный stride больше 64 КиБ не отклонён");

        std::vector<std::int32_t> original(20003);
        for(std::size_t index=0;index<original.size();++index)original[index]=static_cast<std::int32_t>(index)*3-10000;
        const g1r_mcp::ArrayHeader copied_header{reinterpret_cast<std::uintptr_t>(original.data()),20003,20003};
        const auto large_first=g1r_mcp::array_page(copied_header,sizeof(std::int32_t),1,0,17000);
        require(large_first.count==17000 && large_first.bytes==68000 && large_first.next_offset==17000 && large_first.has_more,
                "Запрошенная страница больше 200 элементов и 64 КиБ сохраняет размер и продолжение");
        std::vector<std::int32_t> reconstructed(static_cast<std::size_t>(large_first.count));
        std::memcpy(reconstructed.data(),reinterpret_cast<const void*>(large_first.begin),large_first.bytes);
        const auto large_tail=g1r_mcp::array_page(copied_header,sizeof(std::int32_t),1,large_first.next_offset,17000);
        require(large_tail.count==3003 && large_tail.bytes==12012 && large_tail.next_offset==20003 && !large_tail.has_more,
                "Последняя большая страница ограничена фактическим концом массива");
        const auto copied_count=reconstructed.size();
        reconstructed.resize(copied_count+static_cast<std::size_t>(large_tail.count));
        std::memcpy(reconstructed.data()+copied_count,reinterpret_cast<const void*>(large_tail.begin),large_tail.bytes);
        require(reconstructed==original,"Две запрошенные страницы восстанавливают все значения в исходном порядке");
        const auto large_eof=g1r_mcp::array_page(copied_header,sizeof(std::int32_t),1,large_tail.next_offset,17000);
        require(large_eof.count==0 && large_eof.bytes==0 && large_eof.next_offset==20003 && !large_eof.has_more,
                "EOF после больших страниц не создаёт повторных элементов");
        require(g1r_mcp::same_array_header(header,header),"Одинаковый заголовок не сравнивается");
        require(!g1r_mcp::same_array_header(header,{0x2000,205,256}),"Смена адреса не замечена");
        require(!g1r_mcp::same_array_header(header,{0x1000,204,256}),"Смена размера не замечена");
        require(!g1r_mcp::same_array_header(header,{0x1000,205,512}),"Смена ёмкости не замечена");
        std::cout<<"Проверки страниц массива пройдены\n";return 0;
    } catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
