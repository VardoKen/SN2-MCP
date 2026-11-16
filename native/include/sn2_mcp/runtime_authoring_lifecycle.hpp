#pragma once

#include <sn2_mcp/object_serial.hpp>

#include <cstddef>
#include <cstdint>
#include <functional>
#include <span>
#include <stdexcept>
#include <vector>

namespace sn2_mcp::runtime_authoring_lifecycle {

enum class ObservationState { valid, identity_changed, unavailable };
struct RootObservation {
    ObservationState state{ObservationState::unavailable};
    bool root_set{};
};

enum class RootState { retained, released, unknown };
struct OwnedRoot {
    std::uintptr_t item{};
    ObjectSerialIdentity identity;
    RootState state{RootState::retained};
};

struct RootIdentityObservation {
    std::uintptr_t item{};
    ObjectSerialIdentity identity;
    bool confirmed_root_set{};
};
struct IdentityRefresh {
    std::uintptr_t item{};
    ObjectSerialIdentity before, after;
};

// An engine weak-reference construction can allocate an object's first serial
// while its authoring-owned RootSet remains present. It never changes an already
// nonzero serial. This exception is limited to the verified rooted revision;
// ordinary handles and observations keep their strict identity contract.
inline bool rooted_initial_serial_refresh(const ObjectSerialIdentity& before,
                                         const ObjectSerialIdentity& after) noexcept {
    const auto initial_or_equal = [](std::int32_t old_value, std::int32_t new_value) {
        return valid_observed_serial(old_value) && valid_observed_serial(new_value)
            && (old_value == new_value || (!old_value && new_value > 0));
    };
    return before.object == after.object && before.object_class == after.object_class
        && before.index == after.index && before.name == after.name
        && before.class_index == after.class_index && before.class_name == after.class_name
        && initial_or_equal(before.serial, after.serial)
        && initial_or_equal(before.class_serial, after.class_serial);
}

enum class ReleaseState { retained, released, identity_changed, observation_unavailable, release_unknown };
struct ReleaseResult {
    ReleaseState state{ReleaseState::retained};
    bool attempted{};
    bool retirement_requested{};
    std::size_t released_count{}, retained_count{}, unknown_count{};
    std::size_t identity_refresh_count{};
    std::uintptr_t failed_item{};
    ObservationState failed_observation{ObservationState::valid};
};

// Owns only RootSet flags added by one successful native compilation. Releasing
// those flags is not UObject destruction or a promise that GC has run. Engine
// references from live instances must retain their classes independently.
class RootLedger {
public:
    RootLedger() = default;
    RootLedger(const RootLedger&) = delete;
    RootLedger& operator=(const RootLedger&) = delete;
    RootLedger(RootLedger&&) noexcept = default;
    RootLedger& operator=(RootLedger&&) noexcept = default;

    void reserve(std::size_t count) { roots_.reserve(count); }

    void add_owned_root(OwnedRoot root) {
        if (retirement_requested_)
            throw std::logic_error("A retiring revision cannot acquire roots");
        if (!root.item || !root.identity.object || !root.identity.object_class
            || root.identity.index < 0 || root.identity.class_index < 0
            || !valid_observed_serial(root.identity.serial)
            || !valid_observed_serial(root.identity.class_serial)
            || root.state != RootState::retained)
            throw std::invalid_argument("An owned root requires a valid captured object identity");
        for (const auto& existing : roots_)
            if (existing.item == root.item || existing.identity.object == root.identity.object)
                throw std::invalid_argument("An authoring root must be recorded exactly once");
        roots_.push_back(root);
    }

    const std::vector<OwnedRoot>& roots() const noexcept { return roots_; }
    const std::vector<IdentityRefresh>& identity_refreshes() const noexcept { return identity_refreshes_; }
    bool retirement_requested() const noexcept { return retirement_requested_; }

    // Caller revalidates these identities and RootSet observations on the game
    // thread before calling this method. The entire batch is checked before any
    // captured identity changes; failure leaves the original snapshots intact.
    bool refresh_owned_identities(std::span<const RootIdentityObservation> observed) {
        if (retirement_requested_) return false;
        std::vector<std::size_t> selected;
        std::vector<IdentityRefresh> updates;
        selected.reserve(observed.size());
        updates.reserve(observed.size());
        for (const auto& value : observed) {
            if (!value.confirmed_root_set) return false;
            std::size_t index{};
            while (index < roots_.size() && roots_[index].item != value.item) ++index;
            if (index == roots_.size() || roots_[index].state != RootState::retained) return false;
            for (const auto previous : selected) if (previous == index) return false;
            const auto& before = roots_[index].identity;
            if (!rooted_initial_serial_refresh(before, value.identity)) return false;
            selected.push_back(index);
            if (!(before == value.identity)) updates.push_back({value.item, before, value.identity});
        }
        // Allocate audit storage before applying any identity update.
        identity_refreshes_.reserve(identity_refreshes_.size() + updates.size());
        for (std::size_t i = 0; i < observed.size(); ++i) roots_[selected[i]].identity = observed[i].identity;
        for (const auto& update : updates) identity_refreshes_.push_back(update);
        return true;
    }

