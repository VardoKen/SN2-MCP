#include "sn2_mod/spec.hpp"

#ifndef SN2_MOD_ID
#define SN2_MOD_ID "widget_opacity"
#endif
#ifndef SN2_MOD_SOURCE_REVISION
#define SN2_MOD_SOURCE_REVISION "unrecorded"
#endif

namespace {
// Change this value, build and package the new revision, then restart the
// selected game and initialize the new artifact to observe its own effect.
constexpr float Opacity = 0.5f;
constexpr sn2_mod::Specification Specification{
    SN2_MOD_ID, SN2_MOD_SOURCE_REVISION, Opacity};
}

extern "C" const sn2_mod::Specification* SN2ModSpecification() noexcept {
    return &Specification;
}
