#include <sn2_mcp/blueprint_mod_record.hpp>
#include <iostream>
#include <stdexcept>

namespace {
int checks{};
void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}
}

int main() {
    using namespace sn2_mcp;
    try {
        BlueprintModRecord record;
        record.desired_enabled=true;
        require(!record.may_activate(0),"A missing world cannot start activation");
        require(record.may_activate(1),"The first confirmed world must allow activation");
        record.begin_activation(1);
        require(!record.may_activate(1),"Tick must not repeat a failed attempt in the same world");
        require(record.may_activate(1,true),"An explicit retry may follow a proven no-effect failure");
        const ObjectSerialIdentity actor{0x1000,0x2000,9,71,10,2,33,20};
        record.package_revision="A";
        record.retain_actor(actor,1);
        require(!record.actor_cleanup_verified,"A retained descriptor alone must not authorize actor destruction");
        record.actor_cleanup_verified=true;
        record.package_revision="B";
        require(record.actor_package_revision=="A","A new caller label must not relabel an existing actor revision");
        require(!record.may_activate(1,true),"Repeated enable must not duplicate a retained actor");
        require(!record.may_activate(2),"A new world does not prove the old actor ended");
        require(same_serial_identity(record.actor,actor),"A reconnect must retain engine identity without session handles");
        record.confirm_actor_ended("previous_world_ended");
        require(record.may_activate(2),"An independently ended actor allows a fresh-world attempt");
        record.begin_activation(2);
        require(!record.may_activate(2),"Only one automatic attempt is allowed per world");

        record.spawn_unknown=true;
        record.state="spawn_unknown";
        require(!record.may_activate(2,true) && !record.may_activate(3),"Unknown spawn must block explicit and world replay");
        record.desired_enabled=false;
        require(record.spawn_unknown,"Disable must retain unknown-spawn evidence");
        record.desired_enabled=true;
        record.confirm_actor_ended("destroyed");
        require(!record.may_activate(3),"An unrelated actor observation cannot resolve an unknown spawn");
        record.spawn_unknown=false;
        record.load_unknown=true;
        require(!record.may_activate(3,true),"An unknown blocking load must not be blindly repeated");
        record.load_unknown=false;

        record.retain_actor(actor,2);
        record.actor_cleanup_verified=true;
        record.finish_unknown=true;
        record.destroy_requested=true;
        record.destroy_unknown=true;
        record.state="destroy_pending";
        require(record.actor_retained && !record.may_activate(3),"A destroy call alone cannot prove actor cleanup");
        record.receipts.push_back({{"stage","finish"},{"effect","unknown"}});
        record.confirm_actor_ended("destroying_observed");
        require(!record.finish_unknown && !record.destroy_unknown,"Independent exact-actor cleanup resolves lifecycle ambiguity");
        require(record.receipts.at(0).at("effect")=="unknown","Cleanup must preserve historical unknown-effect receipts");
        require(record.may_activate(3),"Confirmed exact-actor cleanup allows a new-world activation");
        require(!record.may_activate(2),"Cleanup must not produce same-world automatic respawn");

        auto reused=actor;
        ++reused.serial;
        require(!same_serial_identity(actor,reused),"Slot reuse with a new serial must invalidate the old actor");
        auto zero=actor;zero.serial=0;zero.class_serial=0;
        require(serial_coverage(zero)=="unavailable_zero","Zero serial coverage must remain explicit");
        auto pending=actor;pending.serial=0;
        require(finish_allocated_actor_serial(pending,actor,true),"The same Finish return may establish its first actor serial");
        require(!finish_allocated_actor_serial(pending,actor,false),"A changed or unconfirmed world must reject first-serial adoption");
        require(!finish_allocated_actor_serial(actor,reused,true),"A changed nonzero serial must remain invalid");
        auto changed=actor;changed.object+=16;
        require(!finish_allocated_actor_serial(pending,changed,true),"A different returned pointer must not be adopted");
        changed=actor;++changed.name;
        require(!finish_allocated_actor_serial(pending,changed,true),"A different returned name must not be adopted");
        changed=actor;++changed.index;
        require(!finish_allocated_actor_serial(pending,changed,true),"A different returned slot must not be adopted");
        changed=actor;++changed.class_serial;
        require(!finish_allocated_actor_serial(pending,changed,true),"A changed class serial must reject actor serial adoption");
        changed=actor;changed.object_class+=16;
        require(!finish_allocated_actor_serial(pending,changed,true),"A changed class pointer must reject actor serial adoption");
        changed=actor;++changed.class_name;
        require(!finish_allocated_actor_serial(pending,changed,true),"A changed class name must reject actor serial adoption");
        changed=actor;++changed.class_index;
        require(!finish_allocated_actor_serial(pending,changed,true),"A changed class slot must reject actor serial adoption");
        require(!finish_allocated_actor_serial(pending,pending,true),"A still-zero serial is not an established serial");
        require(!same_serial_identity(pending,actor),"Ordinary handles must retain strict zero-serial equality");
        record.actor_identity_unresolved=true;
        record.actor_retained=false;
        require(!record.may_activate(4,true),"Unresolved Finish identity must block actor replay");
        record.actor_observation={{"state","unknown"},{"cause_code","invalid_memory"}};
        record.retain_actor(actor,2);
        require(!record.may_activate(3),"Unreadable identity is not actor invalidation");
        require(!record.actor_observation.contains("effect_observed"),"Actor observation must not invent Blueprint effect evidence");
        std::cout<<"Blueprint mod records: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<'\n';
        return 1;
    }
}
