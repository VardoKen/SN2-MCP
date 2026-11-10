#include <g1r_mcp/as_exception_capture.hpp>
#include <Windows.h>

#include <iostream>
#include <stdexcept>
#include <thread>
#include <type_traits>

namespace {
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}

void record_text(const std::string& text) {
    const auto begin=reinterpret_cast<std::uintptr_t>(text.c_str());
    g1r_mcp::as_exception_capture::record(text.c_str(),[&](std::uintptr_t address,char& value) {
        if(address<begin || address>begin+text.size())return false;
        value=*reinterpret_cast<const char*>(address);return true;
    });
}

constexpr DWORD logger_fault_code=0xe1474343;
__declspec(noinline) void raise_logger_fault() {
    g1r_mcp::as_exception_capture::LoggerScope logger;
    RaiseException(logger_fault_code,0,0,nullptr);
}

// SEH caller не содержит объектов с C++ деструктором. При /EHsc исключение
// перехватывается здесь без unwinding LoggerScope в вызываемой функции.
__declspec(noinline) bool catch_logger_fault() noexcept {
    __try {raise_logger_fault();return false;}
    __except(GetExceptionCode()==logger_fault_code?EXCEPTION_EXECUTE_HANDLER:EXCEPTION_CONTINUE_SEARCH) {return true;}
}
}

