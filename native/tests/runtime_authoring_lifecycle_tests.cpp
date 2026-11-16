#include <sn2_mcp/runtime_authoring_lifecycle.hpp>

#include <array>
#include <iostream>
#include <map>
#include <stdexcept>
#include <string>
#include <vector>

namespace {
using namespace sn2_mcp;
namespace lifecycle = runtime_authoring_lifecycle;
int checks{};
void require(bool condition, const char* message) {
    ++checks;
    if (!condition) throw std::runtime_error(message);
}
lifecycle::OwnedRoot captured(std::uintptr_t item, std::uintptr_t object, std::int32_t index) {
    return {item, {object, 0x9000, index, static_cast<std::uint64_t>(index), 12, 50, 500, 15}};
}
struct EngineState {
    std::map<std::uintptr_t, lifecycle::RootObservation> observed;
    std::vector<std::uintptr_t> reads, clears;
    lifecycle::RootObservation read(const lifecycle::OwnedRoot& root) {
        reads.push_back(root.item);
        return observed.at(root.item);
    }
    bool clear(const lifecycle::OwnedRoot& root) {
        clears.push_back(root.item);
        observed.at(root.item).root_set = false;
        return true;
    }
};

void acquisition_and_preflight() {
    lifecycle::RootLedger ledger;
    ledger.reserve(2);
    const auto package = captured(0x1000, 0xa000, 60), klass = captured(0x2000, 0xb000, 61);
    ledger.add_owned_root(package);
    ledger.add_owned_root(klass);
    bool duplicate{};
    try { ledger.add_owned_root(package); } catch (const std::invalid_argument&) { duplicate = true; }
    require(duplicate && ledger.roots().size() == 2, "Duplicate root aliases must not acquire another release obligation");

    EngineState engine{{{package.item, {lifecycle::ObservationState::valid, true}},
                        {klass.item, {lifecycle::ObservationState::identity_changed, true}}}};
    auto observer = [&](const auto& root) { return engine.read(root); };
    auto clearer = [&](const auto& root) { return engine.clear(root); };
    const auto rejected = ledger.release(observer, clearer);
    require(rejected.state == lifecycle::ReleaseState::identity_changed && !rejected.attempted
            && !rejected.retirement_requested && engine.clears.empty(),
            "A reused later object must reject all release mutations and preserve an active revision");
    require(rejected.failed_item == klass.item && rejected.retained_count == 2,
            "Preflight must identify the changed root without releasing an earlier valid root");

    engine.observed.at(klass.item) = {lifecycle::ObservationState::valid, true};
    engine.reads.clear();
    const auto released = ledger.release(observer, clearer);
    require(released.state == lifecycle::ReleaseState::released && released.attempted
            && released.retirement_requested && released.released_count == 2,
            "Complete verified clearing must retire every owned root");
    require(engine.clears == std::vector<std::uintptr_t>{klass.item, package.item},
            "Class must release before the package acquired ahead of it");
    require(engine.reads.size() == 4 && engine.reads[0] == package.item && engine.reads[1] == klass.item,
            "Every pending object identity must be read before any clear and confirmed afterward");
    const auto reads_before = engine.reads.size();
    const auto repeated = ledger.release(observer, clearer);
    require(repeated.state == lifecycle::ReleaseState::released && !repeated.attempted
            && engine.clears.size() == 2 && engine.reads.size() == reads_before,
            "A completed tombstone must remain idempotent after released objects can be collected");
    bool late_root{};
    try { ledger.add_owned_root(captured(0x3000, 0xc000, 62)); }
    catch (const std::logic_error&) { late_root = true; }
    require(late_root, "A retired revision cannot acquire unrelated roots");
}

void partial_failure_and_reconciliation(bool fault_cleared_flag) {
    lifecycle::RootLedger ledger;
    const auto package = captured(0x1000, 0xa000, 60), klass = captured(0x2000, 0xb000, 61);
    ledger.add_owned_root(package);
    ledger.add_owned_root(klass);
    EngineState engine{{{package.item, {lifecycle::ObservationState::valid, true}},
                        {klass.item, {lifecycle::ObservationState::valid, true}}}};
    auto observer = [&](const auto& root) { return engine.read(root); };
    auto failing_clear = [&](const auto& root) {
        engine.clears.push_back(root.item);
        if (root.item == package.item) {
            if (fault_cleared_flag) engine.observed.at(root.item).root_set = false;
            throw std::runtime_error("Captured engine fault");
        }
        engine.observed.at(root.item).root_set = false;
        return true;
    };
    const auto failed = ledger.release(observer, failing_clear);
    require(failed.state == lifecycle::ReleaseState::release_unknown && failed.attempted
            && failed.released_count == 1 && failed.unknown_count == 1 && failed.retirement_requested,
            "A fault after partial release must preserve the confirmed prefix and uncertain root");
    require(failed.failed_item == package.item, "The failed root must remain attributable");
    const auto previous_reads = engine.reads.size(), previous_clears = engine.clears.size();
    const auto repeated = ledger.release(observer, failing_clear);
    require(repeated.state == lifecycle::ReleaseState::release_unknown && !repeated.attempted
            && engine.reads.size() == previous_reads && engine.clears.size() == previous_clears,
            "Unknown clearing must not be blindly retried or silently reconciled by a release call");

    const auto reconciled = ledger.reconcile(observer);
    require(!reconciled.attempted && !reconciled.unknown_count && reconciled.retirement_requested
            && engine.clears.size() == previous_clears,
            "Explicit reconciliation must observe current flags without a second mutation");
    if (fault_cleared_flag) {
        require(reconciled.state == lifecycle::ReleaseState::released && reconciled.released_count == 2,
                "An independently absent root confirms the originally uncertain call");
    } else {
        require(reconciled.state == lifecycle::ReleaseState::retained && reconciled.retained_count == 1,
                "A confirmed still-rooted item permits a later explicit release");
        const auto resumed = ledger.release(observer, [&](const auto& root) { return engine.clear(root); });
        require(resumed.state == lifecycle::ReleaseState::released
                && engine.clears == std::vector<std::uintptr_t>{klass.item, package.item, package.item},
                "Only the positively reconciled remaining root may be cleared again");
    }
}

void unconfirmed_readback_and_reused_slot() {
    lifecycle::RootLedger ledger;
    const auto klass = captured(0x2000, 0xb000, 61);
    ledger.add_owned_root(klass);
    EngineState engine{{{klass.item, {lifecycle::ObservationState::valid, true}}}};
    auto observer = [&](const auto& root) { return engine.read(root); };
    const auto failed = ledger.release(observer, [&](const auto& root) {
        engine.clears.push_back(root.item);
        engine.observed.at(root.item) = {lifecycle::ObservationState::unavailable, false};
        return true;
    });
    require(failed.state == lifecycle::ReleaseState::release_unknown && failed.unknown_count == 1,
            "An apparently returned engine call still requires readable identity and RootSet readback");
    engine.observed.at(klass.item) = {lifecycle::ObservationState::identity_changed, false};
    const auto rejected = ledger.reconcile(observer);
    require(rejected.state == lifecycle::ReleaseState::release_unknown && rejected.unknown_count == 1
            && rejected.failed_observation == lifecycle::ObservationState::identity_changed,
            "A reused registry slot cannot resolve prior clearing by reading the new object's absent root");
    const auto repeated = ledger.release(observer, [&](const auto& root) { return engine.clear(root); });
    require(!repeated.attempted && engine.clears.size() == 1,
            "Unresolved identity reuse must never clear the replacement object's root");
}

void roots_already_absent_and_empty() {
    lifecycle::RootLedger ledger;
    const auto root = captured(0x1000, 0xa000, 60);
    ledger.add_owned_root(root);
    EngineState engine{{{root.item, {lifecycle::ObservationState::valid, false}},
                        {0x3000, {lifecycle::ObservationState::valid, true}}}};
    auto observer = [&](const auto& item) { return engine.read(item); };
    const auto released = ledger.release(observer, [&](const auto& item) { return engine.clear(item); });
    require(released.state == lifecycle::ReleaseState::released && !released.attempted
            && released.released_count == 1 && engine.clears.empty(),
            "Already absent owned RootSet flags must be confirmed without a redundant clear");
    require(engine.observed.at(0x3000).root_set && engine.reads == std::vector<std::uintptr_t>{root.item},
            "A ledger must not inspect or clear preexisting roots outside its compilation ownership");
    lifecycle::RootLedger empty;
    const auto no_roots = empty.release(observer, [&](const auto& item) { return engine.clear(item); });
    require(no_roots.state == lifecycle::ReleaseState::released && no_roots.retirement_requested
            && !no_roots.attempted && !no_roots.released_count,
            "A successful revision with no newly added roots can retire without touching engine objects");
}

void initial_serial_refresh() {
    lifecycle::RootLedger ledger;
    auto package = captured(0x1000, 0xa000, 60), klass = captured(0x2000, 0xb000, 61);
    package.identity.serial = 0;
    package.identity.class_serial = 0;
    klass.identity.serial = 0;
    ledger.add_owned_root(package);
    ledger.add_owned_root(klass);
    auto live_package = package.identity, live_class = klass.identity;
    live_package.serial = 71;
    live_package.class_serial = 81;
    live_class.serial = 72;

    auto changed = live_class;
    ++changed.class_serial;
    const std::array invalid_batch{
        lifecycle::RootIdentityObservation{package.item, live_package, true},
        lifecycle::RootIdentityObservation{klass.item, changed, true}};
    require(!ledger.refresh_owned_identities(invalid_batch)
            && ledger.roots()[0].identity == package.identity
            && ledger.roots()[1].identity == klass.identity && ledger.identity_refreshes().empty(),
            "A later nonzero identity mismatch must reject the entire refresh batch without adopting an earlier upgrade");

    const std::array valid_batch{
        lifecycle::RootIdentityObservation{package.item, live_package, true},
        lifecycle::RootIdentityObservation{klass.item, live_class, true}};
    require(ledger.refresh_owned_identities(valid_batch)
            && same_serial_identity(ledger.roots()[0].identity, live_package)
            && same_serial_identity(ledger.roots()[1].identity, live_class),
            "Confirmed rooted objects may adopt their first engine-allocated object and class serials together");
    require(ledger.identity_refreshes().size() == 2
            && ledger.identity_refreshes()[0].before.serial == 0
            && ledger.identity_refreshes()[0].before.class_serial == 0
            && ledger.identity_refreshes()[0].after.serial == 71
            && ledger.snapshot().identity_refresh_count == 2,
            "Original zero-serial coverage and each adopted identity must remain available after refresh");
    require(ledger.refresh_owned_identities(valid_batch) && ledger.identity_refreshes().size() == 2,
            "Identical rooted observations must not create duplicate refresh evidence");

    std::map<std::uintptr_t, ObjectSerialIdentity> current{{package.item, live_package}, {klass.item, live_class}};
    EngineState engine{{{package.item, {lifecycle::ObservationState::valid, true}},
                        {klass.item, {lifecycle::ObservationState::valid, true}}}};
    auto observer = [&](const auto& root) {
        if (!same_serial_identity(root.identity, current.at(root.item)))
            return lifecycle::RootObservation{lifecycle::ObservationState::identity_changed, true};
        return engine.read(root);
    };
    const auto released = ledger.release(observer, [&](const auto& root) { return engine.clear(root); });
    require(released.state == lifecycle::ReleaseState::released && released.identity_refresh_count == 2,
            "Release must use the adopted identities for strict preflight and post-clear readback");
    require(!ledger.refresh_owned_identity(package.item, live_package, true),
            "An identical observation after retirement cannot reopen identity acquisition");
}

void invalid_identity_refreshes() {
    lifecycle::RootLedger ledger;
    auto root = captured(0x1000, 0xa000, 60);
    root.identity.serial = 0;
    ledger.add_owned_root(root);
    auto valid = root.identity;
    valid.serial = 71;
    require(!ledger.refresh_owned_identity(root.item, valid, false),
            "An absent or unconfirmed root cannot justify initial serial adoption");
    require(!ledger.refresh_owned_identity(0x9000, valid, true),
            "Identity refresh cannot add an unowned item");
    auto changed = valid;
    ++changed.object;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A replacement object pointer cannot refresh an owned identity");
    changed = valid; ++changed.object_class;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A different class pointer cannot refresh an owned identity");
    changed = valid; ++changed.index;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A different registry index cannot refresh an owned identity");
    changed = valid; ++changed.name;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A different object name cannot refresh an owned identity");
    changed = valid; ++changed.class_index;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A different class registry slot cannot refresh an owned identity");
    changed = valid; ++changed.class_name;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A different class name cannot refresh an owned identity");
    changed = valid; ++changed.class_serial;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "An already nonzero class serial cannot be replaced");
    changed = valid; changed.serial = -1;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A negative object serial cannot be adopted");
    changed = valid; changed.class_serial = -1;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "A negative class serial cannot be adopted");
    const std::array duplicate_batch{
        lifecycle::RootIdentityObservation{root.item, valid, true},
        lifecycle::RootIdentityObservation{root.item, valid, true}};
    require(!ledger.refresh_owned_identities(duplicate_batch)
            && ledger.roots()[0].identity == root.identity && ledger.identity_refreshes().empty(),
            "A duplicate item batch must not partially adopt identity updates");
    require(ledger.refresh_owned_identity(root.item, valid, true), "A compatible rooted first serial must remain adoptable after rejected observations");
    changed = valid; ++changed.serial;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "An adopted nonzero serial cannot change on a later refresh");
    changed = valid; changed.serial = 0;
    require(!ledger.refresh_owned_identity(root.item, changed, true), "An adopted serial cannot be downgraded to zero");
}
} // namespace

int main() {
    try {
        acquisition_and_preflight();
        partial_failure_and_reconciliation(false);
        partial_failure_and_reconciliation(true);
        unconfirmed_readback_and_reused_slot();
        roots_already_absent_and_empty();
        initial_serial_refresh();
        invalid_identity_refreshes();
        std::cout << "Runtime authoring lifecycle: " << checks << " checks passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
