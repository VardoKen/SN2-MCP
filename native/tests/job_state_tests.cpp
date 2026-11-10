#include "sn2_mcp/job_state.hpp"
#include <thread>
#include <iostream>
using sn2_mcp::JobState;
int main() {
    JobState cancelled;
    if(!cancelled.cancel()||cancelled.begin()||cancelled.cancel()||cancelled.get()!=JobState::cancelled)return 1;
    JobState running;
    if(!running.begin()||running.begin()||running.cancel())return 2;
    running.finish();if(running.get()!=JobState::completed||running.begin()||running.cancel())return 3;
    // В гонке ровно один владелец: отмена или начало. Поздний begin после отмены невозможен.
    for(int i=0;i<2000;++i) {
        JobState state;bool begun{},cancelled_first{};
        std::thread a([&]{begun=state.begin();});
        std::thread b([&]{cancelled_first=state.cancel();});
        a.join();b.join();
        if(begun==cancelled_first)return 4;
        if(cancelled_first && (state.begin()||state.get()!=JobState::cancelled))return 5;
        if(begun && (state.cancel()||state.get()!=JobState::running))return 6;
    }
    std::cout<<"job_state: transitions and 2000 cancellation races passed\n";
}
