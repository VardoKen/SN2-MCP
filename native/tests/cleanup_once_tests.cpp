#include <g1r_mcp/cleanup_once.hpp>
#include <g1r_mcp/object_array_input_lifetime.hpp>
#include <g1r_mcp/string_input_lifetime.hpp>

#include <array>
#include <concepts>
#include <iostream>
#include <stdexcept>
#include <type_traits>
#include <vector>

namespace {
namespace strings=g1r_mcp::string_input_lifetime;
namespace arrays=g1r_mcp::object_array_input_lifetime;
int checks{};
void require(bool good,const char* message) {++checks;if(!good)throw std::runtime_error(message);}
template<class Result> bool same(const Result& left,const Result& right) {
    return left.attempted==right.attempted && left.confirmed==right.confirmed && left.uncertain==right.uncertain &&
        left.released_count==right.released_count;
}
template<class Once,class Action> concept CanRun=requires(Once& once,Action&& action) {once.run(std::forward<Action>(action));};
struct NoThrowAction {strings::Cleanup operator()() noexcept {return {};}};
struct ThrowingAction {strings::Cleanup operator()() {return {};}};
struct WrongResultAction {arrays::Cleanup operator()() noexcept {return {};}};
struct NotCallable {};
using StringOnce=g1r_mcp::CleanupOnce<strings::Cleanup>;
using ArrayOnce=g1r_mcp::CleanupOnce<arrays::Cleanup>;
static_assert(CanRun<StringOnce,NoThrowAction>);
static_assert(!CanRun<StringOnce,ThrowingAction>);
static_assert(!CanRun<StringOnce,WrongResultAction>);
static_assert(!CanRun<StringOnce,NotCallable>);
static_assert(noexcept(std::declval<StringOnce&>().run(std::declval<NoThrowAction>())));
static_assert(!std::is_copy_constructible_v<StringOnce> && !std::is_copy_assignable_v<StringOnce>);
static_assert(!std::is_move_constructible_v<StringOnce> && !std::is_move_assignable_v<StringOnce>);
static_assert(!std::is_copy_constructible_v<ArrayOnce> && !std::is_move_constructible_v<ArrayOnce>);

enum class Failure {none,false_result,exception};
template<class Ledger,class Result,class Setup>
void cleanup_case(Setup&& setup,Failure failure,std::size_t fail_after,std::size_t total_allocations,std::size_t retained_bytes) {
    Ledger ledger;setup(ledger);g1r_mcp::CleanupOnce<Result> once;
    require(!once.called(),"CleanupOnce не должен считаться исполненным до первого действия");
    int actions{},free_calls{};bool called_inside{};
    auto release=[&]() noexcept -> Result {
        ++actions;called_inside=once.called();
        return ledger.release([&](std::uintptr_t pointer)->bool {
            ++free_calls;require(pointer>=0x1000 && pointer<=0x4000 && pointer%0x1000==0,"Free должен получить собственный буфер ledger");
            if(failure!=Failure::none && static_cast<std::size_t>(free_calls)>fail_after) {
                if(failure==Failure::exception)throw std::runtime_error("Free fault");return false;
            }
            return true;
        });
    };
    const auto first=once.run(release);
    require(once.called() && called_inside && actions==1,"Первое действие выполняется единожды после установки called");
    const auto expected_released=failure==Failure::none?total_allocations:fail_after;
    require(first.attempted==(total_allocations!=0) && first.confirmed==(failure==Failure::none) && first.uncertain==(failure!=Failure::none) &&
        first.released_count==expected_released,"Первая Cleanup сохраняет точный успех, отказ и число подтверждённых Free");
    require(ledger.retained_count()==total_allocations-expected_released && ledger.retained_bytes()==retained_bytes,"Retained учитывает только неосвобождённые буферы");
    const auto second=once.run(release);
    require(same(first,second) && actions==1,"Повторный run сохраняет первоначальный attempted и весь результат без новой попытки");
    int alternative_calls{};const auto different=once.run([&]() noexcept -> Result {++alternative_calls;return {false,true,false,999};});
    require(same(first,different) && alternative_calls==0,"Даже другое действие не заменяет сохранённую Cleanup");
    require(static_cast<std::size_t>(free_calls)==(failure==Failure::none?total_allocations:fail_after+1),"Повторный run не вызывает дополнительный Free");
    require(ledger.retained_count()==total_allocations-expected_released && ledger.retained_bytes()==retained_bytes,"Повторный run не меняет retained ledger");
}

template<class Ledger,class Result,class Setup> void uncertain_case(Setup&& setup,std::size_t count,std::size_t bytes) {
    Ledger ledger;setup(ledger);ledger.mark_uncertain();g1r_mcp::CleanupOnce<Result> once;int actions{},free_calls{};
    auto release=[&]() noexcept -> Result {++actions;return ledger.release([&](std::uintptr_t){++free_calls;return true;});};
    const auto first=once.run(release);const auto second=once.run(release);
    require(!first.attempted && !first.confirmed && first.uncertain && first.released_count==0,"Неизвестное владение сохраняет точный отказ без попытки Free");
    require(same(first,second) && once.called() && actions==1 && free_calls==0,"Неизвестное владение не повторяет действие или Free");
    require(ledger.retained_count()==count && ledger.retained_bytes()==bytes,"Неизвестный ledger сохраняет все известные буферы");
}
}

