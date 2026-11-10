#include <sn2_mcp/aligned_parameter_buffer.hpp>

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <limits>
#include <memory>
#include <new>
#include <stdexcept>
#include <utility>
#include <vector>

namespace {
using sn2_mcp::aligned_parameter_buffer::Buffer;
using nlohmann::json;
int checks{};

void require(bool condition,const char* message) {
    ++checks;
    if(!condition)throw std::runtime_error(message);
}

void reject_extent(std::size_t extent) {
    try {Buffer rejected(extent);}
    catch(const sn2_mcp::HandlerError& error) {
        require(error.code()=="unsupported_signature","Неверный код отказа размера буфера");
        require(error.details()==json{{"executed",false},{"may_have_executed",false},{"do_not_retry",false}},
                "Неверный extent должен отвергаться до исполнения");
        return;
    }
    throw std::runtime_error("Ожидался отказ непредставимого extent");
}

void require_alignment(const Buffer& buffer) {
    require(reinterpret_cast<std::uintptr_t>(buffer.data())%16==0,
            "Реальный адрес параметров должен иметь выравнивание 16");
}

void check_zero_storage(std::size_t extent) {
    const auto rounded=(extent/16+(extent%16!=0))*16;
    // Грязная память объекта и canaries проверяют placement lifecycle.
    // Отдельно читается вся динамическая память, включая округлённый padding.
    alignas(Buffer) std::array<unsigned char,sizeof(Buffer)+64> guarded;
    guarded.fill(0xa5);
    auto* buffer=std::construct_at(reinterpret_cast<Buffer*>(guarded.data()+32),extent);
    require_alignment(*buffer);
    require(buffer->size()==extent,"Размер буфера должен равняться фактическому extent");
    require(buffer->bytes().size()==extent,"Span должен ограничиваться текущим extent");
    const Buffer& readonly=*buffer;
    require(readonly.bytes().size()==extent,"Const span должен иметь тот же ограниченный extent");
    require(!rounded || std::all_of(readonly.data(),readonly.data()+rounded,[](unsigned char value){return value==0;}),
            "Вся округлённая память, включая padding за extent, должна быть нулевой");

    const auto canaries_unchanged=[&] {
        return std::all_of(guarded.begin(),guarded.begin()+32,[](unsigned char value){return value==0xa5;})
            && std::all_of(guarded.begin()+32+sizeof(Buffer),guarded.end(),[](unsigned char value){return value==0xa5;});
    };
    require(canaries_unchanged(),"Конструктор не должен писать за границы Buffer");
    if(extent) {
        buffer->bytes().front()=std::byte{0x11};
        buffer->bytes().back()=std::byte{0x22};
        require(std::all_of(readonly.data()+extent,readonly.data()+rounded,[](unsigned char value){return value==0;}),
                "Запись в разрешённый span не должна изменять padding за extent");
        require(readonly.data()[extent-1]==0x22,"Последний разрешённый байт должен сохранять запись");
        if(extent>1)require(readonly.data()[0]==0x11,"Первый разрешённый байт должен сохранять независимую запись");
    }
    require(canaries_unchanged(),"Запись на границах span не должна изменять внешние canaries");
    std::destroy_at(buffer);
    require(canaries_unchanged(),"Завершение жизни Buffer не должно изменять canaries");
}
}

