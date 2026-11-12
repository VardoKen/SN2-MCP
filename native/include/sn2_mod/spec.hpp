#pragma once

namespace sn2_mod {
// The editable project supplies this specification; the runtime is compiled
// into that project's DLL and does not depend on SN2_MCP.dll or its transport.
struct Specification {
    const char* mod_id;
    const char* source_revision;
    float opacity;
};
} // namespace sn2_mod

extern "C" const sn2_mod::Specification* SN2ModSpecification() noexcept;
