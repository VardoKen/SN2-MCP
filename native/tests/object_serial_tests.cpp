#include <g1r_mcp/object_serial.hpp>
#include <iostream>
#include <limits>
#include <stdexcept>

namespace {
int checks=0;
void require(bool condition,const char* message) {
    ++checks;if(!condition)throw std::runtime_error(message);
}
}
int main() {
    using namespace g1r_mcp;
    try {
        const ObjectSerialIdentity original{0x1000,0x2000,9,71,10,2,33,20};
        require(same_serial_identity(original,original),"Неизменённая identity потеряна");
        auto reused=original;reused.serial=11;
        require(!same_serial_identity(original,reused),"ABA объекта с прежним tuple и другим serial пропущена");
        reused=original;reused.class_serial=21;
        require(!same_serial_identity(original,reused),"ABA класса с прежним указателем и другим serial пропущена");
        reused=original;reused.class_index=3;
        require(!same_serial_identity(original,reused),"Смена слота класса пропущена");
        reused=original;reused.class_name=34;
        require(!same_serial_identity(original,reused),"Смена имени класса пропущена");
        reused=original;reused.object=0x3000;
        require(!same_serial_identity(original,reused),"Смена указателя объекта пропущена");
        reused=original;reused.object_class=0x3000;
        require(!same_serial_identity(original,reused),"Смена указателя класса пропущена");
        reused=original;reused.index=10;
        require(!same_serial_identity(original,reused),"Смена слота объекта пропущена");
        reused=original;reused.name=72;
        require(!same_serial_identity(original,reused),"Смена имени объекта пропущена");
        const ObjectSerialIdentity zero{0x1000,0x2000,9,71,0,2,33,0};
        require(same_serial_identity(zero,zero),"Нулевая наблюдаемая identity должна сравниваться строго");
        require(serial_coverage(zero)=="unavailable_zero","Нули нельзя объявлять ABA-защитой");
        auto allocated=zero;allocated.serial=1;
        require(!same_serial_identity(zero,allocated),"Новый serial не должен приниматься старой ссылкой");
        require(serial_coverage(allocated)=="object_nonzero","Неверный частичный охват serial объекта");
        allocated=zero;allocated.class_serial=1;
        require(!same_serial_identity(zero,allocated),"Новый serial класса не должен приниматься старой ссылкой");
        require(serial_coverage(allocated)=="class_nonzero","Неверный частичный охват serial класса");
        require(serial_coverage(original)=="object_and_class_nonzero","Положительные serial должны иметь полный наблюдаемый охват");
        auto invalid=original;invalid.serial=-1;
        require(!valid_observed_serial(-1) && !same_serial_identity(invalid,invalid),"Отрицательный serial принят");
        require(serial_coverage(invalid)=="invalid","Повреждённый serial нельзя обозначать как покрытый");
        invalid=original;invalid.class_serial=std::numeric_limits<int32_t>::min();
        require(!same_serial_identity(original,invalid) && !same_serial_identity(invalid,original),"Отрицательный serial класса принят");
        auto upper=original;upper.serial=std::numeric_limits<int32_t>::max();
        require(valid_observed_serial(upper.serial) && same_serial_identity(upper,upper),"Положительный int32 предел не должен переполняться");
        std::cout<<"Object serial: "<<checks<<" проверок прошли\n";return 0;
    } catch(const std::exception& error) {std::cerr<<error.what()<<'\n';return 1;}
}