int main() {
    try {
 const auto one_string=[](strings::Ledger& ledger){ledger.prepare(1);ledger.add_input({0x1000,2,4},1);};
 const auto four_strings=[](strings::Ledger& ledger){ledger.prepare(3);ledger.add_input({0x1000,2,4},1);ledger.add_input({0x2000,2,4},1);
            ledger.add_input({0x3000,2,4},1);ledger.add_return({0x4000,2,4});};
 const auto one_array=[](arrays::Ledger& ledger){ledger.reserve_inputs(1);ledger.add_input({0x1000,1,1},1);};
 const auto four_arrays=[](arrays::Ledger& ledger){ledger.reserve_inputs(4);ledger.add_input({0x1000,1,1},1);ledger.add_input({0x2000,2,2},2);
            ledger.add_input({0x3000,3,3},3);ledger.add_input({0x4000,4,4},4);};
        for(const auto failure:std::array<Failure,2>{Failure::false_result,Failure::exception}) {
            cleanup_case<strings::Ledger,strings::Cleanup>(one_string,failure,0,1,8);
            cleanup_case<arrays::Ledger,arrays::Cleanup>(one_array,failure,0,1,8);
            cleanup_case<strings::Ledger,strings::Cleanup>(four_strings,failure,2,4,16);
            cleanup_case<arrays::Ledger,arrays::Cleanup>(four_arrays,failure,2,4,56);
        }
        cleanup_case<strings::Ledger,strings::Cleanup>(one_string,Failure::none,0,1,0);
        cleanup_case<arrays::Ledger,arrays::Cleanup>(four_arrays,Failure::none,0,4,0);
 cleanup_case<strings::Ledger,strings::Cleanup>([](strings::Ledger& ledger){ledger.prepare(1);ledger.add_input({0,0,0},0);ledger.add_return({0,0,0});},Failure::none,0,0,0);
 cleanup_case<arrays::Ledger,arrays::Cleanup>([](arrays::Ledger& ledger){ledger.reserve_inputs(1);ledger.add_input({0,0,0},0);},Failure::none,0,0,0);
        uncertain_case<strings::Ledger,strings::Cleanup>(four_strings,4,32);
        uncertain_case<arrays::Ledger,arrays::Cleanup>(four_arrays,4,80);
        uncertain_case<strings::Ledger,strings::Cleanup>([](strings::Ledger&){},0,0);
        uncertain_case<arrays::Ledger,arrays::Cleanup>([](arrays::Ledger&){},0,0);
        {alignas(8) std::array<std::uintptr_t,3> objects{0x9000,0x9000,0xa000};
 const auto pointer=reinterpret_cast<std::uintptr_t>(objects.data());arrays::Ledger ledger;ledger.reserve_inputs(1);ledger.add_input({pointer,3,3},3);
            ArrayOnce once;std::vector<std::uintptr_t> released;
            auto cleanup=[&]() noexcept -> arrays::Cleanup {return ledger.release([&](std::uintptr_t buffer){released.push_back(buffer);return true;});};
            const auto first=once.run(cleanup);const auto second=once.run(cleanup);
            require(first.attempted && first.confirmed && !first.uncertain && first.released_count==1 && same(first,second),"Сохранённая успешная Cleanup сохраняет attempted=true при повторном run");
            require(released==std::vector<std::uintptr_t>{pointer} && objects==std::array<std::uintptr_t,3>{0x9000,0x9000,0xa000},"Free получает один буфер, никогда UObject или повторяющиеся элементы");}
        std::cout<<"cleanup_once: "<<checks<<" checks passed\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<"\n";return 1;}
}