    bool refresh_owned_identity(std::uintptr_t item, const ObjectSerialIdentity& identity,
                                bool confirmed_root_set) {
        const RootIdentityObservation observed{item, identity, confirmed_root_set};
        return refresh_owned_identities(std::span<const RootIdentityObservation>(&observed, 1));
    }

    ReleaseResult snapshot() const noexcept {
        ReleaseResult result;
        result.retirement_requested = retirement_requested_;
        result.failed_item = failed_item_;
        result.failed_observation = failed_observation_;
        result.identity_refresh_count = identity_refreshes_.size();
        for (const auto& root : roots_) {
            if (root.state == RootState::released) ++result.released_count;
            else if (root.state == RootState::unknown) ++result.unknown_count;
            else ++result.retained_count;
        }
        result.state = result.unknown_count ? ReleaseState::release_unknown
            : (retirement_requested_ && !result.retained_count) ? ReleaseState::released
            : failure_;
        return result;
    }

    // Observer must independently validate the captured identity and report the
    // current RootSet bit. Clearer returns whether its engine call completed
    // without a fault; ClearRootFlags' own false return may be idempotent.
    template<class Observer, class Clearer>
    ReleaseResult release(Observer&& observer, Clearer&& clearer) {
        if (snapshot().unknown_count) return snapshot();

        std::vector<RootObservation> observed(roots_.size());
        if (!observe_pending(observer, observed)) return snapshot();

        retirement_requested_ = true;
        clear_failure();
        for (std::size_t i = 0; i < roots_.size(); ++i)
            if (roots_[i].state != RootState::released && !observed[i].root_set)
                roots_[i].state = RootState::released;

        bool attempted{};
        // Package is acquired before its class; release owned roots in reverse
        // acquisition order, with all identities preflighted before mutation.
        for (std::size_t remaining = roots_.size(); remaining; --remaining) {
            auto& root = roots_[remaining - 1];
            if (root.state == RootState::released) continue;
            attempted = true;
            bool returned{};
            try { returned = std::invoke(clearer, root); }
            catch (...) { returned = false; }
            if (!returned) {
                root.state = RootState::unknown;
                fail(ReleaseState::release_unknown, root.item, ObservationState::unavailable);
                break;
            }
            const auto after = observe(observer, root);
            if (after.state != ObservationState::valid || after.root_set) {
                root.state = RootState::unknown;
                fail(ReleaseState::release_unknown, root.item, after.state);
                break;
            }
            root.state = RootState::released;
        }
        auto result = snapshot();
        result.attempted = attempted;
        return result;
    }

    // Read-only reconciliation never repeats an uncertain ClearRootFlags call.
    // A valid still-rooted observation permits a later explicit release; a
    // valid unrooted observation confirms release without another mutation.
    template<class Observer>
    ReleaseResult reconcile(Observer&& observer) {
        if (!snapshot().unknown_count) return snapshot();
        std::vector<RootObservation> observed(roots_.size());
        if (!observe_pending(observer, observed)) return snapshot();
        for (std::size_t i = 0; i < roots_.size(); ++i)
            if (roots_[i].state != RootState::released)
                roots_[i].state = observed[i].root_set ? RootState::retained : RootState::released;
        clear_failure();
        return snapshot();
    }

private:
    std::vector<OwnedRoot> roots_;
    std::vector<IdentityRefresh> identity_refreshes_;
    bool retirement_requested_{};
    ReleaseState failure_{ReleaseState::retained};
    std::uintptr_t failed_item_{};
    ObservationState failed_observation_{ObservationState::valid};

    void fail(ReleaseState state, std::uintptr_t item, ObservationState observed) noexcept {
        failure_ = state;
        failed_item_ = item;
        failed_observation_ = observed;
    }
    void clear_failure() noexcept {
        fail(ReleaseState::retained, 0, ObservationState::valid);
    }

    template<class Observer>
    static RootObservation observe(Observer& observer, const OwnedRoot& root) noexcept {
        try { return std::invoke(observer, root); }
        catch (...) { return {ObservationState::unavailable, false}; }
    }

    template<class Observer>
    bool observe_pending(Observer& observer, std::vector<RootObservation>& observed) noexcept {
        for (std::size_t i = 0; i < roots_.size(); ++i) {
            const auto& root = roots_[i];
            if (root.state == RootState::released) continue;
            observed[i] = observe(observer, root);
            if (observed[i].state != ObservationState::valid) {
                fail(observed[i].state == ObservationState::identity_changed
                    ? ReleaseState::identity_changed : ReleaseState::observation_unavailable,
                    root.item, observed[i].state);
                return false;
            }
        }
        return true;
    }
};

} // namespace sn2_mcp::runtime_authoring_lifecycle
