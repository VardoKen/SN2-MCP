#include <g1r_mcp/function_contract.hpp>
#include <iostream>
#include <stdexcept>
#include <vector>
namespace {
int checks{};
void require(bool good,const char* message){++checks;if(!good)throw std::runtime_error(message);}
template<class Action>void reject(const char* code,Action&& action) {
    try {action();}catch(const g1r_mcp::HandlerError& error) {
        require(error.code()==code,"Неверный код отказа сигнатуры");
        require(error.details()==nlohmann::json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},"Отказ должен быть до исполнения");return;
    }
    throw std::runtime_error("Ожидался отказ сигнатуры");
}
}
int main() {
    using namespace g1r_mcp;using nlohmann::json;
    try {
        std::map<std::string,json> saved;std::uint64_t sequence{};
        const json base={{"session","native-1"},{"target_handle","obj-1"},{"function_name","ReadMarker"},
            {"target",{{"serial",0},{"class_serial",0},{"name",9}}},
            {"function",{{"pointer",123},{"serial",0},{"class_serial",0},{"flags",1}}},
            {"owner",{{"pointer",456},{"serial",0},{"class_serial",0}}},
            {"header",{{"parameter_count",1},{"parameter_size",4},{"return_offset",0},{"child_head",789}}},
            {"fields",json::array({{{"pointer",789},{"next",std::uint64_t{0}},{"type","IntProperty"},
                                   {"flags",std::uint64_t{0x580}},{"name",12},{"offset",0},{"size",4},{"array_dim",1}}})}};
        const auto first=store_function_contract(saved,sequence,base);
        require(first=="sig-1","Токен последовательности неверен");
        require(store_function_contract(saved,sequence,base)==first && sequence==1 && saved.size()==1,"Опрос не должен исчерпывать токены");
        require(checked_function_contract(saved,first,"native-1","obj-1","ReadMarker")==base,"Токен не вернул полную запись");
        for(const json& invalid:std::vector<json>{nullptr,true,7,"","sig-","sig-0","sig-01","sig-+1","sig--1","SIG-1","sig-1x",std::string("sig-1\0x",7),"sig-123456789012345678901"})
            reject("invalid_signature_token",[&]{checked_function_contract(saved,invalid,"native-1","obj-1","ReadMarker");});
        reject("unknown_signature_token",[&]{checked_function_contract(saved,"sig-2","native-1","obj-1","ReadMarker");});
        for(const auto& input:std::vector<std::vector<std::string>>{{"native-2","obj-1","ReadMarker"},{"native-1","obj-2","ReadMarker"},{"native-1","obj-1","Other"}})
            reject("signature_token_mismatch",[&]{checked_function_contract(saved,first,input[0],input[1],input[2]);});
        require_same_function_contract(base,base);
        for(const char* path:{"/target/serial","/target/class_serial","/function/pointer","/function/serial","/function/flags","/owner/pointer","/owner/serial","/header/parameter_count","/header/parameter_size","/header/return_offset","/header/child_head","/fields/0/pointer","/fields/0/next","/fields/0/flags","/fields/0/name","/fields/0/offset","/fields/0/size","/fields/0/array_dim"}) {
            auto changed=base;auto& value=changed[json::json_pointer(path)];value=value.get<std::uint64_t>()+1;
            reject("signature_changed",[&]{require_same_function_contract(base,changed);});
        }
        auto type_changed=base;type_changed["fields"][0]["type"]="FloatProperty";
        reject("signature_changed",[&]{require_same_function_contract(base,type_changed);});
        auto auxiliary=base;auxiliary["fields"][0]["aux"]={{"mask",1},{"class",55},{"enum_names",json::array({{{"name",1},{"value",2}}})}};
        for(const char* path:{"/fields/0/aux/mask","/fields/0/aux/class","/fields/0/aux/enum_names/0/value"}) {
            auto changed=auxiliary;changed[json::json_pointer(path)]=3;
            reject("signature_changed",[&]{require_same_function_contract(auxiliary,changed);});
        }
        saved.clear();auto new_session=base;new_session["session"]="native-2";
        require(store_function_contract(saved,sequence,new_session)=="sig-2","Токены нельзя повторять после disconnect");
        reject("unknown_signature_token",[&]{checked_function_contract(saved,first,"native-2","obj-1","ReadMarker");});
        for(int i=0;i<255;++i){auto entry=base;entry["function_name"]="Other"+std::to_string(i);store_function_contract(saved,sequence,entry);}
        auto extra=base;extra["function_name"]="Overflow";
        const auto extra_token=store_function_contract(saved,sequence,extra);
        require(saved.size()==257 && extra_token=="sig-258","257-й контракт должен получить новый токен без отзыва прежних");
        require(checked_function_contract(saved,"sig-2","native-2","obj-1","ReadMarker")==new_session,
                "Первый контракт сессии должен оставаться доступным после 257-го");
        require(store_function_contract(saved,sequence,extra)==extra_token && saved.size()==257 && sequence==258,
                "Повторный доступ после 256 контрактов не должен расходовать номер");
        auto oversized=base;oversized["function_name"]="LargeContract";oversized["message"]=std::string(65537,'x');
        const auto oversized_bytes=oversized.dump();
        const auto oversized_token=store_function_contract(saved,sequence,oversized);
        require(oversized_bytes.size()>65536 && checked_function_contract(saved,oversized_token,"native-1","obj-1","LargeContract").dump()==oversized_bytes,
                "Контракт больше 64 КиБ должен сохранять полную сериализацию");
        const auto chain=capture_function_field_chain(1,[](std::uintptr_t pointer){return json{{"pointer",pointer},{"next",pointer<129?pointer+1:std::uintptr_t{0}},{"flags",pointer==1?0:0x80}};});
        require(chain.size()==129 && chain[0]["flags"]==0 && chain.back()["pointer"]==129 && chain.back()["next"]==std::uintptr_t{0},
                "129 полей включая не-Parm должны сохраняться до точного конца цепочки");
        reject("unsupported_signature",[&]{capture_function_field_chain(1,[](std::uintptr_t){return json{{"next",std::uintptr_t{1}}};});});
        require(capture_function_field_chain(0,[](std::uintptr_t){return json{};}).empty(),"Пустая цепь допустима");
        const json class_aux={{"declared",{{"object",std::uint64_t{0x12345678}},{"name",99}}},
            {"declared_type",{{"name","Class"},{"path","/Script/CoreUObject.Class"},{"object",std::uint64_t{0x12345678}}}},
            {"meta",{{"object",std::uint64_t{0x87654321}},{"name",100}}},
            {"meta_type",{{"name","GothicCharacter"},{"path","/Script/G1R.GothicCharacter"},{"object",std::uint64_t{0x87654321}}}}};
        const auto class_copy=class_aux;
        const json declared_type={{"name","Class"},{"path","/Script/CoreUObject.Class"}};
        const json meta_class={{"name","GothicCharacter"},{"path","/Script/G1R.GothicCharacter"}};
        require(public_parameter_type_metadata("ObjectProperty",class_aux)==json{{"declared_type",declared_type}},
                "UObject должен публиковать только имя и путь объявленного класса");
        require(public_parameter_type_metadata("ClassProperty",class_aux)==json{{"declared_type",declared_type},{"meta_class",meta_class}},
                "UClass должен публиковать объявленный класс и MetaClass без raw identity");
        require(class_aux==class_copy,"Публичная проекция не должна менять внутренний контракт класса");
        const json enum_aux={{"underlying_type","UInt64Property"},{"size",8},
            {"enum",{{"identity",{{"object",std::uint64_t{0xabcdef12}},{"name",101}}},
                {"path","/Script/Angelscript.EProbe"},{"data",std::uint64_t{0x11112222}},{"count",3},{"capacity",8},
                {"entries",json::array({{{"name",std::uint64_t{102}},{"label","EProbe::First"},{"value",std::uint64_t{0}}},
                    {{"name",std::uint64_t{103}},{"label","EProbe::Alias"},{"value",std::uint64_t{0}}},
                    {{"name",std::uint64_t{104}},{"label","EProbe::Largest"},{"value",std::numeric_limits<std::uint64_t>::max()}}})}}}};
        const auto enum_copy=enum_aux;
        const json enumeration={{"path","/Script/Angelscript.EProbe"},{"underlying_type","UInt64Property"},
            {"entries",json::array({{{"name","EProbe::First"},{"value",std::uint64_t{0}}},
                {{"name","EProbe::Alias"},{"value",std::uint64_t{0}}},
                {{"name","EProbe::Largest"},{"value",std::numeric_limits<std::uint64_t>::max()}}})}};
        const auto enum_public=public_parameter_type_metadata("EnumProperty",enum_aux);
        require(enum_public==json{{"enum",enumeration}},"Enum должен сохранять все подписи, aliases и точные значения");
        require(enum_public.at("enum").at("entries")[2].at("value").is_number_unsigned(),"UInt64 enum не должен сужаться до int64");
        require(public_parameter_type_metadata("ByteProperty",enum_aux)==enum_public,"Byte enum должен публиковать ту же таблицу");
        require(enum_aux==enum_copy,"Публичная проекция не должна менять внутренний enum контракт");
        const json plain_byte={{"underlying_type","ByteProperty"},{"size",1},{"enum",nullptr}};
        require(public_parameter_type_metadata("ByteProperty",plain_byte)==json{{"enum",{{"path",nullptr},
                    {"underlying_type","ByteProperty"},{"entries",json::array()}}}},"Обычный ByteProperty должен явно показывать отсутствие enum");
        require(public_parameter_type_metadata("IntProperty",json::object()).empty(),"Скалярный параметр не должен получать чужую type metadata");
        auto typed_contract=base;
        typed_contract["fields"][0]["aux"]=enum_aux;
        typed_contract["public_function"]={{"parameters",json::array({enum_public})}};
        std::map<std::string,json> typed_saved;std::uint64_t typed_sequence{};
        const auto typed_token=store_function_contract(typed_saved,typed_sequence,typed_contract);
        require(store_function_contract(typed_saved,typed_sequence,typed_contract)==typed_token && typed_sequence==1,
                "Публичная metadata не должна менять токен неизменного полного контракта");
        for(const char* path:{"/fields/0/aux/enum/path","/fields/0/aux/enum/entries/0/label"}) {
            auto changed=typed_contract;changed[json::json_pointer(path)]="changed";
            reject("signature_changed",[&]{require_same_function_contract(typed_contract,changed);});
        }
        auto changed_class=base;changed_class["fields"][0]["aux"]=class_aux;
        auto moved_class=changed_class;moved_class["fields"][0]["aux"]["declared_type"]["path"]="/Script/Other.Class";
        reject("signature_changed",[&]{require_same_function_contract(changed_class,moved_class);});
        std::cout<<"function_contract: "<<checks<<" checks passed\n";return 0;
    }catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