int main() {
    using namespace g1r_mcp::as_exception_capture;
    static_assert(!std::is_copy_constructible_v<Scope> && !std::is_copy_assignable_v<Scope>);
    static_assert(!std::is_copy_constructible_v<LoggerScope> && !std::is_copy_assignable_v<LoggerScope>);
    try {
        int outside_reads{};
        record("outside",[&](std::uintptr_t,char&){++outside_reads;return false;});
        require(active==nullptr && outside_reads==0,"Logger вне ProcessEvent не должен читать и сохранять сообщения");
        State outer;
        {
            Scope scope(outer);
            record_text("outer first");
            State inner;
            {
                Scope nested(inner);
                record_text("inner");
                require(inner.exception_count==1 && inner.messages==std::vector<std::string>{"inner"},
                        "Вложенный ProcessEvent должен принадлежать собственной записи");
                require(outer.exception_count==1,"Вложенное сообщение не должно попадать во внешний вызов");
            }
            record_text("outer second");
            require(active==&outer && outer.messages==std::vector<std::string>{"outer first","outer second"},
                    "Завершение вложенного вызова должно возвращать прежнего владельца");
            bool isolated{};
            std::thread worker([&] {
                isolated=active==nullptr;
                record_text("foreign thread");
                State own;
                {Scope local(own);record_text("own thread");}
                isolated=isolated && own.exception_count==1 && own.messages==std::vector<std::string>{"own thread"} && active==nullptr;
            });
            worker.join();
            require(isolated && outer.exception_count==2,"Другой поток не должен изменять текущую запись ProcessEvent");
        }
        require(active==nullptr && outer.full_capture,"Завершение ProcessEvent должно убрать TLS владельца");
        {
            State state;
            {Scope scope(state);
                const std::string long_message(65536,'x');
                for(int i=0;i<65;++i)record_text(i==64?long_message:std::string("message ")+std::to_string(i));
            }
            require(state.exception_count==65 && state.messages.size()==65 && state.messages.back().size()==65536 && state.full_capture,
                    "Захват сообщений не должен ограничивать количество или длину");
        }
        {
            State state;
            {Scope scope(state);
                record(nullptr,[](std::uintptr_t,char&)->bool {throw std::runtime_error("null must not read");});
                record_text("");
                record_text(std::string("raw\xff",4));
            }
            require(state.messages==std::vector<std::string>{"NO EXCEPTION","",std::string("raw\xff",4)} && state.exception_count==3 && state.full_capture,
                    "Null, пустое сообщение и ANSI байты должны сохраняться без выдуманного текста или преобразования");
        }
        for(const bool throws:{false,true}) {
            State state;
            {Scope scope(state);
                const std::string text="partial message";
                const auto begin=reinterpret_cast<std::uintptr_t>(text.c_str());
                record(text.c_str(),[&](std::uintptr_t address,char& value) {
                    if(address==begin+3) {
                        if(throws)throw std::bad_alloc();
                        return false;
                    }
                    value=*reinterpret_cast<const char*>(address);return true;
                });
                record_text("next complete");
            }
            require(state.exception_count==2 && !state.full_capture && state.messages==std::vector<std::string>{"next complete"},
                    "Ошибка чтения или выделения памяти должна сохранить факт исключения и не публиковать частичное сообщение");
        }
        {
            State state;
            try {Scope scope(state);record_text("before C++ error");throw std::runtime_error("scope exit");}
            catch(const std::runtime_error&) {}
            require(active==nullptr && state.exception_count==1,"Выход с C++ ошибкой должен восстанавливать TLS владельца");
        }
        {
            State state;
            state.exception_count=3;
            state.messages={"ASCII exception","Ошибка скрипта",std::string("emoji \xf0\x9f\x98\x80",10)};
            const auto diagnostic=describe(state);
            const nlohmann::json expected={{"source","observed_logging_entry"},{"exception_count",3},
                {"full_capture",true},{"stack_captured",false},{"messages",state.messages}};
            require(diagnostic==expected && nlohmann::json::parse(diagnostic.dump())==expected,
                    "Корректный UTF-8 должен передаваться полностью без изменения и fabricated stack");
        }
        {
            State state;
            state.exception_count=4;state.full_capture=false;
            state.messages={"first",std::string("raw\xff",4),"third",std::string("\xc3\x28",2)};
            const auto original=state.messages;
            const auto diagnostic=describe(state);
            const nlohmann::json expected={{"source","observed_logging_entry"},{"exception_count",4},
                {"full_capture",false},{"stack_captured",false},
                {"messages",nlohmann::json::array({"first",nullptr,"third",nullptr})},
                {"message_bytes",nlohmann::json::array({{{"index",1},{"encoding","hex"},{"bytes","726177ff"}},
                    {{"index",3},{"encoding","hex"},{"bytes","c328"}}})}};
            require(diagnostic==expected && nlohmann::json::parse(diagnostic.dump())==expected && state.messages==original,
                    "Некорректные ANSI байты должны сохраняться точным hex с исходными индексами без замены и потери сообщений");
        }
        {
            State state;
            state.exception_count=2;state.full_capture=false;
            const auto diagnostic=describe(state);
            require(diagnostic.at("exception_count")==2 && !diagnostic.at("full_capture").get<bool>()
                    && diagnostic.at("messages").empty() && !diagnostic.contains("message_bytes"),
                    "Ошибка захвата должна сохранять факт исключений даже без скопированных сообщений");
        }
        {
            State state;
            record_stack({"outside ProcessEvent"},true);
            {Scope scope(state);
                record_stack({"outside logger"},true);
                require(state.stacks.empty() && state.exception_count==0,
                    "Formatter вне logger не должен создавать исключение или стек");
                {LoggerScope logger;
                    require(logger.owner()==&state && logger.exception_index()==0 && state.exception_count==1,
                        "Logger должен назначать индекс исключения до копирования сообщения");
                    record_text("message");
                    record_stack({"Ошибка в Module::Function",std::string("emoji \xf0\x9f\x98\x80",10)},true);
                }
                record_stack({"after logger"},true);
            }
            const auto diagnostic=describe(state);
            require(state.exception_count==1 && state.messages==std::vector<std::string>{"message"}
                && state.stacks.size()==1 && active_logger==nullptr,
                "Сообщение и formatter одного logger должны считаться одним исключением");
            require(diagnostic.at("stacks_source")=="observed_engine_formatter"
                && diagnostic.at("stack_captured").get<bool>()
                && diagnostic.at("stacks")==nlohmann::json::array({{{"exception_index",0},
                    {"frames",state.stacks[0].frames},{"complete",true}}})
                && nlohmann::json::parse(diagnostic.dump())==diagnostic,
                "Описание должно публиковать скопированные UTF-8 кадры и фактический источник");
        }
        for(const bool throws:{false,true}) {
            State state;
            {Scope scope(state);
                {LoggerScope logger;
                    record("unreadable",[&](std::uintptr_t,char&)->bool {
                        if(throws)throw std::bad_alloc();
                        return false;
                    });
                    record_stack({"first exception frame"},true);
                }
                {LoggerScope logger;
                    record_text("second message");
                    record_stack({"second exception frame"},false);
                }
            }
            require(state.exception_count==2 && !state.full_capture
                && state.messages==std::vector<std::string>{"second message"}
                && state.stacks.size()==2 && state.stacks[0].exception_index==0
                && state.stacks[1].exception_index==1,
                "Ошибка сообщения не должна смещать индексы последующих стеков");
            const auto diagnostic=describe(state);
            require(diagnostic.at("stack_captured").get<bool>()
                && diagnostic.at("stacks")[0].at("complete").get<bool>()
                && !diagnostic.at("stacks")[1].at("complete").get<bool>(),
                "Полнота каждого стека должна сохраняться независимо от результата сообщения");
        }
        {
            State nested_outer,nested_inner;
            {Scope scope(nested_outer);
                LoggerScope logger;
                record_text("outer");
                {Scope nested(nested_inner);
                    record_stack({"unrelated formatter in inner ProcessEvent"},true);
                    require(nested_inner.stacks.empty() && nested_outer.stacks.empty(),
                        "Вложенный ProcessEvent без своего logger не должен забирать внешний стек");
                    {LoggerScope nested_logger;
                        record_text("inner");
                        record_stack({"inner frame"},true);
                    }
                require(active_logger==nullptr,
                    "Вложенный logger должен возвращать пустую TLS область вложенного ProcessEvent");
                }
                record_stack({"outer frame"},true);
            }
            require(nested_outer.exception_count==1 && nested_inner.exception_count==1
                && nested_outer.stacks.size()==1 && nested_inner.stacks.size()==1
                && nested_outer.stacks[0].frames==std::vector<std::string>{"outer frame"}
                && nested_inner.stacks[0].frames==std::vector<std::string>{"inner frame"}
                && active==nullptr && active_logger==nullptr,
                "Вложенные области должны сохранять независимые сообщения и стеки");
        }
        {
            State state;
            {Scope scope(state);
                LoggerScope first;
                {LoggerScope second;record_stack({"nested same owner"},true);}
                record_stack({"first owner restored"},true);
            }
            require(state.exception_count==2 && state.stacks.size()==2
                && state.stacks[0].exception_index==1 && state.stacks[1].exception_index==0,
                "Вложенные logger одного ProcessEvent должны восстанавливать свой индекс");
        }
        {
            State state;
            {LoggerScope outside;
                require(outside.owner()==nullptr,"Logger вне ProcessEvent не должен захватывать владельца");
                {Scope scope(state);record_stack({"unrelated ProcessEvent"},true);}
            }
            require(state.exception_count==0 && state.stacks.empty() && active_logger==nullptr,
                "ProcessEvent не должен наследовать logger, начатый вне него");
        }
        for(const bool complete:{false,true}) {
            State state;
            {Scope scope(state);LoggerScope logger;record_stack({},complete);}
            const auto diagnostic=describe(state);
            require(state.exception_count==1 && state.stacks.size()==1
                && diagnostic.at("stack_captured").get<bool>()==complete
                && diagnostic.at("stacks")[0].at("frames").empty()
                && diagnostic.at("stacks")[0].at("complete").get<bool>()==complete,
                "Подтверждённый пустой стек должен отличаться от нечитаемого результата formatter");
        }
        {
            State state;
            {Scope scope(state);LoggerScope logger;
                std::vector<std::string> copied_frames(65,std::string(65536,'s'));
                record_stack(copied_frames,true);
                copied_frames[0]="changed after capture";
                require(state.stacks.size()==1 && state.stacks[0].frames.size()==65
                    && state.stacks[0].frames.front().size()==65536 && state.stacks[0].frames.back().size()==65536,
                    "Кадры должны храниться собственными строками без ограничения числа или длины");
            }
        }
        {
            State state;
            {Scope scope(state);LoggerScope logger;
                bool isolated{};
                std::thread worker([&] {
                    isolated=active==nullptr && active_logger==nullptr;
                    record_stack({"foreign thread"},true);
                    State local;
                    {Scope local_scope(local);LoggerScope local_logger;record_stack({"local thread"},true);}
                    isolated=isolated && local.exception_count==1 && local.stacks.size()==1
                        && active==nullptr && active_logger==nullptr;
                });
                worker.join();
                require(isolated && state.exception_count==1 && state.stacks.empty(),
                    "Formatter другого потока должен оставаться у собственного TLS владельца");
            }
        }
        {
            State state;
            try {
                Scope scope(state);LoggerScope logger;
                record_stack({"before C++ error"},false);
                throw std::runtime_error("logger exit");
            } catch(const std::runtime_error&) {}
            require(active==nullptr && active_logger==nullptr && state.exception_count==1
                && state.stacks.size()==1 && !describe(state).at("stack_captured").get<bool>(),
                "Выход с C++ ошибкой должен восстанавливать обе TLS области и сохранять частичный стек");
        }
        {
            State state;
            {Scope scope(state);LoggerScope logger;
                {Scope nested(state);
                    record_stack({"same State without own logger"},true);
                    require(active==&state && active_logger==nullptr && state.stacks.empty()
                        && state.exception_count==1,
                        "Вложенный ProcessEvent с тем же State должен изолировать formatter от внешнего logger");
                    {LoggerScope nested_logger;record_stack({"same State inner"},true);}
                }
                require(active_logger==&logger && state.stacks.size()==1
                    && state.stacks[0].exception_index==1,
                    "После вложенного ProcessEvent с тем же State должен восстанавливаться внешний logger");
                record_stack({"same State outer"},true);
            }
            require(state.exception_count==2 && state.stacks.size()==2
                && state.stacks[1].exception_index==0 && active==nullptr && active_logger==nullptr,
                "Совпадение State не должно смешивать индексы вложенных logger");
        }
        {
            State state;
            {Scope scope(state);
                const bool caught=catch_logger_fault();
                require(caught && active==&state && state.exception_count==1 && active_logger!=nullptr,
                    "Реальный native SEH при /EHsc должен воспроизводить пропущенный деструктор LoggerScope");
            }
            require(active==nullptr && active_logger==nullptr,
                "Внешняя Scope должна очистить TLS logger после перехваченного native SEH");
            record_stack({"after SEH"},true);
            require(state.stacks.empty(),"Formatter после native SEH не должен обращаться к ушедшему logger");
        }
        {
            State outer_state,inner_state;
            {Scope scope(outer_state);LoggerScope logger;
                {Scope nested(inner_state);
                    require(catch_logger_fault() && inner_state.exception_count==1,
                        "Native SEH вложенного ProcessEvent должен сохранять его факт исключения");
                }
                require(active==&outer_state && active_logger==&logger,
                    "После native SEH вложенного ProcessEvent должен восстанавливаться живой внешний logger");
                record_stack({"outer after inner SEH"},true);
            }
            require(outer_state.exception_count==1 && outer_state.stacks.size()==1
                && outer_state.stacks[0].exception_index==0 && inner_state.stacks.empty()
                && active==nullptr && active_logger==nullptr,
                "Стек внешнего logger после вложенного native SEH должен сохранять правильного владельца");
        }
        std::cout<<"as_exception_capture: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
