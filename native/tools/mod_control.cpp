#include <Windows.h>
#include <TlHelp32.h>
#include <bcrypt.h>
#include <nlohmann/json.hpp>
#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <vector>
#include "../include/sn2_mcp/process_identity.hpp"
#include "../include/sn2_mod/command.hpp"

// Adapted from the copied G1R/SN2 bridge loader. Mod initialization is a
// separate export command; module presence alone does not establish activation.
namespace fs = std::filesystem;
using json = nlohmann::json;
namespace {
struct Handle { HANDLE value{}; ~Handle(){if(value && value!=INVALID_HANDLE_VALUE)CloseHandle(value);} };
struct RemoteMemory {
    HANDLE process{}; void* address{}; bool retain{};
    RemoteMemory(HANDLE selected,std::size_t size):process(selected),address(VirtualAllocEx(selected,nullptr,size,MEM_COMMIT|MEM_RESERVE,PAGE_READWRITE)) {
        if(!address)throw std::runtime_error("remote_allocation_failed");
    }
    ~RemoteMemory(){if(address && !retain)VirtualFreeEx(process,address,0,MEM_RELEASE);}
};
std::wstring normalized(const fs::path& path){return sn2::loader::comparable_path(fs::canonical(path).wstring());}
std::string utf8(const std::wstring& text) {
    const auto size=WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),nullptr,0,nullptr,nullptr);
    if(size<=0)throw std::runtime_error("path_encoding_failed");
    std::string result(size,'\0');WideCharToMultiByte(CP_UTF8,WC_ERR_INVALID_CHARS,text.data(),static_cast<int>(text.size()),result.data(),size,nullptr,nullptr);return result;
}
std::uint64_t number(const std::wstring& text) {
    if(text.empty())throw std::runtime_error("invalid_arguments");std::uint64_t result{};
    for(auto value:text){if(value<L'0'||value>L'9'||result>(UINT64_MAX-(value-L'0'))/10)throw std::runtime_error("invalid_arguments");result=result*10+value-L'0';}
    return result;
}
std::vector<std::uint8_t> file_bytes(HANDLE file) {
    LARGE_INTEGER size{};if(!GetFileSizeEx(file,&size)||size.QuadPart<=0||static_cast<std::uint64_t>(size.QuadPart)>SIZE_MAX)throw std::runtime_error("file_size_failed");
    std::vector<std::uint8_t> result(static_cast<std::size_t>(size.QuadPart));std::size_t received{};
    while(received<result.size()){DWORD count{};const auto part=static_cast<DWORD>(std::min<std::size_t>(result.size()-received,MAXDWORD));
        if(!ReadFile(file,result.data()+received,part,&count,nullptr)||!count)throw std::runtime_error("file_read_failed");received+=count;}
    return result;
}
std::string sha256(const std::vector<std::uint8_t>& bytes) {
    BCRYPT_ALG_HANDLE algorithm{};BCRYPT_HASH_HANDLE hash{};
    if(BCryptOpenAlgorithmProvider(&algorithm,BCRYPT_SHA256_ALGORITHM,nullptr,0)<0)throw std::runtime_error("hash_failed");
    struct Cleanup {BCRYPT_ALG_HANDLE a;BCRYPT_HASH_HANDLE& h;~Cleanup(){if(h)BCryptDestroyHash(h);BCryptCloseAlgorithmProvider(a,0);}} cleanup{algorithm,hash};
    DWORD size{},received{};if(BCryptGetProperty(algorithm,BCRYPT_OBJECT_LENGTH,reinterpret_cast<PUCHAR>(&size),sizeof(size),&received,0)<0)throw std::runtime_error("hash_failed");
    std::vector<std::uint8_t> object(size);if(BCryptCreateHash(algorithm,&hash,object.data(),size,nullptr,0,0)<0)throw std::runtime_error("hash_failed");
    for(std::size_t offset=0;offset<bytes.size();){const auto count=static_cast<ULONG>(std::min<std::size_t>(bytes.size()-offset,std::numeric_limits<ULONG>::max()));
        if(BCryptHashData(hash,const_cast<PUCHAR>(bytes.data()+offset),count,0)<0)throw std::runtime_error("hash_failed");offset+=count;}
    std::uint8_t digest[32]{};if(BCryptFinishHash(hash,digest,sizeof(digest),0)<0)throw std::runtime_error("hash_failed");
    const char* digits="0123456789abcdef";std::string result;for(auto value:digest){result+=digits[value>>4];result+=digits[value&15];}return result;
}
struct Image {
    std::vector<std::uint8_t> mapped;IMAGE_NT_HEADERS64 headers{};std::vector<IMAGE_SECTION_HEADER> sections;
    template<class T>T at(std::size_t offset)const{if(offset>mapped.size()||sizeof(T)>mapped.size()-offset)throw std::runtime_error("invalid_pe");T value{};std::memcpy(&value,mapped.data()+offset,sizeof(T));return value;}
    std::string string_at(DWORD rva)const{if(rva>=mapped.size())throw std::runtime_error("invalid_pe");const auto begin=mapped.begin()+rva;auto end=std::find(begin,mapped.end(),0);if(end==mapped.end())throw std::runtime_error("invalid_pe");return std::string(begin,end);}
    explicit Image(const std::vector<std::uint8_t>& raw) {
        auto read=[&]<class T>(std::size_t offset){if(offset>raw.size()||sizeof(T)>raw.size()-offset)throw std::runtime_error("invalid_pe");T value{};std::memcpy(&value,raw.data()+offset,sizeof(T));return value;};
        auto dos=read.operator()<IMAGE_DOS_HEADER>(0);if(dos.e_magic!=IMAGE_DOS_SIGNATURE||dos.e_lfanew<sizeof(dos))throw std::runtime_error("invalid_pe");
        headers=read.operator()<IMAGE_NT_HEADERS64>(dos.e_lfanew);
        if(headers.Signature!=IMAGE_NT_SIGNATURE||headers.FileHeader.Machine!=IMAGE_FILE_MACHINE_AMD64||headers.OptionalHeader.Magic!=IMAGE_NT_OPTIONAL_HDR64_MAGIC
            ||!(headers.FileHeader.Characteristics&IMAGE_FILE_DLL)||!headers.OptionalHeader.SizeOfImage||headers.OptionalHeader.SizeOfHeaders>raw.size())throw std::runtime_error("not_x64_dll");
        mapped.resize(headers.OptionalHeader.SizeOfImage);
        if(headers.OptionalHeader.SizeOfHeaders>mapped.size())throw std::runtime_error("invalid_pe");
        std::copy_n(raw.begin(),headers.OptionalHeader.SizeOfHeaders,mapped.begin());
        auto table=static_cast<std::size_t>(dos.e_lfanew)+sizeof(DWORD)+sizeof(IMAGE_FILE_HEADER)+headers.FileHeader.SizeOfOptionalHeader;
        for(unsigned index=0;index<headers.FileHeader.NumberOfSections;++index){auto section=read.operator()<IMAGE_SECTION_HEADER>(table+index*sizeof(IMAGE_SECTION_HEADER));
            if(section.PointerToRawData>raw.size()||section.SizeOfRawData>raw.size()-section.PointerToRawData||section.VirtualAddress>mapped.size()||section.SizeOfRawData>mapped.size()-section.VirtualAddress)throw std::runtime_error("invalid_pe");
            std::copy_n(raw.begin()+section.PointerToRawData,section.SizeOfRawData,mapped.begin()+section.VirtualAddress);sections.push_back(section);}
    }
    DWORD command_export()const {
        const auto directory=headers.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_EXPORT];if(!directory.VirtualAddress||!directory.Size)throw std::runtime_error("mod_export_missing");
        const auto exports=at<IMAGE_EXPORT_DIRECTORY>(directory.VirtualAddress);
        for(DWORD index=0;index<exports.NumberOfNames;++index){const auto name=at<DWORD>(exports.AddressOfNames+static_cast<std::size_t>(index)*4);
            if(string_at(name)!="SN2ModCommand")continue;const auto ordinal=at<WORD>(exports.AddressOfNameOrdinals+static_cast<std::size_t>(index)*2);
            if(ordinal>=exports.NumberOfFunctions)throw std::runtime_error("invalid_pe");const auto rva=at<DWORD>(exports.AddressOfFunctions+static_cast<std::size_t>(ordinal)*4);
            if(rva>=directory.VirtualAddress&&rva-directory.VirtualAddress<directory.Size)throw std::runtime_error("mod_export_forwarded");
            for(const auto& section:sections)if(section.Characteristics&IMAGE_SCN_MEM_EXECUTE){const auto size=std::max(section.Misc.VirtualSize,section.SizeOfRawData);
                if(rva>=section.VirtualAddress&&rva-section.VirtualAddress<size)return rva;}
            throw std::runtime_error("mod_export_not_executable");}
        throw std::runtime_error("mod_export_missing");
    }
    void relocate(std::uintptr_t base) {
        const auto delta=static_cast<std::uint64_t>(base)-headers.OptionalHeader.ImageBase;if(!delta)return;
        const auto directory=headers.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_BASERELOC];if(!directory.VirtualAddress||!directory.Size)throw std::runtime_error("relocations_missing");
        if(directory.VirtualAddress>mapped.size()||directory.Size>mapped.size()-directory.VirtualAddress)throw std::runtime_error("invalid_pe");
        for(std::size_t offset=0;offset<directory.Size;){const auto block=at<IMAGE_BASE_RELOCATION>(directory.VirtualAddress+offset);
            if(block.SizeOfBlock<sizeof(block)||block.SizeOfBlock>directory.Size-offset||(block.SizeOfBlock-sizeof(block))%sizeof(WORD))throw std::runtime_error("invalid_pe");
            for(std::size_t entry=sizeof(block);entry<block.SizeOfBlock;entry+=2){const auto fix=at<WORD>(directory.VirtualAddress+offset+entry);const auto type=fix>>12;
                if(type==IMAGE_REL_BASED_ABSOLUTE)continue;if(type!=IMAGE_REL_BASED_DIR64)throw std::runtime_error("unsupported_relocation");
                const auto position=static_cast<std::size_t>(block.VirtualAddress)+(fix&0xfff);auto value=at<std::uint64_t>(position)+delta;std::memcpy(mapped.data()+position,&value,sizeof(value));}
            offset+=block.SizeOfBlock;}
    }
};
std::vector<MODULEENTRY32W> modules(DWORD pid) {
    Handle snapshot{CreateToolhelp32Snapshot(TH32CS_SNAPMODULE|TH32CS_SNAPMODULE32,pid)};if(snapshot.value==INVALID_HANDLE_VALUE)throw std::runtime_error("module_snapshot_failed");
    MODULEENTRY32W entry{};entry.dwSize=sizeof(entry);std::vector<MODULEENTRY32W> result;
    if(!Module32FirstW(snapshot.value,&entry))throw std::runtime_error("module_snapshot_empty");do{result.push_back(entry);entry.dwSize=sizeof(entry);}while(Module32NextW(snapshot.value,&entry));return result;
}
std::uintptr_t mod_base(DWORD pid,const fs::path& dll) {
    const auto path=normalized(dll);std::uintptr_t result{};for(const auto& entry:modules(pid)){
        if(_wcsicmp(entry.szModule,dll.filename().c_str())!=0)continue;
        if(normalized(entry.szExePath)!=path)throw std::runtime_error("other_mod_loaded");result=reinterpret_cast<std::uintptr_t>(entry.modBaseAddr);}
    return result;
}
void read_remote(HANDLE process,void* address,void* output,std::size_t size) {
    SIZE_T received{};if(!ReadProcessMemory(process,address,output,size,&received)||received!=size)throw std::runtime_error("remote_read_failed");
}
void write_remote(HANDLE process,void* address,const void* input,std::size_t size) {
    SIZE_T written{};if(!WriteProcessMemory(process,address,input,size,&written)||written!=size)throw std::runtime_error("remote_write_failed");
}
void verify_remote(HANDLE process,std::uintptr_t base,Image expected) {
    IMAGE_DOS_HEADER dos{};read_remote(process,reinterpret_cast<void*>(base),&dos,sizeof(dos));if(dos.e_magic!=IMAGE_DOS_SIGNATURE||dos.e_lfanew<sizeof(dos))throw std::runtime_error("remote_image_mismatch");
    IMAGE_NT_HEADERS64 header{};read_remote(process,reinterpret_cast<void*>(base+dos.e_lfanew),&header,sizeof(header));
    if(header.Signature!=IMAGE_NT_SIGNATURE||header.FileHeader.Machine!=expected.headers.FileHeader.Machine||header.FileHeader.TimeDateStamp!=expected.headers.FileHeader.TimeDateStamp
       ||header.OptionalHeader.SizeOfImage!=expected.headers.OptionalHeader.SizeOfImage)throw std::runtime_error("remote_image_mismatch");
    expected.relocate(base);
    const auto iat=expected.headers.OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IAT];
    for(const auto& section:expected.sections){if((section.Characteristics&IMAGE_SCN_MEM_WRITE)&&!(section.Characteristics&IMAGE_SCN_MEM_EXECUTE))continue;
        const auto size=std::max(section.Misc.VirtualSize,section.SizeOfRawData);if(section.VirtualAddress>expected.mapped.size()||size>expected.mapped.size()-section.VirtualAddress)throw std::runtime_error("invalid_pe");
        std::vector<std::uint8_t> actual(size);read_remote(process,reinterpret_cast<void*>(base+section.VirtualAddress),actual.data(),size);
        for(std::size_t offset=0;offset<size;++offset){const auto rva=static_cast<std::size_t>(section.VirtualAddress)+offset;
            // Import slots are fixed up by the target loader, while immutable
            // source/revision constants, code, unwind data and resources must
            // still match the pinned artifact after base relocations.
            if(iat.VirtualAddress&&rva>=iat.VirtualAddress&&rva-iat.VirtualAddress<iat.Size)continue;
            if(actual[offset]!=expected.mapped[rva])throw std::runtime_error("loaded_mod_code_mismatch");}}
}
bool wait_thread(HANDLE thread,std::chrono::steady_clock::time_point deadline) {
    while(true){const auto left=std::chrono::duration_cast<std::chrono::milliseconds>(deadline-std::chrono::steady_clock::now()).count();
        const auto wait=WaitForSingleObject(thread,left>0?static_cast<DWORD>(std::min<std::uint64_t>(left,MAXDWORD-1)):0);
        if(wait==WAIT_OBJECT_0)return true;if(wait==WAIT_FAILED)throw std::runtime_error("thread_wait_failed");if(std::chrono::steady_clock::now()>=deadline)return false;}
}
std::uintptr_t remote_load_library(HANDLE process,DWORD pid) {
    const auto local=GetProcAddress(GetModuleHandleW(L"kernel32.dll"),"LoadLibraryW");MEMORY_BASIC_INFORMATION memory{};
    if(!local||!VirtualQuery(local,&memory,sizeof(memory)))throw std::runtime_error("loader_address_failed");
    auto owner=static_cast<HMODULE>(memory.AllocationBase);std::wstring text(32768,L'\0');const auto count=GetModuleFileNameW(owner,text.data(),static_cast<DWORD>(text.size()));if(!count||count>=text.size())throw std::runtime_error("loader_address_failed");text.resize(count);
    const auto owner_path=normalized(text);const auto offset=reinterpret_cast<std::uintptr_t>(local)-reinterpret_cast<std::uintptr_t>(owner);
    for(const auto& entry:modules(pid))if(_wcsicmp(entry.szModule,fs::path(owner_path).filename().c_str())==0){
        if(normalized(entry.szExePath)!=owner_path||offset>=entry.modBaseSize)throw std::runtime_error("loader_module_mismatch");
        IMAGE_DOS_HEADER local_dos=*reinterpret_cast<IMAGE_DOS_HEADER*>(owner);IMAGE_NT_HEADERS64 remote_header{};
        read_remote(process,entry.modBaseAddr+local_dos.e_lfanew,&remote_header,sizeof(remote_header));auto local_header=reinterpret_cast<IMAGE_NT_HEADERS64*>(reinterpret_cast<std::uint8_t*>(owner)+local_dos.e_lfanew);
        if(remote_header.Signature!=IMAGE_NT_SIGNATURE||remote_header.FileHeader.TimeDateStamp!=local_header->FileHeader.TimeDateStamp||remote_header.OptionalHeader.SizeOfImage!=local_header->OptionalHeader.SizeOfImage)throw std::runtime_error("loader_module_mismatch");
        return reinterpret_cast<std::uintptr_t>(entry.modBaseAddr)+offset;}
    throw std::runtime_error("loader_module_missing");
}
}

