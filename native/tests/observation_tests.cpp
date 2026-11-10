#include <g1r_mcp/observations.hpp>
#include <g1r_mcp/transport.hpp>
#include <g1r_mcp/scalar_arguments.hpp>
#include <g1r_mcp/object_arguments.hpp>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using nlohmann::json;
using g1r_mcp::Observations;
namespace {
void require(bool value, const char* message) {
    if (!value) throw std::runtime_error(message);
}
template<class Operation> void error(const char* code, Operation operation) {
    try { operation(); }
    catch (const g1r_mcp::HandlerError& caught) {
        require(caught.code() == code, "Неожиданный код отказа");
        return;
    }
    throw std::runtime_error("Ожидался отказ");
}
json params(int duration=100, int capacity=4) {
    return {{"handle","obj-1"},{"properties",{"Health"}},
            {"duration_ms",duration},{"interval_ms",10},{"capacity",capacity}};
}
json snapshot(int value) {
    return {{"snapshot_id","snapshot-1"},{"session_id","owner"},{"partial",false},{"truncated",false},
            {"objects",json::array({{{"handle","obj-1"},{"path","World.Actor"},{"class","Actor"},
               {"properties",json::array({{{"name","Health"},{"type","IntProperty"},
                                          {"supported",true},{"value",value}}})}}})}};
}
json query(const std::string& id, int after=0, int limit=100) {
    return {{"watch_id",id},{"after_seq",after},{"limit",limit}};
}

void changes_limits_and_stop() {
    Observations model;
    auto start=model.start("owner",params(100,2),snapshot(10),0,1);
    const auto id=start.at("watch_id").get<std::string>();
    require(start.at("start_seq")==1 && start.at("state")=="running", "Неверный старт");
    int calls=0;
    model.pump(9,2,[&](const json&){++calls;return snapshot(20);});
    require(calls==0,"Чтение до интервала");
    model.pump(10,3,[&](const json&){++calls;return snapshot(10);});
    require(calls==1,"Не выполнено due чтение");
    require(model.read("owner",query(id)).at("events").size()==1,"Неизменённое значение создало событие");
    model.pump(20,4,[&](const json&){++calls;return snapshot(20);});
    model.pump(30,5,[&](const json&){++calls;return snapshot(30);});
    auto events=model.read("owner",query(id));
    require(events.at("dropped_events")==1 && events.at("gap")==true,"Потеря baseline не сообщена");
    require(events.at("events").size()==2 && events.at("events")[0].at("seq")==2,"Неверный ring буфер");
    auto paged=model.read("owner",query(id,0,1));
    require(paged.at("truncated")==true && paged.at("cursor")==2,"Пагинация потеряла cursor");
    require(model.read("owner",query(id)).at("events").size()==2,"Чтение неожиданно опустошило буфер");
    require(model.read("other",query(id))==events,"Another client could not read the existing watch");
    error("watch_running",[&]{model.forget("other",query(id));});
    const auto stopped=model.stop("other",query(id),31,6);
    require(stopped.at("cleanup_complete")==true && stopped.at("state")=="stopped","Остановка не подтверждена");
    const auto last=stopped.at("last_seq");
    model.stop("owner",query(id),32,7);
    require(model.read("owner",query(id)).at("last_seq")==last,"Повтор stop создал второе terminal");
    model.pump(90,8,[&](const json&){++calls;return snapshot(99);});
    require(calls==3,"Чтение после stop");
    require(model.forget("another-client",query(id)).at("forgotten")==true && model.size()==0,"Another client could not forget the stopped watch");
    error("unknown_watch",[&]{model.read("owner",query(id));});
}

void expiry_invalidation_and_disconnect() {
    Observations model;
    const auto id=model.start("owner",params(10),snapshot(1),0,1).at("watch_id").get<std::string>();
    int calls=0;
    model.pump(10,2,[&](const json&){++calls;return snapshot(2);});
    require(calls==0 && model.read("owner",query(id)).at("state")=="completed","Expiry прочитал уже законченную работу");
    model.pump(20,3,[&](const json&){++calls;return snapshot(2);});
    require(calls==0,"Чтение после expiry");
    const auto id2=model.start("owner",params(),snapshot(1),20,4).at("watch_id").get<std::string>();
    model.pump(30,5,[&](const json&){++calls;auto copy=snapshot(1);copy["partial"]=true;return copy;});
    require(model.read("owner",query(id2)).at("state")=="failed","Invalidation не завершила работу");
    model.pump(50,6,[&](const json&){++calls;return snapshot(3);});
    require(calls==1,"Чтение после invalidation");
    model.start("other",params(),snapshot(1),50,7);
    model.disconnect("owner");
    require(model.size()==1,"Disconnect затронул чужую работу или оставил свои записи");
    error("unknown_watch",[&]{model.read("owner",query(id2));});
    model.disconnect("other");
    model.pump(80,8,[&](const json&){++calls;return snapshot(3);});
    require(calls==1 && model.size()==0,"Disconnect оставил callback");
}

void shared_connection_client_ownership() {
    Observations model;
    const auto a=model.start("connection-1","client-a",params(),snapshot(1),0,1);
    const auto b=model.start("connection-1","client-b",params(),snapshot(1),0,1);
    const auto other=model.start("connection-2","client-a",params(),snapshot(1),0,1);
    const auto a_id=a.at("watch_id").get<std::string>();
    const auto b_id=b.at("watch_id").get<std::string>();
    const auto other_id=other.at("watch_id").get<std::string>();
    require(a.at("session_id")=="connection-1" && b.at("session_id")=="connection-1"
            && other.at("session_id")=="connection-2","Наблюдение должно публиковать native connection session");
    require(model.read("client-a",query(a_id)).at("events").size()==1
            && model.read("client-b",query(b_id)).at("events").size()==1,"Клиенты не прочитали собственные baseline");
    require(model.read("client-b",query(a_id)).at("client_owner")=="client-a"
            && model.read("connection-1",query(a_id)).at("events").size()==1,
            "Client metadata must not prevent reading a watch by id");
    error("watch_running",[&]{model.forget("client-b",query(a_id));});
    require(model.stop("client-b",query(a_id),1,2).at("state")=="stopped","Another client could not stop the watch");
    require(model.read("client-b",query(b_id)).at("state")=="running","Остановка клиента A затронула клиента B");
    require(model.read("client-a",query(a_id)).at("state")=="stopped",
            "The original client could not read a watch stopped by another client");
    model.disconnect("client-a");
    require(model.size()==3,"Disconnect должен проверять connection session, а не владельца клиента");
    model.disconnect("connection-1");
    require(model.size()==1,"Disconnect не очистил running и terminal наблюдения всех клиентов соединения");
    error("unknown_watch",[&]{model.read("client-a",query(a_id));});
    error("unknown_watch",[&]{model.read("client-b",query(b_id));});
    require(model.read("client-a",query(other_id)).at("state")=="running","Disconnect затронул другое соединение с тем же клиентом");
    int calls=0;
    model.pump(10,3,[&](const json&){++calls;return snapshot(2);});
    require(calls==1 && model.read("client-a",query(other_id)).at("events").size()==2,
            "После disconnect sampling должен продолжаться только для другого соединения");
    model.disconnect("connection-2");
    model.pump(20,4,[&](const json&){++calls;return snapshot(3);});
    require(calls==1 && model.size()==0,"Disconnect последнего соединения оставил sampling");
    const auto legacy=model.start("legacy-session",params(),snapshot(1),20,5);
    const auto legacy_id=legacy.at("watch_id").get<std::string>();
    require(legacy.at("session_id")=="legacy-session"
            && model.read("legacy-session",query(legacy_id)).at("state")=="running","Legacy owner=session изменился");
    model.stop("another-client",query(legacy_id),21,6);
    require(model.forget("",query(legacy_id)).at("forgotten")==true && model.size()==0,
            "Legacy watches must allow stop and forget by any client");
    const auto anonymous=model.start("connection-1","",params(),snapshot(1),0,1);
    require(anonymous.at("client_owner")=="" && anonymous.at("session_id")=="connection-1",
            "Optional client metadata must not prevent creating a watch");
    model.disconnect("connection-1");
}

void list_and_resume_without_restarting() {
    Observations model;
    model.start("connection-1","client-a",params(100,4),snapshot(10),5,1);
    const auto other=model.start("connection-1","client-other",params(100,4),snapshot(20),5,1);
    model.start("connection-2","client-a",params(),snapshot(30),5,1);
    const auto listed=model.list("connection-1","client-b");
    require(listed.at("session_id")=="connection-1" && listed.at("current_client_owner")=="client-b"
            && listed.at("items").size()==2,"List не восстановил записи после потерянного ответа start");
    const auto recovered=listed.at("items")[0];
    const auto id=recovered.at("watch_id").get<std::string>();
    const auto other_id=other.at("watch_id").get<std::string>();
    require(recovered.at("client_owner")=="client-a" && recovered.at("started_ms")==5,
            "List должен сообщать текущего владельца и исходное время старта");
    for(const auto& item:listed.at("items"))
        require(!item.contains("events") && !item.contains("targets") && !item.contains("snapshot") && !item.contains("value"),
                "List не должен публиковать наблюдаемые значения или события");
    json claim={{"watch_id",id},{"expected_session_id","connection-1"},{"expected_client_owner","client-a"}};
    const auto before=model.read("client-a",query(id));
    const auto other_before=model.read("client-other",query(other_id));
    const auto resumed=model.resume("connection-1","client-b",claim);
    auto expected=before;expected["client_owner"]="client-b";
    require(resumed.at("state")=="running" && resumed.at("client_owner")=="client-b"
            && model.read("client-b",query(id))==expected,"Resume изменил события или состояние running наблюдения");
    require(model.read("client-other",query(other_id))==other_before && model.size()==3,
            "Resume затронул другую запись или создал новую");
    require(model.read("client-a",query(id))==expected,
            "Updating client metadata must not revoke access from another client");
    error("watch_running",[&]{model.forget("client-a",query(id));});
    const auto after_resume=model.read("client-b",query(id));
    require(model.resume("connection-1","client-b",claim)==resumed,
            "Stale expected client metadata must not prevent repeating resume");
    require(model.list("connection-1","client-b").at("items")[0].at("client_owner")=="client-b"
            && model.read("client-b",query(id))==after_resume,"Неопределённый результат resume не виден в list или повтор мутирует запись");
    auto wrong_session=claim;wrong_session["expected_session_id"]="connection-2";
    error("watch_resume_mismatch",[&]{model.resume("connection-1","client-c",wrong_session);});
    auto current_claim=claim;current_claim["expected_client_owner"]="client-b";
    error("watch_resume_mismatch",[&]{model.resume("connection-2","client-c",current_claim);});
    auto malformed=current_claim;malformed.erase("expected_client_owner");
    require(model.resume("connection-1","client-b",malformed)==resumed,
            "Resume must not require expected client metadata");
    malformed["expected_client_owner"]=3;
    require(model.resume("connection-1","client-b",malformed)==resumed,
            "Legacy expected client metadata must not act as authorization");
    malformed=current_claim;malformed["expected_session_id"]=3;
    error("invalid_watch_params",[&]{model.resume("connection-1","client-c",malformed);});
    const auto anonymous=model.resume("connection-1","",current_claim);
    expected=after_resume;expected["client_owner"]="";
    require(anonymous.at("client_owner")=="" && model.read("client-a",query(id))==expected,
            "Optional client metadata must not prevent resuming a watch");
    require(model.resume("connection-1","client-b",{{"watch_id",id}})==resumed,
            "Resume by watch id must preserve the current native connection lifecycle");
    require(model.read("client-b",query(id))==after_resume,"Отказ resume изменил запись");
    int calls=0;
    model.pump(14,2,[&](const json&){++calls;return snapshot(11);});
    require(calls==0,"Resume изменил следующий интервал sampling");
    model.pump(15,3,[&](const json&){++calls;return snapshot(11);});
    require(calls==3 && model.read("client-b",query(id)).at("last_seq")==2,"Sampling после передачи владения перезапущен");
    model.pump(105,4,[&](const json&){++calls;return snapshot(12);});
    model.pump(105,5,[&](const json&){++calls;return snapshot(12);});
    model.pump(105,6,[&](const json&){++calls;return snapshot(12);});
    require(calls==3 && model.read("client-b",query(id)).at("state")=="completed",
            "Resume изменил исходный deadline");
    const auto terminal_before=model.read("client-b",query(id));
    const auto terminal=model.resume("connection-1","client-c",current_claim);
    expected=terminal_before;expected["client_owner"]="client-c";
    require(terminal.at("state")=="completed" && model.read("client-c",query(id))==expected,
            "Передача terminal записи изменила события или terminal состояние");
    require(model.read("client-b",query(id))==expected,
            "The previous client could not read a terminal watch after a metadata update");
    require(model.forget("client-b",query(id)).at("forgotten")==true,
            "The previous client could not forget the terminal watch");
    auto final_claim=current_claim;final_claim["expected_client_owner"]="client-c";
    model.disconnect("connection-1");
    require(model.list("connection-1","client-c").at("items").empty() && model.size()==1,
            "List вернул записи после disconnect или disconnect затронул другую TCP-сессию");
    error("unknown_watch",[&]{model.resume("connection-1","client-d",final_claim);});
    require(model.list("connection-2","client-a").at("items").size()==1,"Disconnect потерял другое соединение");
    model.disconnect("connection-2");
}

void two_watches_and_scheduling() {
    Observations model;
    auto one=model.start("owner",params(100,128),snapshot(1),0,1).at("watch_id").get<std::string>();
    auto two=model.start("owner",params(100,128),snapshot(1),0,1).at("watch_id").get<std::string>();
    int calls=0;
    model.pump(10,2,[&](const json&){++calls;return snapshot(2);});
    require(calls==2,"Не все due работы получили чтение в одном Tick");
    model.pump(11,3,[&](const json&){++calls;return snapshot(3);});
    require(calls==2,"Повторный Tick до интервала выполнил лишнее чтение");
    model.stop("owner",query(one),12,4);
    model.pump(21,5,[&](const json&){++calls;return snapshot(4);});
    require(calls==3 && model.read("owner",query(two)).at("state")=="running","Stop одной работы затронул другую");
    model.pump(31,6,[&](const json&){++calls;return snapshot(5);});
    require(model.read("owner",query(two)).at("state")=="running","Чтение второй работы завершило наблюдение");
    model.pump(90,7,[&](const json&){++calls;return snapshot(9);});
    require(calls==5,"Активная работа перестала получать чтения");
}

void invalid_parameters_and_capacity() {
    Observations model;
    auto bad=params();bad["interval_ms"]=0;
    error("invalid_watch_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    bad=params();bad["duration_ms"]=true;
    error("invalid_watch_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    bad=params();bad["properties"]={"Health","Health"};
    error("invalid_snapshot_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    auto incomplete=snapshot(1);incomplete["partial"]=true;
    error("unsupported_watch_target",[&]{model.start("owner",params(),incomplete,0,1);});
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array());});
    auto target=json{{"handle","obj-1"},{"properties",{"Health"}}};
    auto extra=target;extra["unknown"]=true;
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array({extra}));});
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array({target,target}));});
    auto long_handle=target;long_handle["handle"]=std::string(81,'x');
    long_handle["properties"]={std::string(257,'p')};
    Observations::validate_targets(json::array({long_handle}));
    auto empty=target;empty["handle"]="";
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array({empty}));});
    empty=target;empty["properties"]={""};
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array({empty}));});
    auto non_string=target;non_string["properties"]={1};
    error("invalid_snapshot_params",[&]{Observations::validate_targets(json::array({non_string}));});
    bad=params();bad["duration_ms"]=0;
    error("invalid_watch_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    bad=params();bad["capacity"]=0;
    error("invalid_watch_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    bad=params();bad["capacity"]=-1;
    error("invalid_watch_params",[&]{model.start("owner",bad,snapshot(1),0,1);});
    const auto id=model.start("owner",params(),snapshot(1),0,1).at("watch_id").get<std::string>();
    error("invalid_watch_params",[&]{model.read("owner",query(id,99));});
    error("invalid_watch_params",[&]{model.read("owner",query(id,0,0));});
    error("unknown_watch",[&]{model.read("owner",query(std::string(81,'x')));});
    error("invalid_watch_params",[&]{model.read("owner",query(""));});
    model.disconnect("owner");
    require(model.size()==0,"Не очищены записи после проверки параметров");
}

void targets_and_watch_counts_follow_the_request() {
    json targets=json::array();
    for(int object=0;object<17;++object) {
        json properties=json::array();
        for(int property=0;property<33;++property)properties.push_back("Property"+std::to_string(property));
        targets.push_back({{"handle","obj-"+std::to_string(object)},{"properties",std::move(properties)}});
    }
    Observations::validate_targets(targets);
    auto duplicate=targets;duplicate[16]=duplicate[0];
    error("invalid_snapshot_params",[&]{Observations::validate_targets(duplicate);});
    duplicate=targets;duplicate[16]["properties"][32]=duplicate[16]["properties"][0];
    error("invalid_snapshot_params",[&]{Observations::validate_targets(duplicate);});
    Observations model;
    std::vector<std::string> ids;
    for(int i=0;i<9;++i)ids.push_back(model.start("owner",params(),snapshot(i),0,1).at("watch_id").get<std::string>());
    require(model.size()==9 && model.list("owner","owner").at("items").size()==9,"Число наблюдений ограничено прежним пределом");
    int calls=0;
    for(int i=0;i<9;++i)model.pump(10,i+2,[&](const json&){++calls;return snapshot(20);});
    require(calls==9,"Не все запрошенные наблюдения получили due чтение");
    for(const auto& id:ids)require(model.read("owner",query(id)).at("last_seq")==2,"Потеряно событие одного из наблюдений");
    model.disconnect("owner");
    require(model.size()==0,"Disconnect оставил записи сверх прежнего предела");
}

void copied_large_values_and_no_catchup() {
    Observations model;
    auto baseline=snapshot(1);
    baseline["objects"][0]["properties"][0]["value"]=std::string(300000,'a');
    require(baseline.dump().size()>256*1024,"Fixture не пересёк прежний предел снимка");
    const auto id=model.start("owner",params(1000,4),baseline,0,1).at("watch_id").get<std::string>();
    baseline["objects"][0]["properties"][0]["value"]=nullptr;
    require(model.read("owner",query(id)).at("events")[0].at("snapshot").at("objects")[0]
              .at("properties")[0].at("value")==std::string(300000,'a'),"Большой baseline не является независимой полной копией");
    int calls=0;
    for(int at=100;at<=200;at+=100) {
        model.pump(at,at,[&](const json&) {
            ++calls;auto data=snapshot(1);
            data["objects"][0]["properties"][0]["value"]=std::string(300000,static_cast<char>('a'+calls));
            return data;
        });
        model.pump(at,at,[&](const json&){++calls;return snapshot(999);});
    }
    require(calls==2,"Sampling догонял пропущенные интервалы или прочёл дважды за один срок");
    const auto read=model.read("owner",query(id));
    require(read.at("state")=="running" && read.at("dropped_events")==0 && read.at("events").size()==3,
            "Большие события завершили наблюдение или вытеснились до заполнения запрошенной capacity");
    require(read.at("gap")==false && read.at("truncated")==false && read.at("cursor")==3
            && read.at("oldest_seq")==1,"Большой read потерял начало или ограничен прежним байтовым пределом");
    std::size_t bytes=0;
    for(std::size_t index=0;index<3;++index) {
        const auto& event=read.at("events").at(index);
        bytes+=event.dump().size();
        require(event.at("snapshot").at("objects")[0].at("properties")[0].at("value")
                ==std::string(300000,static_cast<char>('a'+index)),"Большое событие не сохранилось полностью");
    }
    require(read.at("buffer_bytes")==bytes && bytes>256*1024 && read.at("buffer_limit_bytes").is_null(),
            "Учёт байтов больших событий неверен или metadata рекламирует отсутствующий предел");
    const auto paged=model.read("owner",query(id,0,1));
    require(paged.at("events").size()==1 && paged.at("cursor")==1 && paged.at("truncated")==true,
            "Большой read проигнорировал явно запрошенный limit");
    const auto tail=model.read("owner",query(id,1));
    require(tail.at("events").size()==2 && tail.at("cursor")==3 && tail.at("truncated")==false,
            "Cursor не позволяет получить оставшиеся большие события");
    model.disconnect("owner");
}

void requested_capacity_and_read_limit_above_old_maxima() {
    Observations model;
    auto requested=params(1000,129);requested["interval_ms"]=1;
    const auto id=model.start("owner",requested,snapshot(0),0,1).at("watch_id").get<std::string>();
    for(int at=1;at<=128;++at)model.pump(at,at+1,[&](const json&){return snapshot(at);});
    const auto first=model.read("owner",query(id,0,101));
    require(first.at("capacity")==129 && first.at("events").size()==101 && first.at("truncated")==true
            && first.at("cursor")==101 && first.at("dropped_events")==0,"Запрошенные capacity 129 или limit 101 ограничены прежними максимумами");
    const auto remaining=model.read("owner",query(id,101,101));
    require(remaining.at("events").size()==28 && remaining.at("cursor")==129 && remaining.at("truncated")==false,
            "Пагинация сверх прежнего максимума потеряла события");
    model.pump(129,130,[&](const json&){return snapshot(129);});
    const auto full=model.read("owner",query(id,0,129));
    require(full.at("events").size()==129 && full.at("dropped_events")==1 && full.at("gap")==true
            && full.at("oldest_seq")==2 && full.at("cursor")==130,"Вытеснение больше не следует запрошенной capacity");
}

void requested_timing_above_old_maxima() {
    Observations model;
    auto requested=params(600002);requested["interval_ms"]=60001;
    const auto id=model.start("owner",requested,snapshot(1),0,1).at("watch_id").get<std::string>();
    int calls=0;
    model.pump(60000,2,[&](const json&){++calls;return snapshot(2);});
    require(calls==0,"Длинный interval заменён прежним максимумом");
    model.pump(60001,3,[&](const json&){++calls;return snapshot(2);});
    model.pump(600001,4,[&](const json&){++calls;return snapshot(3);});
    require(calls==2 && model.read("owner",query(id)).at("state")=="running","Длинная duration завершилась на прежнем максимуме");
    model.pump(600002,5,[&](const json&){++calls;return snapshot(4);});
    require(calls==2 && model.read("owner",query(id)).at("state")=="completed","Запрошенный срок не завершил наблюдение без дополнительного чтения");
}

void timing_and_count_native_representability() {
    constexpr auto maximum=std::numeric_limits<std::uint64_t>::max();
    Observations model;
    auto requested=params();requested["duration_ms"]=std::uint64_t{5};requested["interval_ms"]=std::uint64_t{3};
    requested["capacity"]=std::numeric_limits<std::size_t>::max();
    const auto id=model.start("owner",requested,snapshot(1),maximum-5,1).at("watch_id").get<std::string>();
    int calls=0;
    model.pump(maximum-3,2,[&](const json&){++calls;return snapshot(2);});
    require(calls==0,"Срок первой выборки переполнился");
    model.pump(maximum-2,3,[&](const json&){++calls;return snapshot(2);});
    model.pump(maximum-1,4,[&](const json&){++calls;return snapshot(3);});
    require(calls==1 && model.read("owner",{{"watch_id",id},{"after_seq",0},{"limit",std::numeric_limits<std::size_t>::max()}}).at("events").size()==2,
            "Следующая выборка переполнилась или представимые capacity/limit отклонены");
    model.pump(maximum,5,[&](const json&){++calls;return snapshot(4);});
    require(calls==1 && model.read("owner",query(id)).at("state")=="completed","Представимый deadline не завершил наблюдение");
    auto overflowing=requested;overflowing["duration_ms"]=std::uint64_t{6};
    error("invalid_watch_params",[&]{model.start("owner",overflowing,snapshot(1),maximum-5,1);});
    error("invalid_watch_params",[&]{model.start("owner",requested,snapshot(1),maximum,1);});
    requested["interval_ms"]=maximum;
    const auto no_sample=model.start("owner",requested,snapshot(1),maximum-5,6).at("watch_id").get<std::string>();
    model.pump(maximum-1,7,[&](const json&){++calls;return snapshot(5);});
    model.pump(maximum,8,[&](const json&){++calls;return snapshot(6);});
    require(calls==1 && model.read("owner",query(no_sample)).at("state")=="completed",
            "Представимый interval длиннее срока переполнился или вызвал лишнюю выборку");
}

void scalar_argument_boundaries() {
    require(g1r_mcp::parameter_extent(8,8,1,1)==9,"AS bool return beyond ParmsSize не включён");
    require(g1r_mcp::parameter_extent(8,8,8,1)==16,"AS object return beyond ParmsSize не включён");
    require(g1r_mcp::parameter_extent(8,0,8,1)==8,"Обычный параметр изменил размер");
    require(g1r_mcp::parameter_extent(8192,8192,8,1)==8200,"Параметр за 4096 байт не включён");
    require(g1r_mcp::parameter_extent(0,0,8192,1)==8192,"Поле больше 4096 байт не принимается");
    require(g1r_mcp::parameter_extent(65535,65535,8,1)==65543,"AS return за uint16 ParmsSize не включён");
    constexpr int maximum_extent=std::numeric_limits<int>::max();
    require(g1r_mcp::parameter_extent(0,maximum_extent-8,8,1)==maximum_extent,"INT_MAX extent не принимается");
    require(g1r_mcp::parameter_extent(maximum_extent,0,1,1)==maximum_extent,"Текущий INT_MAX extent не сохраняется");
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(0,maximum_extent-7,8,1);});
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(-1,0,1,1);});
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(8,-1,1,1);});
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(8,0,0,1);});
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(8,0,-1,1);});
    error("unsupported_signature",[]{g1r_mcp::parameter_extent(8,0,1,2);});
    g1r_mcp::validate_bool_layout(1,0,1);
    g1r_mcp::validate_bool_layout(1,0,255);
    error("unsupported_property_layout",[]{g1r_mcp::validate_bool_layout(1,1,1);});
    error("unsupported_property_layout",[]{g1r_mcp::validate_bool_layout(0,0,1);});
    error("unsupported_property_layout",[]{g1r_mcp::validate_bool_layout(1,0,0);});
    require(g1r_mcp::int32_argument(json(std::numeric_limits<std::int32_t>::min()))==
            std::numeric_limits<std::int32_t>::min(),"INT32_MIN не принимается");
    require(g1r_mcp::int32_argument(json(std::numeric_limits<std::int32_t>::max()))==
            std::numeric_limits<std::int32_t>::max(),"INT32_MAX не принимается");
    require(g1r_mcp::int32_argument(json(std::uint64_t{0}))==0,"Unsigned zero не принимается");
    error("invalid_argument",[]{g1r_mcp::int32_argument(json(std::numeric_limits<std::uint64_t>::max()));});
    error("invalid_argument",[]{g1r_mcp::int32_argument(json(std::uint64_t{2147483648ULL}));});
    error("invalid_argument",[]{g1r_mcp::int32_argument(json(std::int64_t{-2147483649LL}));});
    error("invalid_argument",[]{g1r_mcp::int32_argument(json(1.0));});
    error("invalid_argument",[]{g1r_mcp::int32_argument(json(true));});
    error("invalid_argument",[]{g1r_mcp::float_argument(json(1e300));});
    error("invalid_argument",[]{g1r_mcp::float_argument(json(-1e300));});
    error("invalid_argument",[]{g1r_mcp::float_argument(json(std::numeric_limits<double>::quiet_NaN()));});
    error("invalid_argument",[]{g1r_mcp::double_argument(json(std::numeric_limits<double>::infinity()));});
    require(g1r_mcp::float_argument(json(std::numeric_limits<float>::max()))==
            std::numeric_limits<float>::max(),"float max не принимается");
    require(g1r_mcp::double_argument(json(1e300))==1e300,"Конечный double потерян");
}
void object_argument_boundaries_and_inheritance() {
    const g1r_mcp::ObjectIdentity identity{1,2,3,4};
    require(g1r_mcp::same_object_identity(identity,{1,2,3,4}),"Одинаковая идентичность не переиспользуется");
    require(!g1r_mcp::same_object_identity(identity,{1,9,3,4}),"Изменение класса не меняет идентичность");
    require(!g1r_mcp::same_object_identity(identity,{1,2,3,9}),"Изменение имени не меняет идентичность");
    require(!g1r_mcp::same_object_identity(identity,{9,2,3,4}),"Изменение указателя не меняет идентичность");
    require(g1r_mcp::object_argument_handle(json(nullptr)).empty(),"Null объектного аргумента не принят");
    require(g1r_mcp::object_argument_handle(json{{"handle","obj-1"}})=="obj-1","Handle не принят");
    error("invalid_object_argument",[]{g1r_mcp::object_argument_handle(json(42));});
    error("invalid_object_argument",[]{g1r_mcp::object_argument_handle(json{{"handle","obj-1"},{"ptr",42}});});
    error("invalid_object_argument",[]{g1r_mcp::object_argument_handle(json{{"handle",""}});});
    auto parent=[](std::uintptr_t klass){return klass==3?std::uintptr_t{2}:klass==2?std::uintptr_t{1}:std::uintptr_t{0};};
    require(g1r_mcp::compatible_object_class(3,3,parent),"Точный класс не принят");
    require(g1r_mcp::compatible_object_class(3,1,parent),"Наследник не принят");
    require(!g1r_mcp::compatible_object_class(1,3,parent),"Базовый класс принят вместо наследника");
    error("invalid_object_class",[]{g1r_mcp::compatible_object_class(1,2,[](std::uintptr_t){return std::uintptr_t{1};});});
    auto long_parent=[](std::uintptr_t klass){return klass<200?klass+1:std::uintptr_t{0};};
    require(g1r_mcp::compatible_object_class(1,200,long_parent),"Предок после 64 звеньев не принят");
    require(!g1r_mcp::compatible_object_class(1,201,long_parent),"Отсутствующий предок принят в длинной цепочке");
    error("invalid_object_class",[]{g1r_mcp::compatible_object_class(1,201,
        [](std::uintptr_t klass){return klass<200?klass+1:std::uintptr_t{65};});});
    error("invalid_object_class",[]{g1r_mcp::compatible_object_class(0,1,[](std::uintptr_t){return std::uintptr_t{0};});});
    Observations model;
    auto data=snapshot(1);
    data["objects"][0]["properties"][0]["type"]="ObjectProperty";
    data["objects"][0]["properties"][0]["value"]={{"handle","obj-2"},{"index",2},
        {"name","Peer"},{"path","World.Peer"},{"class","Actor"},{"session_id","owner"}};
    const auto id=model.start("owner",params(),data,0,1).at("watch_id").get<std::string>();
    model.pump(10,2,[&](const json&){return data;});
    require(model.read("owner",query(id)).at("events").size()==1,"Стабильный object descriptor дал ложное изменение");
    model.pump(20,3,[&](const json&){auto empty=data;empty["objects"][0]["properties"][0]["value"]=nullptr;return empty;});
    require(model.read("owner",query(id)).at("events").size()==2,"Изменение object descriptor в null не замечено");
}
}

int main() {
    try {
        changes_limits_and_stop();
        expiry_invalidation_and_disconnect();
        shared_connection_client_ownership();
        list_and_resume_without_restarting();
        two_watches_and_scheduling();
        invalid_parameters_and_capacity();
        targets_and_watch_counts_follow_the_request();
        copied_large_values_and_no_catchup();
        requested_capacity_and_read_limit_above_old_maxima();
        requested_timing_above_old_maxima();
        timing_and_count_native_representability();
        scalar_argument_boundaries();
        object_argument_boundaries_and_inheritance();
        std::cout << "Проверки наблюдений пройдены\n";
        return 0;
    } catch (const std::exception& caught) {
        std::cerr << caught.what() << '\n';
        return 1;
    }
}
