#pragma once

#include <cstdint>
#include <string>
#include <nlohmann/json.hpp>

namespace sn2_mcp {

// Discovery records code/data candidates. Per-operation readiness additionally
// requires game-thread metadata or type/lifetime evidence, never a build label.
struct RuntimeBinding {
    std::uintptr_t rva{};
    std::string error;
    bool runtime_validated{};
    nlohmann::json discovery=nullptr;
    bool available() const noexcept { return rva != 0 && error.empty(); }
    nlohmann::json describe() const {
        return {{"state",available()?(runtime_validated?"verified":"discovered"):"unavailable"},
                {"rva",rva?nlohmann::json(rva):nlohmann::json(nullptr)},
                {"error",error.empty()?nlohmann::json(nullptr):nlohmann::json(error)},
            {"runtime_validated",runtime_validated},{"discovery",discovery}};
    }
};

struct RuntimeBindings {
    RuntimeBinding tick, object_array, names, process_event, name_construct,
                   string_construct, free, realloc, create_package;
    bool executor_discovered() const noexcept { return tick.available(); }
    bool reflection_discovered() const noexcept {
        return object_array.available() && names.available();
    }
    nlohmann::json describe() const {
        return {{"tick",tick.describe()},{"object_array",object_array.describe()},
                {"names",names.describe()},{"process_event",process_event.describe()},
                {"name_construct",name_construct.describe()},
                {"string_construct",string_construct.describe()},
                {"free",free.describe()},{"realloc",realloc.describe()},
                {"create_package",create_package.describe()}};
    }
};

} // namespace sn2_mcp