int wmain(int argc,wchar_t** argv) {
    SetConsoleOutputCP(CP_UTF8);json receipt={{"ok",false},{"outcome_unknown",false},{"module_loaded",false}};bool effect_started{};
    try {
        DWORD pid{};fs::path dll,executable;std::uint64_t created{},request_id{},timeout_ms{};std::string expected_hash,operation;std::vector<std::wstring> seen;
        for(int index=1;index<argc;index+=2){if(index+1>=argc)throw std::runtime_error("invalid_arguments");std::wstring key=argv[index],value=argv[index+1];
            if(std::find(seen.begin(),seen.end(),key)!=seen.end())throw std::runtime_error("invalid_arguments");seen.push_back(key);
            if(key==L"--pid"){auto parsed=number(value);if(!parsed||parsed>MAXDWORD)throw std::runtime_error("invalid_arguments");pid=static_cast<DWORD>(parsed);}
            else if(key==L"--dll")dll=value;else if(key==L"--expected-exe")executable=value;
            else if(key==L"--expected-creation-filetime")created=number(value);else if(key==L"--request-id")request_id=number(value);
            else if(key==L"--timeout-ms")timeout_ms=number(value);else if(key==L"--expected-sha256")expected_hash=utf8(value);
            else if(key==L"--operation")operation=utf8(value);else throw std::runtime_error("invalid_arguments");}
        if(!pid||!created||!request_id||!timeout_ms||!dll.is_absolute()||!executable.is_absolute()||!fs::is_regular_file(dll)
            ||expected_hash.size()!=64||expected_hash.find_first_not_of("0123456789abcdef")!=std::string::npos
            ||(operation!="start"&&operation!="status"&&operation!="disable"))throw std::runtime_error("invalid_arguments");
        receipt.update({{"operation",operation},{"request_id",request_id},{"dll_path",utf8(fs::canonical(dll).wstring())},{"dll_sha256",expected_hash},
            {"process",{{"pid",pid},{"creation_time",std::to_string(created)},{"executable",utf8(executable.wstring())}}}});
        Handle pin{CreateFileW(dll.c_str(),GENERIC_READ,FILE_SHARE_READ,nullptr,OPEN_EXISTING,FILE_ATTRIBUTE_NORMAL,nullptr)};
        if(pin.value==INVALID_HANDLE_VALUE)throw std::runtime_error("dll_pin_failed");auto bytes=file_bytes(pin.value);if(sha256(bytes)!=expected_hash)throw std::runtime_error("dll_changed");
        Image image(bytes);const auto export_rva=image.command_export();
        Handle process{OpenProcess(PROCESS_CREATE_THREAD|PROCESS_QUERY_INFORMATION|PROCESS_VM_OPERATION|PROCESS_VM_WRITE|PROCESS_VM_READ,FALSE,pid)};
        if(!process.value)throw std::runtime_error("open_process_failed");
        const auto path=sn2::loader::process_image(process.value);if(!sn2::loader::identity_matches(path,sn2::loader::creation_filetime(process.value),fs::canonical(executable).wstring(),created))throw std::runtime_error("process_identity_mismatch");
        if(!sn2::loader::comparable_path(path).ends_with(L"\\subnautica2\\binaries\\win64\\subnautica2-win64-shipping.exe"))throw std::runtime_error("wrong_process");
        USHORT process_machine{},native_machine{};if(!IsWow64Process2(process.value,&process_machine,&native_machine)||process_machine!=IMAGE_FILE_MACHINE_UNKNOWN||native_machine!=IMAGE_FILE_MACHINE_AMD64)throw std::runtime_error("wrong_architecture");
        const auto max_duration=std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::time_point::max()-std::chrono::steady_clock::now()).count();
        if(timeout_ms>static_cast<std::uint64_t>(max_duration))throw std::runtime_error("timeout_not_representable");
        const auto deadline=std::chrono::steady_clock::now()+std::chrono::milliseconds(timeout_ms);auto base=mod_base(pid,dll);
        if(!base&&operation!="start"){receipt.update({{"ok",true},{"state","not_loaded"},{"residency","process_lifetime"}});std::cout<<receipt.dump()<<"\n";return 0;}
        if(!base){const auto path_text=fs::canonical(dll).wstring();RemoteMemory remote(process.value,(path_text.size()+1)*sizeof(wchar_t));write_remote(process.value,remote.address,path_text.c_str(),(path_text.size()+1)*sizeof(wchar_t));
            Handle thread{CreateRemoteThread(process.value,nullptr,0,reinterpret_cast<LPTHREAD_START_ROUTINE>(remote_load_library(process.value,pid)),remote.address,0,nullptr)};
            if(!thread.value)throw std::runtime_error("load_thread_failed");effect_started=true;remote.retain=true;
            if(!wait_thread(thread.value,deadline)){remote.retain=true;receipt["remote_memory_retained"]=true;throw std::runtime_error("load_outcome_unknown");}
            remote.retain=false;
            base=mod_base(pid,dll);if(!base)throw std::runtime_error("mod_load_failed");receipt["load_state"]="loaded";
        }else receipt["load_state"]="already_loaded";
        receipt["module_loaded"]=true;receipt["module_base"]=base;verify_remote(process.value,base,image);receipt["loaded_code_verified"]=true;
        auto selected=operation=="start"?sn2_mod::Operation::start:operation=="disable"?sn2_mod::Operation::disable:sn2_mod::Operation::status;
        std::uint64_t capacity=65536;json mod_receipt;
        while(true){if(capacity>SIZE_MAX)throw std::runtime_error("response_size_not_representable");RemoteMemory output(process.value,static_cast<std::size_t>(capacity));RemoteMemory remote(process.value,sizeof(sn2_mod::Command));
            sn2_mod::Command command{};command.size=sizeof(command);command.abi=1;command.operation=static_cast<std::uint32_t>(selected);command.expected_pid=pid;command.expected_creation_filetime=created;command.request_id=request_id;command.response_address=reinterpret_cast<std::uint64_t>(output.address);command.response_capacity=capacity;
            write_remote(process.value,remote.address,&command,sizeof(command));Handle thread{CreateRemoteThread(process.value,nullptr,0,reinterpret_cast<LPTHREAD_START_ROUTINE>(base+export_rva),remote.address,0,nullptr)};
            if(!thread.value)throw std::runtime_error("command_thread_failed");effect_started=true;remote.retain=output.retain=true;
            if(!wait_thread(thread.value,deadline)){remote.retain=output.retain=true;receipt["remote_memory_retained"]=true;throw std::runtime_error("command_outcome_unknown");}
            remote.retain=output.retain=false;
            read_remote(process.value,remote.address,&command,sizeof(command));receipt["command_result_code"]=command.result_code;
            if(command.result_code==static_cast<std::uint32_t>(sn2_mod::Result::response_too_small)){
                if(command.response_required<=capacity)throw std::runtime_error("invalid_response_size");capacity=command.response_required;selected=sn2_mod::Operation::status;continue;}
            if(command.result_code==static_cast<std::uint32_t>(sn2_mod::Result::invalid_request))throw std::runtime_error("mod_request_rejected");
            if(command.response_size>capacity)throw std::runtime_error("invalid_response_size");std::string text(static_cast<std::size_t>(command.response_size),'\0');if(!text.empty())read_remote(process.value,output.address,text.data(),text.size());
            mod_receipt=json::parse(text);receipt["mod_receipt"]=mod_receipt;
            if(command.result_code!=static_cast<std::uint32_t>(sn2_mod::Result::ok))throw std::runtime_error("mod_command_failed");break;}
        receipt.update({{"ok",true},{"outcome_unknown",false},{"state","command_completed"},{"residency","process_lifetime"},{"hot_unload_supported",false}});
        std::cout<<receipt.dump()<<"\n";return 0;
    }catch(const std::exception& error){std::string code=error.what();if(code.empty()||code.find_first_not_of("abcdefghijklmnopqrstuvwxyz_")!=std::string::npos)code="mod_control_failed";
        const bool unknown=effect_started&&(code=="command_outcome_unknown"||code=="load_outcome_unknown"||code=="thread_wait_failed"||code=="remote_read_failed"||code=="mod_control_failed"||code=="mod_command_failed"||code=="invalid_response_size");
        receipt.update({{"code",code},{"message","The selected native modification command was not confirmed; inspect the complete receipt"},{"outcome_unknown",unknown},{"state",unknown?"outcome_unknown":"failed"}});
        std::cout<<receipt.dump()<<"\n";return unknown?3:2;}
}
