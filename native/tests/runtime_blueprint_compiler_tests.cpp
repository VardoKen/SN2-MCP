#include "sn2_mcp/runtime_blueprint_compiler.hpp"
#include <algorithm>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace sn2_mcp::runtime_blueprint;
using nlohmann::json;
namespace {
int checks{};
void require(bool condition,const char* message){++checks;if(!condition)throw std::runtime_error(message);}
template<class Action>void reject(const char* code,Action action) {
    try{action();}catch(const CompilerError& error){++checks;if(error.code!=code)throw std::runtime_error(std::string("Expected ")+code+" but received "+error.code+": "+error.what());return;}
    throw std::runtime_error("Compiler accepted invalid source or binding");
}
json node(std::string id,std::string kind){return {{"id",id},{"kind",kind}};}
json edge(const char* from,const char* from_pin,const char* to,const char* to_pin) {
    return {{"from",{{"node",from},{"pin",from_pin}}},{"to",{{"node",to},{"pin",to_pin}}}};
}
json parameter(const char* name,const char* type){return {{"name",name},{"type",type}};}
json asset(json function) {
    return {{"kind","blueprint"},{"path","/CompilerCheck/BP_Check.BP_Check"},
        {"parent_class","/Script/Engine.Actor"},{"variables",json::array()},
        {"nodes",json::array()},{"connections",json::array()},{"functions",json::array({function})}};
}
json literal_function(std::string value) {
    auto result=node("result","function_result");result["defaults"]={{"Version",value}};
    return {{"name","GetServiceVersion"},{"inputs",json::array()},
        {"outputs",json::array({parameter("Version","string")})},
        {"nodes",json::array({node("entry","function_entry"),result})},
        {"connections",json::array({edge("entry","then","result","execute")})}};
}
std::uint32_t u32(const std::vector<std::uint8_t>& bytes,std::size_t offset) {
    require(offset+4<=bytes.size(),"Expected complete 32-bit bytecode operand");
    std::uint32_t value{};for(unsigned i=0;i<4;++i)value|=std::uint32_t(bytes[offset+i])<<(i*8);return value;
}
BoundSymbol binding(const Relocation& relocation) {
    BoundSymbol symbol;symbol.identity=relocation.symbol+":serial-1";
    symbol.pointer=relocation.symbol=="function:GetServiceVersion/parameter:Version"?0x1122334455667788ull:0x2233445566778899ull;
    symbol.comparison_index=11;symbol.display_index=12;symbol.number=13;return symbol;
}
Resolver functions() {
    Resolver resolver;
    resolver.compatible=[](const Type& a,const Type& b){return a==b;};
    resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.symbol="reflected:"+owner+"."+name;result.name=name;result.owner_path=owner;result.flags=0x400;
        if(name=="SetRenderOpacity")result.parameters={{"InOpacity",result.symbol+"/InOpacity",Type{"float"},Direction::input}};
        else if(name=="GetRenderOpacity"){result.pure=true;result.parameters={{"ReturnValue",result.symbol+"/ReturnValue",Type{"float"},Direction::return_value}};}
        else if(name=="Add_FloatFloat") {
            result.flags|=0x2001;result.pure=true;result.default_object_symbol="object:/Script/Engine.Default__KismetMathLibrary";
            result.parameters={{"A",result.symbol+"/A",Type{"float"},Direction::input},
                {"B",result.symbol+"/B",Type{"float"},Direction::input},
                {"ReturnValue",result.symbol+"/ReturnValue",Type{"float"},Direction::return_value}};
        } else throw std::runtime_error("Unknown fixture reflection function");
        return result;
    };
    return resolver;
}
json apply_asset() {
    auto widget=parameter("Widget","object");widget["class_path"]="/Script/UMG.Widget";
    auto setter=node("setter","call_function");setter["class"]="/Script/UMG.Widget";setter["function"]="SetRenderOpacity";
    auto getter=node("getter","call_function");getter["class"]="/Script/UMG.Widget";getter["function"]="GetRenderOpacity";
    auto set_counter=node("counter","variable_set");set_counter["variable"]="Counter";
    auto get_counter=node("read_counter","variable_get");get_counter["variable"]="Counter";
    auto add=node("add","call_function");add["class"]="/Script/Engine.KismetMathLibrary";add["function"]="Add_FloatFloat";add["defaults"]={{"B","1.0"}};
    auto result=node("result","function_result");result["defaults"]={{"Applied","true"}};
    auto skipped=node("skipped","function_result"); // Schema deliberately permits one result node.
    json function={{"name","Apply"},{"inputs",json::array({widget,parameter("Enabled","bool"),parameter("Opacity","float")})},
        {"outputs",json::array({parameter("Applied","bool"),parameter("ActualOpacity","float"),parameter("Count","float")})},
        {"nodes",json::array({node("entry","function_entry"),node("branch","branch"),setter,getter,set_counter,get_counter,add,result})},
        {"connections",json::array({edge("entry","then","branch","execute"),edge("entry","Enabled","branch","Condition"),
            edge("branch","then","setter","execute"),edge("branch","else","result","execute"),edge("setter","then","counter","execute"),
            edge("counter","then","result","execute"),edge("entry","Widget","setter","self"),edge("entry","Opacity","setter","InOpacity"),
            edge("entry","Widget","getter","self"),edge("getter","ReturnValue","result","ActualOpacity"),
            edge("read_counter","Counter","add","A"),edge("add","ReturnValue","counter","Counter"),edge("read_counter","Counter","result","Count")})}};
    auto source=asset(function);source["variables"]=json::array({parameter("Counter","float")});return source;
}
void literal_and_binding() {
    const auto source=asset(literal_function("0.7.0"));const auto compiled=compile_blueprint(source,{});
    require(compiled.functions.size()==1,"Expected one function");const auto& function=compiled.functions.front();
    const auto bound=bind_script(function,binding);const auto& bytes=bound.bytecode;
    // Expected native Kismet instruction shape: entry jump, EX_Let, out-variable,
    // ASCII literal, result jump, fallthrough jump, Return/Nothing/EndOfScript.
    require(bytes[0]==0x06,"Function entry must dispatch through an Unreal jump");
    require(bytes[5]==0x0f&&bytes[14]==0x48&&bytes[23]==0x1f,"String output assignment has invalid Unreal opcode layout");
    require(std::string(bytes.begin()+24,bytes.begin()+29)=="0.7.0"&&bytes[29]==0,"Expected the authored literal, including terminator");
    require(bytes[bytes.size()-3]==0x04&&bytes[bytes.size()-2]==0x0b&&bytes.back()==0x53,"Expected native function epilogue");
    require(function.relocations.size()==2,"String output must reference its real FProperty twice");
    require(bound.referenced_identities.size()==1,"Repeated property operands must share one identity");
    require(u32(bytes,1)==5,"Entry jump must target the result statement");
    auto missing=[](const Relocation&){return BoundSymbol{};};reject("unresolved_symbol",[&]{bind_script(function,missing);});
    unsigned sequence{};reject("symbol_changed",[&]{bind_script(function,[&](const Relocation& r){auto b=binding(r);b.identity=std::to_string(++sequence);return b;});});
    sequence=0;reject("symbol_changed",[&]{bind_script(function,[&](const Relocation& r){auto b=binding(r);b.pointer+=++sequence;return b;});});
    auto corrupt=function;corrupt.relocations.push_back(corrupt.relocations.front());reject("overlapping_relocation",[&]{bind_script(corrupt,binding);});
    corrupt=function;corrupt.relocations.front().offset=corrupt.bytecode.size();reject("invalid_relocation",[&]{bind_script(corrupt,binding);});
    auto invalid=source;invalid["functions"][0]["nodes"][1]["defaults"]["Version"]=true;
    reject("invalid_pin_default",[&]{compile_blueprint(invalid,{});});
    const auto unicode=compile_blueprint(asset(literal_function("Привет 🌊")),{});
    require(std::find(unicode.functions[0].bytecode.begin(),unicode.functions[0].bytecode.end(),0x34)!=unicode.functions[0].bytecode.end(),"Unicode literals require EX_UnicodeStringConst");
    std::string broken("\xc0\xaf",2);reject("invalid_utf8",[&]{compile_blueprint(asset(literal_function(broken)),{});});
}
void graph_and_call() {
    const auto source=apply_asset();const auto compiled=compile_blueprint(source,functions());const auto& function=compiled.functions.front();
    require(compiled.members.size()==1&&compiled.members[0].symbol=="member:Counter","Member definition is absent");
    require((function.flags&0x00c00000)==0x00c00000,"Output/local properties require correct initialization flags");
    require(std::count_if(function.properties.begin(),function.properties.end(),[](const auto& p){return p.role==PropertyRole::local;})==2,"Pure calls require managed local result properties");
    const auto bound=bind_script(function,binding);require(!bound.bytecode.empty(),"Expected actual executable bytecode");
    require(std::count(bound.bytecode.begin(),bound.bytecode.end(),0x19)>=3,"Calls must execute through native context expressions");
    bool native_name=false,math_function=false;
    for(const auto& relocation:function.relocations) {
        native_name|=relocation.kind==RelocationKind::script_name&&relocation.symbol=="name:SetRenderOpacity";
        math_function|=relocation.kind==RelocationKind::pointer&&relocation.symbol.find("Add_FloatFloat")!=std::string::npos;
        if(relocation.kind==RelocationKind::script_name) {
            require(u32(bound.bytecode,relocation.offset)==11&&u32(bound.bytecode,relocation.offset+4)==12&&u32(bound.bytecode,relocation.offset+8)==13,"FScriptName must contain three 32-bit components");
        }
    }
    require(native_name&&math_function,"Virtual native and final reflected operator calls must be emitted");
    const auto branch=std::find_if(function.source_ranges.begin(),function.source_ranges.end(),[](const auto& r){return r.node=="Apply/branch";});
    require(branch!=function.source_ranges.end(),"Branch source attribution missing");
    require(bound.bytecode[branch->begin]==0x07,"Branch must use actual EX_JumpIfNot");
    const auto false_target=u32(bound.bytecode,branch->begin+1);
    const auto result=std::find_if(function.source_ranges.begin(),function.source_ranges.end(),[](const auto& r){return r.node=="Apply/result";});
    require(result!=function.source_ranges.end()&&false_target==result->begin,"False branch fixup targets wrong statement");
    auto invalid=source;invalid["functions"][0]["connections"].push_back(edge("entry","Opacity","setter","InOpacity"));
    reject("connection_replacement",[&]{compile_blueprint(invalid,functions());});
    invalid=source;invalid["functions"][0]["connections"][1]["from"]["pin"]="Opacity";
    reject("connection_invalid",[&]{compile_blueprint(invalid,functions());});
    auto cyclic=source;cyclic["functions"][0]["connections"][10]=edge("add","ReturnValue","add","A");
    reject("data_dependency_cycle",[&]{compile_blueprint(cyclic,functions());});
    invalid=source;
    auto& connections=invalid["functions"][0]["connections"];
    for(auto iterator=connections.begin();iterator!=connections.end();)
        if((*iterator)["to"]["node"]=="setter"&&(*iterator)["to"]["pin"]=="self")iterator=connections.erase(iterator);else ++iterator;
    reject("incompatible_self_receiver",[&]{compile_blueprint(invalid,functions());});
}
void compound_literals_and_split() {
    auto objects=parameter("Objects","object");objects["class_path"]="/Script/CoreUObject.Object";objects["container"]="array";
    auto e=node("entry","function_entry"),r=node("result","function_result");
    json function={{"name","EmptyObjects"},{"inputs",json::array()},{"outputs",json::array({objects})},
        {"nodes",json::array({e,r})},{"connections",json::array({edge("entry","then","result","execute")})}};
    const auto empty=compile_blueprint(asset(function),{}).functions.front();
    bool inner=false;for(const auto& relocation:empty.relocations)inner|=relocation.symbol=="function:EmptyObjects/parameter:Objects/inner";
    require(inner,"EX_ArrayConst requires its real inner FProperty relocation");
    const auto end=std::find(empty.bytecode.begin(),empty.bytecode.end(),0x66);require(end!=empty.bytecode.end(),"Array constant requires EX_EndArrayConst");
    Resolver resolver;resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.name=name;result.owner_path=owner;result.symbol="reflected:Trace";result.flags=0x2401;
        result.default_object_symbol="object:Default__Trace";
        result.parameters={{"OutHit","reflected:Trace/OutHit",Type{"struct","/Script/Engine.HitResult"},Direction::output},
            {"ReturnValue","reflected:Trace/ReturnValue",Type{"bool"},Direction::return_value}};return result;
    };
    resolver.structure=[](const std::string& path){require(path=="/Script/Engine.HitResult","Unexpected struct resolution");
        return StructSignature{"struct:HitResult",256,{{"Distance","field:HitResult.Distance",Type{"float"}}}};};
    auto call=node("trace","call_function");call["class"]="/Script/Engine.KismetSystemLibrary";call["function"]="Trace";call["split_pins"]=json::array({"OutHit"});
    function={{"name","Trace"},{"inputs",json::array()},{"outputs",json::array({parameter("Distance","float"),parameter("Hit","bool")})},
        {"nodes",json::array({e,call,r})},{"connections",json::array({edge("entry","then","trace","execute"),edge("trace","then","result","execute"),
            edge("trace","OutHit_Distance","result","Distance"),edge("trace","ReturnValue","result","Hit")})}};
    const auto trace=compile_blueprint(asset(function),resolver).functions.front();
    const auto field=std::find_if(trace.relocations.begin(),trace.relocations.end(),[](const auto& item){return item.symbol=="field:HitResult.Distance";});
    require(field!=trace.relocations.end()&&trace.bytecode[field->offset-1]==0x42,"Split struct output requires EX_StructMemberContext with reflected member property");
    require(std::count_if(trace.properties.begin(),trace.properties.end(),[](const auto& p){return p.role==PropertyRole::local;})==2,"Managed call output requires actual typed local properties");
    auto invalid=asset(function);invalid["functions"][0]["nodes"][1]["split_pins"]=json::array({"Missing"});
    reject("invalid_split_pin",[&]{compile_blueprint(invalid,resolver);});
    resolver.literal=[](const Type&,const std::string&){return json{{"Distance",json::array({1.25,2.5})}};};
    resolver.structure=[](const std::string&){return StructSignature{"struct:Fixed",8,{{"Distance","field:Fixed.Distance",Type{"float"},2}}};};
    auto structure=parameter("Data","struct");structure["struct_path"]="/Script/Compiler.Fixed";
    function={{"name","StructLiteral"},{"inputs",json::array()},{"outputs",json::array({structure})},
        {"nodes",json::array({e,r})},{"connections",json::array({edge("entry","then","result","execute")})}};
    const auto fixed=compile_blueprint(asset(function),resolver).functions.front();
    const auto marker=std::find(fixed.bytecode.begin(),fixed.bytecode.end(),0x2f);
    require(marker!=fixed.bytecode.end(),"Struct default must emit EX_StructConst");
    const auto start=static_cast<std::size_t>(marker-fixed.bytecode.begin());
    require(fixed.bytecode[start+13]==0x1e&&fixed.bytecode[start+18]==0x1e&&fixed.bytecode[start+23]==0x30,"Fixed ArrayDim struct fields must emit elements in PropertyLink order");
}
void sequence_flow_and_wildcard() {
    auto make_call=[](const char* id){auto call=node(id,"call_function");call["class"]="/Script/UMG.Widget";call["function"]="SetRenderOpacity";return call;};
    auto widget=parameter("Widget","object");widget["class_path"]="/Script/UMG.Widget";
    auto sequence=node("sequence","sequence");auto e=node("entry","function_entry"),r=node("result","function_result");
    json function={{"name","Sequence"},{"inputs",json::array({widget})},{"outputs",json::array()},
        {"nodes",json::array({e,sequence,make_call("first"),make_call("second"),r})},
        {"connections",json::array({edge("entry","then","sequence","execute"),edge("sequence","then_0","first","execute"),
            edge("sequence","then_1","second","execute"),edge("entry","Widget","first","self"),edge("entry","Widget","second","self"),
            edge("second","then","result","execute")})}};
    const auto compiled=compile_blueprint(asset(function),functions()).functions.front();
    require(compiled.bytecode.front()==0x4c,"Sequence function must seed its execution flow stack with return target");
    const auto sequence_range=std::find_if(compiled.source_ranges.begin(),compiled.source_ranges.end(),[](const auto& range){return range.node=="Sequence/sequence";});
    const auto first=std::find_if(compiled.source_ranges.begin(),compiled.source_ranges.end(),[](const auto& range){return range.node=="Sequence/first";});
    const auto second=std::find_if(compiled.source_ranges.begin(),compiled.source_ranges.end(),[](const auto& range){return range.node=="Sequence/second";});
    require(sequence_range!=compiled.source_ranges.end()&&first!=compiled.source_ranges.end()&&second!=compiled.source_ranges.end(),"Sequence source nodes must be emitted");
    require(compiled.bytecode[sequence_range->begin]==0x4c&&u32(compiled.bytecode,sequence_range->begin+1)==second->begin,"Sequence must push second branch first");
    require(compiled.bytecode[sequence_range->begin+5]==0x06&&u32(compiled.bytecode,sequence_range->begin+6)==first->begin,"Sequence must jump to first branch");
    require(compiled.bytecode[first->end-1]==0x4d,"Unconnected first branch must resume the next sequence branch");
    Resolver resolver;resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.symbol="reflected:Array_Add";result.owner_path=owner;result.name=name;result.flags=0x2401;
        result.default_object_symbol="object:Default__KismetArrayLibrary";
        result.parameters={{"TargetArray",result.symbol+"/TargetArray",Type{"int",{},true},Direction::input_output,{},false,true},
            {"NewItem",result.symbol+"/NewItem",Type{"int"},Direction::input,{},false,true},
            {"ReturnValue",result.symbol+"/ReturnValue",Type{"int"},Direction::return_value}};return result;
    };
    auto held=parameter("Held","object");held["class_path"]="/Script/CoreUObject.Object";held["container"]="array";
    auto object=parameter("Object","object");object["class_path"]="/Script/CoreUObject.Object";
    auto get=node("held","variable_get");get["variable"]="Held";auto add=node("add","call_function");add["class"]="/Script/Engine.KismetArrayLibrary";add["function"]="Array_Add";
    function={{"name","Add"},{"inputs",json::array({object})},{"outputs",json::array({parameter("Index","int")})},
        {"nodes",json::array({e,get,add,r})},{"connections",json::array({edge("entry","then","add","execute"),edge("add","then","result","execute"),
            edge("held","Held","add","TargetArray"),edge("entry","Object","add","NewItem"),edge("add","ReturnValue","result","Index")})}};
    auto source=asset(function);source["variables"]=json::array({held});
    const auto wildcard=compile_blueprint(source,resolver).functions.front();
    require(std::find(wildcard.bytecode.begin(),wildcard.bytecode.end(),0x1c)!=wildcard.bytecode.end(),"Wildcard call must execute real final native thunk");
    require(std::any_of(wildcard.relocations.begin(),wildcard.relocations.end(),[](const auto& relocation){return relocation.symbol=="member:Held";}),"Wildcard array must reference actual typed member property");
    auto read_only=source;read_only["variables"]=json::array();read_only["functions"][0]["inputs"].push_back(held);
    read_only["functions"][0]["nodes"].erase(read_only["functions"][0]["nodes"].begin()+1);
    read_only["functions"][0]["connections"][2]=edge("entry","Held","add","TargetArray");
    reject("read_only_reference_argument",[&]{compile_blueprint(read_only,resolver);});
    auto incorrect=source;incorrect["functions"][0]["inputs"][0]=parameter("Object","int");
    reject("wildcard_type_mismatch",[&]{compile_blueprint(incorrect,resolver);});
    auto get_resolver=resolver;get_resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.symbol="reflected:Array_Get";result.owner_path=owner;result.name=name;result.flags=0x2401;result.pure=true;
        result.default_object_symbol="object:Default__KismetArrayLibrary";
        result.parameters={{"TargetArray",result.symbol+"/TargetArray",Type{"int",{},true},Direction::input,{},false,true},
            {"Item",result.symbol+"/Item",Type{"int"},Direction::output,{},false,true}};return result;
    };
    auto get_call=node("get","call_function");get_call["class"]="/Script/Engine.KismetArrayLibrary";get_call["function"]="Array_Get";
    function={{"name","Get"},{"inputs",json::array()},{"outputs",json::array({object})},
        {"nodes",json::array({e,get,get_call,r})},{"connections",json::array({edge("entry","then","result","execute"),
            edge("held","Held","get","TargetArray"),edge("get","Item","result","Object")})}};
    auto get_source=asset(function);get_source["variables"]=json::array({held});
    const auto get_compiled=compile_blueprint(get_source,get_resolver).functions.front();
    auto item=std::find_if(get_compiled.properties.begin(),get_compiled.properties.end(),[](const auto& p){return p.symbol=="function:Get/local:get.Item";});
    require(item!=get_compiled.properties.end()&&item->type.kind=="object"&&item->type.path=="/Script/CoreUObject.Object","Wildcard output must allocate the inferred real object property type");
    auto literal_array=get_source;literal_array["functions"][0]["connections"].erase(literal_array["functions"][0]["connections"].begin()+1);
    // The unconstrained placeholder is an int array, so result type is also int.
    literal_array["functions"][0]["outputs"][0]=parameter("Object","int");
    const auto literal_compiled=compile_blueprint(literal_array,get_resolver).functions.front();
    require(std::any_of(literal_compiled.properties.begin(),literal_compiled.properties.end(),[](const auto& p){return p.symbol.ends_with("TargetArray.reference")&&p.type.array;}),"Wildcard array default must be materialized before a native thunk consumes its address");
}
void numeric_conversion_contract() {
    Resolver resolver;resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.name=name;result.owner_path=owner;result.symbol="reflected:"+name;result.flags=0x2401;result.pure=true;
        result.default_object_symbol="object:Default__Math";
        result.parameters={{"Value",result.symbol+"/Value",Type{"double"},Direction::input},
            {"ReturnValue",result.symbol+"/ReturnValue",Type{"double"},Direction::return_value}};return result;
    };
    auto call=node("math","call_function");call["class"]="/Script/Engine.KismetMathLibrary";call["function"]="DoubleValue";
    auto e=node("entry","function_entry"),r=node("result","function_result");
    json function={{"name","Convert"},{"inputs",json::array({parameter("Input","float")})},{"outputs",json::array({parameter("Result","double")})},
        {"nodes",json::array({e,call,r})},{"connections",json::array({edge("entry","then","result","execute"),
            edge("entry","Input","math","Value"),edge("math","ReturnValue","result","Result")})}};
    const auto property=compile_blueprint(asset(function),resolver).functions.front();
    auto operand=std::find_if(property.relocations.begin(),property.relocations.end(),[](const auto& relocation){return relocation.symbol=="function:Convert/parameter:Input";});
    require(operand!=property.relocations.end()&&property.bytecode[operand->offset-3]==0x38&&property.bytecode[operand->offset-2]==4&&property.bytecode[operand->offset-1]==0,"Numeric property coercion requires CST_FloatToDouble over EX_LocalVariable");
    // No Cast token may surround a literal, because Unreal cast handlers require
    // MostRecentPropertyAddress and cannot write through a null literal result.
    auto literal=asset(function);literal["functions"][0]["connections"].erase(literal["functions"][0]["connections"].begin()+1);
    literal["functions"][0]["nodes"][1]["defaults"]={{"Value","1.25"}};
    const auto constant=compile_blueprint(literal,resolver).functions.front();
    require(std::find(constant.bytecode.begin(),constant.bytecode.end(),0x37)!=constant.bytecode.end(),"Double literal must use EX_DoubleConst");
    auto reference=resolver;reference.function=[&](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.parameters[0].direction=Direction::input_output;return signature;};
    reject("mutable_reference_type_mismatch",[&]{compile_blueprint(asset(function),reference);});
    auto scripted=resolver;scripted.function=[&](const std::string& owner,const std::string& name){
        auto signature=resolver.function(owner,name);signature.flags=0x2001;
        signature.parameters[0].const_reference=true;return signature;};
    const auto by_reference=compile_blueprint(literal,scripted).functions.front();
    const auto temporary=std::find_if(by_reference.properties.begin(),by_reference.properties.end(),[](const auto& p){return p.symbol.ends_with(".reference");});
    require(temporary!=by_reference.properties.end()&&temporary->role==PropertyRole::local&&temporary->type.kind=="double","Script const-ref literal requires a real typed temporary property");
    require(std::count_if(by_reference.relocations.begin(),by_reference.relocations.end(),[&](const auto& relocation){return relocation.symbol==temporary->symbol;})>=3,"Const-ref temporary must be assigned and passed as an addressable property");
    const auto reference_conversion=compile_blueprint(asset(function),scripted).functions.front();
    const auto converted_temporary=std::find_if(reference_conversion.properties.begin(),reference_conversion.properties.end(),[](const auto& p){return p.symbol.ends_with(".reference");});
    require(converted_temporary!=reference_conversion.properties.end()&&converted_temporary->type.kind=="double","Const-ref numeric conversion must materialize target-width storage");
    auto repeated=asset(function);repeated["functions"][0]["outputs"].push_back(parameter("Another","double"));
    repeated["functions"][0]["connections"].push_back(edge("math","ReturnValue","result","Another"));
    const auto repeated_compiled=compile_blueprint(repeated,scripted).functions.front();
    require(std::count_if(repeated_compiled.properties.begin(),repeated_compiled.properties.end(),[](const auto& p){return p.symbol.ends_with(".reference");})==1,"Repeated pure uses must not duplicate compiler temporary fields");
}
}
int main() {
    try{literal_and_binding();graph_and_call();compound_literals_and_split();sequence_flow_and_wildcard();numeric_conversion_contract();std::cout<<"runtime_blueprint_compiler: "<<checks<<" meaningful checks passed\n";return 0;}
    catch(const CompilerError& error){std::cerr<<error.code<<": "<<error.what()<<" ["<<error.node<<"/"<<error.pin<<"]\n";return 1;}
    catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
