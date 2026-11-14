#include "sn2_mcp/runtime_archive.hpp"
#include <Windows.h>
#include <algorithm>
#include <array>
#include <cstring>
#include <limits>
#include <utility>

namespace sn2_mcp::runtime_archive {
ArchiveError::ArchiveError(std::string value,std::string message)
    :std::runtime_error(std::move(message)),code(std::move(value)) {}
namespace {
using NativeThis=void(__fastcall*)(void*);
using NativePair=void(__fastcall*)(void*,void*);
using NativeReference=void*(__fastcall*)(void*,void*);
using NativeReturn=void*(__fastcall*)(void*);
using NativeBool=void(__fastcall*)(void*,bool);

int seh_filter(unsigned long code) noexcept {
    // Let C++ exceptions reach the callback's catch block, never an engine
    // stack frame. Hardware faults are attributed to this export operation.
    return code==0xe06d7363?EXCEPTION_CONTINUE_SEARCH:EXCEPTION_EXECUTE_HANDLER;
}
unsigned long copy_live(void* output,const void* input,std::size_t size) noexcept {
    __try {if(size)std::memcpy(output,input,size);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long native_this(std::uintptr_t address,void* self) {
    __try {reinterpret_cast<NativeThis>(address)(self);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long native_pair(std::uintptr_t address,void* self,void* value) {
    __try {reinterpret_cast<NativePair>(address)(self,value);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long native_reference(std::uintptr_t address,void* self,void* value) {
    __try {reinterpret_cast<NativeReference>(address)(self,value);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long native_bool(std::uintptr_t address,void* self,bool value) {
    __try {reinterpret_cast<NativeBool>(address)(self,value);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long native_return(std::uintptr_t address,void* self,void** output) {
    __try {*output=reinterpret_cast<NativeReturn>(address)(self);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
unsigned long invoke_protected(const SerializerInvoker* invoke,std::uintptr_t object,void* archive) {
    __try {(*invoke)(object,archive);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
template<class T> T read(const void* value) {
    T result{};
    if(copy_live(&result,value,sizeof(result)))throw ArchiveError("invalid_archive_memory","A live serializer value could not be read");
    return result;
}
template<class T> T state(const void* self,std::size_t offset) {
    return read<T>(static_cast<const std::uint8_t*>(self)+offset);
}
template<class T> void put(void* self,std::size_t offset,T value) noexcept {
    std::memcpy(static_cast<std::uint8_t*>(self)+offset,&value,sizeof(value));
}
struct Context {
    const Bindings* bindings{};
    const Mappers* mappers{};
    runtime_package::ExportSink sink;
    std::array<std::uintptr_t,vtable_size> original{},table{};
    std::uintptr_t object{};
    std::string code,message;
    bool failed{},diagnostic_lost{};
    unsigned long native_fault{};
    std::int64_t script_start{},script_end{};
    std::uint64_t raw_writes{},name_writes{},object_writes{},field_writes{};
    void fail(void* self,const char* error,const char* detail,unsigned long fault=0) noexcept {
        if(!failed) {
            failed=true;native_fault=fault;
            try {code=error;message=detail;}catch(...){diagnostic_lost=true;}
        }
        // Exact Shipping IsError/IsCriticalError bits at +0x29.
        std::uint8_t flags{};
        std::memcpy(&flags,static_cast<std::uint8_t*>(self)+0x29,sizeof(flags));
        put(self,0x29,static_cast<std::uint8_t>(flags|0x06));
    }
};
struct alignas(16) Frame {
    std::array<std::uint8_t,state_size> state{};
    Context* context{};
};
static_assert(offsetof(Frame,context)==state_size);
Context& context(void* self) noexcept {
    return *static_cast<Frame*>(self)->context;
}
template<class F> unsigned long action_protected(F* action,Context* ctx) {
    __try {(*action)(*ctx);return 0;}
    __except(seh_filter(GetExceptionCode())) {return GetExceptionCode();}
}
template<class F> void guarded(void* self,F&& action) noexcept {
    auto& ctx=context(self);
    if(ctx.failed)return;
    try {
        const auto fault=action_protected(&action,&ctx);
        if(fault)ctx.fail(self,"archive_callback_native_fault","A serializer mapping callback raised a hardware exception",fault);
    }
    catch(const ArchiveError& error) {ctx.fail(self,error.code.c_str(),error.what());}
    catch(const runtime_package::PackageError& error) {ctx.fail(self,error.code.c_str(),error.what());}
    catch(const std::exception& error) {ctx.fail(self,"archive_callback_failed",error.what());}
    catch(...) {ctx.fail(self,"archive_callback_failed","A serializer callback raised an unknown C++ exception");}
}
void* __fastcall name(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){
        if(!ctx.mappers->name)throw ArchiveError("name_mapper_unavailable","Package-local FName mapping is unavailable");
        const auto live=read<std::uint64_t>(value);
        const auto mapped=ctx.mappers->name(live);
        if(mapped.number!=static_cast<std::int32_t>(live>>32))
            throw ArchiveError("invalid_name_mapping","The package FName mapping changed the live name number");
        ctx.sink.name(mapped);++ctx.name_writes;
    });return self;
}
void map_object(Context& ctx,std::uintptr_t object) {
    if(!object)ctx.sink.object(0);
    else {
        if(!ctx.mappers->object)throw ArchiveError("object_mapper_unavailable","Package-local UObject mapping is unavailable");
        const auto index=ctx.mappers->object(object);
        if(!index)throw ArchiveError("unmapped_object_reference","A nonnull UObject was mapped to the null package index");
        ctx.sink.object(index);
    }
    ++ctx.object_writes;
}
void* __fastcall object(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){map_object(ctx,read<std::uintptr_t>(value));});return self;
}
void* indirect(void* self,void* value,IndirectObject kind) noexcept {
    guarded(self,[&](Context& ctx){
        if(kind==IndirectObject::object_ptr && !read<std::uint64_t>(value)) {
            map_object(ctx,0);return;
        }
        if(!ctx.mappers->indirect_object)throw ArchiveError("indirect_object_mapper_unavailable","A wrapper requires verified object-handle or weak-reference resolution");
        map_object(ctx,ctx.mappers->indirect_object(kind,reinterpret_cast<std::uintptr_t>(value)));
    });return self;
}
void* __fastcall object_ptr(void* self,void* value) noexcept {return indirect(self,value,IndirectObject::object_ptr);}
void* __fastcall weak_ptr(void* self,void* value) noexcept {return indirect(self,value,IndirectObject::weak_object_ptr);}
void* __fastcall field(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){
        const auto pointer=read<std::uintptr_t>(value);
        FieldPath mapped;
        if(pointer) {
            if(!ctx.mappers->field)throw ArchiveError("field_mapper_unavailable","Package-local FFieldPath mapping is unavailable");
            mapped=ctx.mappers->field(pointer);
            if(mapped.filtered_editor_only) {
                if(!mapped.path.empty() || mapped.owner)
                    throw ArchiveError("invalid_filtered_field","An editor-filtered FField must serialize as an empty path and null owner");
            } else if(!mapped.owner || mapped.path.empty())
                throw ArchiveError("unmapped_field_reference","A nonnull FField requires its owning UStruct and field path");
        }
        ctx.sink.field_path(mapped.path,mapped.owner);++ctx.field_writes;
    });return self;
}
void* __fastcall lazy_ptr(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){
        if(!ctx.mappers->lazy_guid)throw ArchiveError("lazy_guid_mapper_unavailable","Persistent FLazyObjectPtr serialization requires its exact GUID");
        for(const auto part:ctx.mappers->lazy_guid(reinterpret_cast<std::uintptr_t>(value)))ctx.sink.u32(part);
    });return self;
}
void* __fastcall soft_path(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){
        if(ctx.mappers->soft_path_index) {
            const auto index=ctx.mappers->soft_path_index(reinterpret_cast<std::uintptr_t>(value)).index;
            if(index<0)throw ArchiveError("unmapped_soft_path","A soft path was not harvested into the package soft-path table");
            ctx.sink.i32(index);
        } else if(ctx.bindings->soft_path_serializer) {
            const auto fault=native_pair(ctx.bindings->soft_path_serializer,value,self);
            if(fault)ctx.fail(self,"soft_path_serialization_fault","The game's soft-path serializer raised a hardware exception",fault);
        } else throw ArchiveError("soft_path_serializer_unavailable","Inline soft paths require the verified game serializer and its custom-version context");
    });return self;
}
void* __fastcall soft_ptr(void* self,void* value) noexcept {
    // Current supplied target dump and PersistentObjectPtr source agree:
    // weak cache occupies 8 bytes, followed by the 32-byte SoftObjectPath.
    return soft_path(self,static_cast<std::uint8_t*>(value)+8);
}
void* __fastcall text(void* self,void* value) noexcept {
    guarded(self,[&](Context& ctx){
        const auto fault=native_reference(ctx.original[45],self,value);
        if(fault)ctx.fail(self,"text_serialization_fault","The game's FText serializer raised a hardware exception; live text may have been modified",fault);
    });return self;
}
void __fastcall bytes(void* self,void* value,std::int64_t length) noexcept {
    guarded(self,[&](Context& ctx){
        if(length<0 || static_cast<std::uint64_t>(length)>std::numeric_limits<std::size_t>::max())
            throw ArchiveError("invalid_serialize_extent","A serializer supplied an invalid byte extent");
        if(length && !value)throw ArchiveError("invalid_serialize_pointer","A nonempty serialization write has a null source");
        std::vector<std::uint8_t> copy(static_cast<std::size_t>(length));
        if(copy_live(copy.data(),value,copy.size()))throw ArchiveError("invalid_serialize_pointer","A serializer supplied unreadable source memory");
        ctx.sink.bytes(copy);++ctx.raw_writes;
    });
}
std::int64_t __fastcall tell(void* self) noexcept {
    const auto position=context(self).sink.tell();
    return position<=static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())?static_cast<std::int64_t>(position):-1;
}
std::int64_t __fastcall size(void* self) noexcept {
    const auto extent=context(self).sink.size();
    return extent<=static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max())?static_cast<std::int64_t>(extent):-1;
}
bool __fastcall at_end(void* self) noexcept {return tell(self)>=size(self);}
void __fastcall seek(void* self,std::int64_t position) noexcept {
    guarded(self,[&](Context& ctx){
        if(position<0 || static_cast<std::uint64_t>(position)>std::numeric_limits<std::size_t>::max())
            throw ArchiveError("invalid_archive_seek","A serializer supplied an invalid seek position");
        ctx.sink.seek(static_cast<std::size_t>(position));
    });
}
void __fastcall script_start(void* self,const void* object) noexcept {
    guarded(self,[&](Context& ctx){
        if(reinterpret_cast<std::uintptr_t>(object)!=ctx.object)throw ArchiveError("nested_script_serialization","Script boundaries belong to a different export object");
        ctx.script_start=tell(self);
    });
}
void __fastcall script_end(void* self,const void* object) noexcept {
    guarded(self,[&](Context& ctx){
        if(reinterpret_cast<std::uintptr_t>(object)!=ctx.object)throw ArchiveError("nested_script_serialization","Script boundaries belong to a different export object");
        ctx.script_end=tell(self);
    });
}
bool __fastcall bulk(void* self,void*,const void*) noexcept {
    context(self).fail(self,"bulk_data_unsupported","This package backend does not publish exports with bulk data");
    // FBulkData::Serialize treats false as permission to execute its inline
    // fallback. Mark this request handled after poisoning the archive so the
    // engine returns without loading or mutating unsupported live bulk data.
    return true;
}
void __fastcall attach_editor_bulk(void* self,void*) noexcept {
    context(self).fail(self,"bulk_data_unsupported","This package backend does not attach editor bulk data");
}
void __fastcall attach_bulk(void* self,void*,void*) noexcept {
    context(self).fail(self,"bulk_data_unsupported","This package backend does not attach bulk data");
}
void __fastcall detach_bulk(void* self,void*,bool) noexcept {
    context(self).fail(self,"bulk_data_unsupported","This package backend does not detach bulk data");
}
template<class T> std::uintptr_t address(T function) noexcept {return reinterpret_cast<std::uintptr_t>(function);}
void clone(Context& ctx) {
    if(copy_live(ctx.original.data(),reinterpret_cast<const void*>(ctx.bindings->base_vtable),sizeof(ctx.original)))
        throw ArchiveError("archive_vtable_unreadable","The discovered game FArchive vtable is unreadable");
    ctx.table=ctx.original;
    ctx.table[5]=address(tell);ctx.table[6]=address(size);ctx.table[7]=address(at_end);
    // MSVC reverses the overloaded operator group. Slot 47 is Shipping
    // ForceBlueprintFinalization(), not the editor-only SerializeBool().
    ctx.table[38]=address(weak_ptr);ctx.table[39]=address(soft_path);
    ctx.table[40]=address(soft_ptr);ctx.table[41]=address(object_ptr);
    ctx.table[42]=address(lazy_ptr);ctx.table[43]=address(field);
    ctx.table[44]=address(object);ctx.table[45]=address(text);ctx.table[46]=address(name);
    ctx.table[48]=address(bytes);ctx.table[54]=address(seek);
    // Both bulk overloads are declared even without editor-only data. MSVC
    // reverses each overload group: editor bulk precedes Owner/FBulkData.
    ctx.table[55]=address(attach_editor_bulk);ctx.table[56]=address(attach_bulk);
    ctx.table[57]=address(detach_bulk);ctx.table[58]=address(detach_bulk);ctx.table[59]=address(bulk);
    ctx.table[66]=address(script_start);ctx.table[67]=address(script_end);
}
void capture_custom_versions(void* self,Context& ctx,Result& result) {
    void* container{};
    if(native_return(ctx.original[12],self,&container))throw ArchiveError("custom_versions_fault","The game's custom-version container could not be obtained");
    if(!container)throw ArchiveError("custom_versions_unavailable","The game returned a null custom-version container");
    const auto data=state<std::uintptr_t>(container,0);
    const auto count=state<std::int32_t>(container,8),capacity=state<std::int32_t>(container,12);
    if(count<0 || capacity<count || (count && !data))throw ArchiveError("invalid_custom_versions","The game's custom-version array has invalid bounds");
    // Exact GetVersion binary advances by 40 bytes (Key16,Version4,
    // ReferenceCount4,Validator8,FName8) in this Shipping build.
    result.custom_versions.reserve(static_cast<std::size_t>(count));
    for(std::int32_t i=0;i<count;++i) {
        const auto entry=reinterpret_cast<const void*>(data+static_cast<std::size_t>(i)*40);
        runtime_package::CustomVersion version;
        version.guid=read<std::array<std::uint32_t,4>>(entry);
        version.version=state<std::int32_t>(entry,16);
        result.custom_versions.push_back(version);
    }
}
} // namespace

Result serialize(std::uintptr_t object,const SerializerInvoker& invoke,const Bindings& bindings,
                 const Mappers& mappers,const Options& options) {
    if(!object || !invoke)throw ArchiveError("serializer_unavailable","A live object and verified serializer invoker are required");
    if(!bindings.state_constructor || !bindings.state_destructor || !bindings.base_vtable)
        throw ArchiveError("archive_bindings_unavailable","The exact game archive constructor, destructor and vtable are required");
    Context ctx;ctx.bindings=&bindings;ctx.mappers=&mappers;ctx.object=object;
    clone(ctx);
    Frame frame;frame.context=&ctx;
    if(native_this(bindings.state_constructor,&frame))
        throw ArchiveError("archive_construction_fault","The game's archive state constructor raised a hardware exception; partial allocation cleanup is unconfirmed");
    bool constructed=true;
    try {
        put(&frame,0,reinterpret_cast<std::uintptr_t>(ctx.table.data()));
        if(native_bool(ctx.original[26],&frame,true) || native_bool(ctx.original[32],&frame,true)
           || native_bool(ctx.original[16],&frame,options.filter_editor_only)
           || native_bool(ctx.original[30],&frame,options.unversioned_properties))
            throw ArchiveError("archive_configuration_fault","The game's archive setters raised a hardware exception");
        put(&frame,0x30,options.serializing_defaults);put(&frame,0x34,options.port_flags);
        const auto fault=invoke_protected(&invoke,object,&frame);
        if(fault)ctx.fail(&frame,"object_serialization_fault","The live UObject serializer raised a hardware exception; object effects are unconfirmed",fault);
        if(ctx.failed)throw ArchiveError(ctx.diagnostic_lost?"archive_callback_failed":ctx.code,
            ctx.diagnostic_lost?"Serialization failed and diagnostic allocation also failed":ctx.message);
        if(state<std::uint8_t>(&frame,0x29)&0x06)
            throw ArchiveError("engine_archive_error","The live serializer marked the archive as erroneous");
        Result result;
        result.ue4_version=state<std::int32_t>(&frame,0x40);result.ue5_version=state<std::int32_t>(&frame,0x44);
        result.licensee_version=state<std::int32_t>(&frame,0x48);
        result.engine_version.major=state<std::uint16_t>(&frame,0x4c);
        result.engine_version.minor=state<std::uint16_t>(&frame,0x4e);
        result.engine_version.patch=state<std::uint16_t>(&frame,0x50);
        result.engine_version.changelist=state<std::uint32_t>(&frame,0x54);
        capture_custom_versions(&frame,ctx,result);
        if(ctx.script_end>ctx.script_start) {
            result.script_serialization_start=ctx.script_start;result.script_serialization_end=ctx.script_end;
        } else if(ctx.script_start || ctx.script_end) {
            throw ArchiveError("invalid_script_serialization_range","An export has an empty or reversed script-serialization range");
        }
        result.raw_writes=ctx.raw_writes;result.name_writes=ctx.name_writes;
        result.object_writes=ctx.object_writes;result.field_writes=ctx.field_writes;
        result.data=ctx.sink.take();
        // Direct state destruction releases game allocations but never frees
        // bridge-owned storage through the deleting destructor at slot 0.
        constructed=false;
        const auto destroy_fault=native_this(bindings.state_destructor,&frame);
        if(destroy_fault)throw ArchiveError("archive_destruction_fault","The game archive destructor faulted; custom-version allocation cleanup is unconfirmed");
        return result;
    } catch(const ArchiveError& error) {
        if(constructed && native_this(bindings.state_destructor,&frame))
            throw ArchiveError(error.code,std::string(error.what())+"; archive allocation cleanup also raised a hardware exception");
        throw;
    } catch(...) {
        if(constructed && native_this(bindings.state_destructor,&frame))
            throw ArchiveError("archive_cleanup_fault","Serialization failed and game archive allocation cleanup is unconfirmed");
        throw;
    }
}
} // namespace sn2_mcp::runtime_archive
