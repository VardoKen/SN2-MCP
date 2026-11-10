#include <g1r_mcp/name_argument.hpp>
#include <iostream>
#include <stdexcept>
#include <vector>

namespace {
int checks=0;
void require(bool condition,const char* message){++checks;if(!condition)throw std::runtime_error(message);}
template<class Operation>void rejects(const char* code,Operation operation,
    const nlohmann::json& details={{"executed",false},{"may_have_executed",false},{"do_not_retry",false}}) {
    try {operation();}catch(const g1r_mcp::HandlerError& error){
        require(error.code()==code,"Неверный код отказа FName");
        require(error.details()==details,"Отказ FName должен сохранять точные сведения об отсутствии исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ FName");
}
}
int main() {
    using namespace g1r_mcp;
    using nlohmann::json;
    try {
        const json good={{"name_from",{{"handle","obj-7"},{"property_name","StoredEntry"}}}};
        const auto source=name_argument_source(good);
        require(source.handle=="obj-7" && source.property_name=="StoredEntry","Точный источник не разобран");
        int callbacks=0;
        const auto copied=validated_name_argument(8,1,good,[&](const NameArgumentSource& selected){
            require(selected==source,"Capture получил другой источник");++callbacks;return uint64_t{0x100000007};
        });
        require(copied==0x100000007 && callbacks==1,"Полное 64-битное FName должно сохраняться");
        for(const auto& dims:std::vector<std::pair<int,int>>{{4,1},{16,1},{8,2},{8,0},{-8,1},{8,-1}}) {
            callbacks=0;
            rejects("unsupported_signature",[&]{validated_name_argument(dims.first,dims.second,good,[&](const auto&){++callbacks;return 0;});});
            require(callbacks==0,"Неверный target layout не должен читать источник");
        }
        const std::vector<json> invalid={nullptr,"spine_03",7,
            json{{"handle","obj-7"}},json{{"name_from",nullptr}},
            json{{"name_from",{{"handle","obj-7"}}}},
            json{{"name_from",{{"handle","obj-7"},{"property_name","StoredEntry"},{"bits",7}}}},
            json{{"name_from",{{"handle",7},{"property_name","StoredEntry"}}}},
            json{{"name_from",{{"handle","obj-7"},{"property_name",7}}}},
            json{{"name_from",{{"handle",""},{"property_name","StoredEntry"}}}},
            json{{"name_from",{{"handle","obj-7"},{"property_name",""}}}},
            json{{"name_from",{{"handle",std::string("obj\0x",5)},{"property_name","StoredEntry"}}}},
            json{{"name_from",{{"handle","obj-7"},{"property_name",std::string("a\0b",3)}}}},
            json{{"name_from",{{"handle","obj-7"},{"property_name","StoredEntry"}}},{"bits",7}}};
        for(const auto& value:invalid) {
            callbacks=0;
            rejects("invalid_name_argument",[&]{validated_name_argument(8,1,value,[&](const auto&){++callbacks;return 0;});});
            require(callbacks==0,"Неверный token не должен обращаться к игре");
        }
        for(const auto& lengths:std::vector<std::pair<int,int>>{{1,1},{80,256}}) {
            const NameArgumentSource boundary{std::string(lengths.first,'h'),std::string(lengths.second,'p')};
            callbacks=0;
            const auto selected=validated_name_argument(8,1,
                json{{"name_from",{{"handle",boundary.handle},{"property_name",boundary.property_name}}}},
                [&](const NameArgumentSource& current){++callbacks;return current;});
            require(selected==boundary && callbacks==1,"Включительные границы длины должны сохраняться без преобразований");
        }
        std::string unicode_handle,unicode_property;
        for(int i=0;i<128;++i){unicode_property+="\xD0\xAF";if(i<40)unicode_handle+="\xD0\xAF";}
        const json unicode={{"name_from",{{"handle",unicode_handle},{"property_name",unicode_property}}}};
        callbacks=0;
        const auto unicode_source=validated_name_argument(8,1,unicode,[&](const auto& current){++callbacks;return current;});
        require(unicode_source.handle.size()==80 && unicode_source.property_name.size()==256
            && unicode_source.handle==unicode_handle && unicode_source.property_name==unicode_property && callbacks==1,
            "Unicode на границах 80/256 байт должен сохраняться точно");
        callbacks=0;
        rejects("unsupported_signature",[&]{validated_name_argument(4,2,nullptr,[&](const auto&){++callbacks;return 0;});});
        require(callbacks==0,"Неверный layout должен проверяться раньше token и capture");
        callbacks=0;
        rejects("stale_handle",[&]{validated_name_argument(8,1,good,[&](const auto&)->int{
            ++callbacks;throw HandlerError("stale_handle","expired");
        });});
        require(callbacks==1,"Ошибка capture не должна повторно читать источник");
        const json capture_details={{"executed",false},{"may_have_executed",false},{"do_not_retry",true},{"source","serial_changed"}};
        callbacks=0;
        rejects("name_source_changed",[&]{validated_name_argument(8,1,good,[&](const auto&)->int{
            ++callbacks;throw HandlerError("name_source_changed","changed",capture_details);
        });},capture_details);
        require(callbacks==1,"Точные сведения capture должны сохраняться без повторного вызова");
        std::cout<<"Name arguments: "<<checks<<" проверок прошли\n";return 0;
    }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
