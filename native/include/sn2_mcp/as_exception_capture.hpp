#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <utility>
#include <vector>
#include <nlohmann/json.hpp>

namespace sn2_mcp::as_exception_capture {

struct Stack {
    std::size_t exception_index{};
    std::vector<std::string> frames;
    bool complete{};
};

struct State {
    std::size_t exception_count{};
    std::vector<std::string> messages;
    bool full_capture{true};
    std::vector<Stack> stacks;
};

inline thread_local State* active{};
class LoggerScope;
inline thread_local LoggerScope* active_logger{};

// This scope exists only around the current ProcessEvent. A nested call
// temporarily owns its own record, then restores the previous owner.
// Restore Logger here after native SEH as well; /EHsc can skip
// the Logger's own C++ destructor inside ProcessEvent.
class Scope {
public:
    explicit Scope(State& state) noexcept:previous_(std::exchange(active,&state)),
        previous_logger_(std::exchange(active_logger,nullptr)) {}
    ~Scope() {active_logger=previous_logger_;active=previous_;}
    Scope(const Scope&)=delete;
    Scope& operator=(const Scope&)=delete;
private:
    State* previous_;
    LoggerScope* previous_logger_;
};

// Logger and its formatter share the exception index even
// when the message cannot be copied. A nested logger restores its owner.
class LoggerScope {
public:
    LoggerScope() noexcept:owner_(active),previous_(std::exchange(active_logger,this)) {
        if(owner_)exception_index_=owner_->exception_count++;
    }
    ~LoggerScope() {active_logger=previous_;}
    LoggerScope(const LoggerScope&)=delete;
    LoggerScope& operator=(const LoggerScope&)=delete;
    State* owner() const noexcept {return owner_;}
    std::size_t exception_index() const noexcept {return exception_index_;}
private:
    State* owner_;
    LoggerScope* previous_;
    std::size_t exception_index_{};
};

// Reader copies one byte through the existing SEH reader. Neither the logger pointer
// nor a partially read message is retained; the caller invokes the original logger
// even when copying fails.
template<class Reader>void record(const char* message,Reader&& read) noexcept {
    auto* state=active;
    if(!state)return;
    if(!active_logger || active_logger->owner()!=state)++state->exception_count;
    try {
        std::string copied;
        if(!message)copied="NO EXCEPTION";
        else {
            auto address=reinterpret_cast<std::uintptr_t>(message);
            for(;;++address) {
                char value{};
                if(!std::invoke(read,address,value)) {
                    state->full_capture=false;
                    return;
                }
                if(!value)break;
                copied.push_back(value);
            }
        }
        state->messages.push_back(std::move(copied));
    } catch(...) {state->full_capture=false;}
}

// The caller supplies owned UTF-8 strings after copying
// the formatter's borrowed result. Engine array pointers are not retained here.
inline void record_stack(std::vector<std::string> frames,bool complete) noexcept {
    if(!active_logger || !active || active_logger->owner()!=active)return;
    try {
        active->stacks.push_back({active_logger->exception_index(),std::move(frames),complete});
    } catch(...) {active->full_capture=false;}
}

inline nlohmann::json describe(const State& state) {
    nlohmann::json result={{"source","observed_logging_entry"},{"exception_count",state.exception_count},
        {"full_capture",state.full_capture},{"stack_captured",false},{"messages",nlohmann::json::array()}};
    nlohmann::json message_bytes=nlohmann::json::array();
    for(std::size_t index=0;index<state.messages.size();++index) {
        const auto& message=state.messages[index];
        const nlohmann::json text=message;
        try {
            static_cast<void>(text.dump());
            result["messages"].push_back(text);
        } catch(const nlohmann::json::type_error&) {
            result["messages"].push_back(nullptr);
            std::string hexadecimal;
            constexpr char digits[]="0123456789abcdef";
            for(const unsigned char byte:message) {
                hexadecimal.push_back(digits[byte>>4]);
                hexadecimal.push_back(digits[byte&15]);
            }
            message_bytes.push_back({{"index",index},{"encoding","hex"},{"bytes",std::move(hexadecimal)}});
        }
    }
    if(!message_bytes.empty())result["message_bytes"]=std::move(message_bytes);
    if(!state.stacks.empty()) {
        result["stacks_source"]="observed_engine_formatter";
        result["stacks"]=nlohmann::json::array();
        for(const auto& stack:state.stacks) {
            result["stacks"].push_back({{"exception_index",stack.exception_index},
                {"frames",stack.frames},{"complete",stack.complete}});
            if(stack.complete)result["stack_captured"]=true;
        }
    }
    return result;
}

} // namespace sn2_mcp::as_exception_capture