int main() {
    try {
        for(const auto extent:std::array<std::size_t,8>{0,1,15,4095,4096,4097,8193,65535})check_zero_storage(extent);
        for(const auto extent:std::array<std::size_t,3>{std::numeric_limits<std::size_t>::max()-14,
                std::numeric_limits<std::size_t>::max()-1,std::numeric_limits<std::size_t>::max()})
            reject_extent(extent);

        Buffer source(8193);
        source.bytes().front()=std::byte{0x31};
        source.bytes().back()=std::byte{0x73};
        Buffer copied(source);
        require_alignment(copied);
        require(copied.data()!=source.data(),"Copy construction должен владеть независимым хранилищем");
        require(copied.size()==8193 && std::equal(source.data(),source.data()+8208,copied.data()),
                "Copy construction должен сохранять фактический extent и округлённую память");
        copied.bytes().front()=std::byte{0x44};
        source.bytes().back()=std::byte{0x55};
        require(source.data()[0]==0x31 && copied.data()[8192]==0x73,
                "Изменение копии и исходника не должно затрагивать другое хранилище");

        Buffer assigned(1);
        assigned=source;
        require_alignment(assigned);
        require(assigned.bytes().size()==8193 && assigned.data()!=source.data(),
                "Copy assignment должен переносить extent без alias исходного буфера");
        assigned.bytes()[8192]=std::byte{0x66};
        require(source.data()[8192]==0x55,"Запись assignment-копии должна оставаться независимой");
        assigned=assigned;
        require(assigned.size()==8193 && assigned.data()[8192]==0x66,
                "Self assignment должен сохранять extent и данные");

        Buffer small(17);
        assigned=small;
        require_alignment(assigned);
        require(assigned.size()==17 && assigned.data()!=small.data()
                && std::all_of(assigned.data(),assigned.data()+32,[](unsigned char value){return value==0;}),
                "Уменьшение assignment должно сохранять только нужную округлённую нулевую память");
        Buffer empty(0);
        assigned=empty;
        require(assigned.size()==0 && assigned.bytes().empty(),"Assignment пустого буфера должен очищать extent");

        Buffer moved(std::move(source));
        require_alignment(moved);
        require(moved.size()==source.size() && moved.data()!=source.data()
                && std::equal(source.data(),source.data()+8208,moved.data()),
                "Rvalue construction должен сохранять прежнюю семантику независимой копии");
        Buffer move_assigned(1);
        move_assigned=std::move(moved);
        require_alignment(move_assigned);
        require(moved.size()==8193 && move_assigned.size()==8193 && moved.data()!=move_assigned.data(),
                "Rvalue assignment должен сохранять исходный буфер и переносить независимую копию");
        move_assigned.bytes().back()=std::byte{0x77};
        require(moved.data()[8192]==0x55,"Запись rvalue-копии не должна изменять исходное хранилище");

        Buffer scalar_frame(8201);
        const std::uint64_t scalar=0x0123456789abcdef;
        const auto scalar_offset=scalar_frame.size()-sizeof(scalar);
        std::memcpy(scalar_frame.data()+scalar_offset,&scalar,sizeof(scalar));
        std::uint64_t read_back{};
        std::memcpy(&read_back,scalar_frame.data()+scalar_offset,sizeof(read_back));
        require_alignment(scalar_frame);
        require(scalar_offset>4096 && read_back==scalar && scalar_frame.bytes().size()==8201
                && std::all_of(scalar_frame.data()+8201,scalar_frame.data()+8208,[](unsigned char value){return value==0;}),
                "Последний scalar за 4096 байт должен помещаться целиком и сохранять нулевой padding");

        // Реальное размещение в контейнере и перенос его allocator также
        // должны сохранять адреса, пригодные для параметров SIMD/FTransform.
        std::vector<Buffer> buffers;
        buffers.reserve(1);
        buffers.emplace_back(8193);
        buffers[0].bytes()[8192]=std::byte{0x7f};
        buffers.emplace_back(1);
        buffers.emplace_back(65535);
        buffers.emplace_back(0);
        for(const auto& buffer:buffers)require_alignment(buffer);
        require(buffers[0].data()[8192]==0x7f,"Перенос vector должен сохранять полную копию параметров");
        require(std::all_of(buffers[1].data(),buffers[1].data()+16,[](unsigned char value){return value==0;}),
                "Перенос соседнего буфера не должен загрязнять нулевую ёмкость");
        require(buffers[0].data()!=buffers[1].data() && buffers[0].data()!=buffers[2].data()
                && buffers[1].data()!=buffers[2].data(),
                "Отдельные буферы в vector должны иметь разные адреса хранилищ");

        std::cout<<"aligned_parameter_buffer: "<<checks<<" checks passed\n";
        return 0;
    } catch(const std::exception& error) {
        std::cerr<<error.what()<<"\n";
        return 1;
    }
}
