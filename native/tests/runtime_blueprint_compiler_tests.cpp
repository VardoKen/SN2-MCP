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
        FunctionSignature result;result.symbol="reflected:"+owner+"."+name;result.name=name;result.owner_path=owner;result.flags=0x04000400;
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
void ordinary_call_admission() {
    const auto source=apply_asset();const auto accepted=functions();
    require(!compile_blueprint(source,accepted).functions.front().bytecode.empty(),"A real BlueprintCallable control must remain compilable");
    auto denied=accepted;
    denied.function=[accepted](const std::string& owner,const std::string& name) {
        auto signature=accepted.function(owner,name);signature.flags&=~0x04000000u;return signature;
    };
    reject("function_not_blueprint_callable",[&]{compile_blueprint(source,denied);});
    denied.function=[accepted](const std::string& owner,const std::string& name) {
        auto signature=accepted.function(owner,name);signature.flags|=0x00100000u;return signature;
    };
    reject("function_not_blueprint_callable",[&]{compile_blueprint(source,denied);});
    denied.function=[accepted](const std::string& owner,const std::string& name) {
        auto signature=accepted.function(owner,name);signature.flags|=0x00010000u;return signature;
    };
    reject("function_not_blueprint_callable",[&]{compile_blueprint(source,denied);});
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
        FunctionSignature result;result.name=name;result.owner_path=owner;result.symbol="reflected:Trace";result.flags=0x04002401;
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
        FunctionSignature result;result.symbol="reflected:Array_Add";result.owner_path=owner;result.name=name;result.flags=0x04002401;
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
        FunctionSignature result;result.symbol="reflected:Array_Get";result.owner_path=owner;result.name=name;result.flags=0x04002401;result.pure=true;
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
        FunctionSignature result;result.name=name;result.owner_path=owner;result.symbol="reflected:"+name;result.flags=0x04002401;result.pure=true;
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
        auto signature=resolver.function(owner,name);signature.flags=0x04002001;
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
Resolver event_resolver() {
    auto resolver=functions();const auto reflected=resolver.function;
    resolver.function=[reflected](const std::string& owner,const std::string& name) {
        if(name!="ReceiveInit"&&name!="ReceiveShutdown"&&name!="ReceiveTick")return reflected(owner,name);
        FunctionSignature signature;signature.name=name;signature.owner_path=owner;
        signature.symbol="reflected:"+owner+"."+name;
        signature.flags=0x0c020c08; // Native BlueprintEvent; generated override must strip Native.
        if(name=="ReceiveTick")signature.parameters={{"DeltaSeconds",signature.symbol+"/DeltaSeconds",Type{"float"},Direction::input}};
        return signature;
    };
    return resolver;
}
void explicit_self_value() {
    auto object_output=parameter("Outer","object");object_output["class_path"]="/Script/CoreUObject.Object";
    auto outer=node("outer","call_function");outer["class"]="/Script/Engine.KismetSystemLibrary";outer["function"]="GetOuterObject";
    auto current=node("current","self");
    json function={{"name","GetEngine"},{"inputs",json::array()},{"outputs",json::array({object_output})},
        {"nodes",json::array({node("entry","function_entry"),current,outer,node("result","function_result")})},
        {"connections",json::array({edge("entry","then","result","execute"),edge("current","self","outer","Object"),
            edge("outer","ReturnValue","result","Outer")})}};
    auto source=asset(function);source["parent_class"]="/Script/Engine.GameInstance";
    Resolver resolver;
    resolver.compatible=[](const Type& a,const Type& b) {
        return a==b||(a.kind=="object"&&b.kind=="object"&&!a.array&&!b.array
            &&a.path=="/Script/Engine.GameInstance"&&b.path=="/Script/CoreUObject.Object");
    };
    resolver.function=[](const std::string& owner,const std::string& name) {
        FunctionSignature result;result.symbol="reflected:"+owner+"."+name;result.name=name;result.owner_path=owner;
        result.flags=0x04002401;result.pure=true;
        result.default_object_symbol="object:/Script/Engine.Default__KismetSystemLibrary";
        result.parameters={{"Object",result.symbol+"/Object",Type{"object","/Script/CoreUObject.Object",false},Direction::input},
            {"ReturnValue",result.symbol+"/ReturnValue",Type{"object","/Script/CoreUObject.Object",false},Direction::return_value}};
        return result;
    };
    const auto compiled=compile_blueprint(source,resolver).functions.front();
    const auto called=std::find_if(compiled.relocations.begin(),compiled.relocations.end(),[](const auto& relocation) {
        return relocation.symbol=="reflected:/Script/Engine.KismetSystemLibrary.GetOuterObject";
    });
    require(called!=compiled.relocations.end()&&compiled.bytecode[called->offset-1]==0x1c,
        "GetOuterObject must retain its exact final native function reference");
    require(compiled.bytecode[called->offset+8]==0x17&&compiled.bytecode[called->offset+9]==0x16,
        "Explicit current-instance argument must emit EX_Self followed by end parameters");
    require(std::any_of(compiled.properties.begin(),compiled.properties.end(),[](const auto& property) {
        return property.role==PropertyRole::local&&property.type==Type{"object","/Script/CoreUObject.Object",false};
    }),"Pure object result must retain exact reflected local storage");
    const auto bound=bind_script(compiled,binding);
    require(bound.referenced_identities.contains(called->symbol),"Binding must preserve the reflected outer function identity");
    auto exact=resolver;exact.compatible=[](const Type& a,const Type& b){return a==b;};
    reject("connection_invalid",[&]{compile_blueprint(source,exact);});
    auto unrelated=resolver;unrelated.function=[resolver](const std::string& owner,const std::string& name) {
        auto signature=resolver.function(owner,name);signature.parameters[0].type.path="/Script/UMG.Widget";return signature;
    };
    reject("connection_invalid",[&]{compile_blueprint(source,unrelated);});
    auto mutable_self=resolver;mutable_self.function=[resolver](const std::string& owner,const std::string& name) {
        auto signature=resolver.function(owner,name);signature.parameters[0].type.path="/Script/Engine.GameInstance";
        signature.parameters[0].direction=Direction::input_output;return signature;
    };
    reject("mutable_reference_requires_variable",[&]{compile_blueprint(source,mutable_self);});
    auto incompatible=source;incompatible["functions"][0]["connections"].push_back(edge("current","self","result","execute"));
    reject("connection_invalid",[&]{compile_blueprint(incompatible,resolver);});
    auto direct=source;direct["functions"][0]["nodes"].erase(direct["functions"][0]["nodes"].begin()+2);
    direct["functions"][0]["outputs"][0]["class_path"]="/Script/Engine.GameInstance";
    direct["functions"][0]["connections"]=json::array({edge("entry","then","result","execute"),edge("current","self","result","Outer")});
    const auto exact_self=compile_blueprint(direct,exact).functions.front();
    require(std::count(exact_self.bytecode.begin(),exact_self.bytecode.end(),0x17)==1,
        "Direct self output must emit the instance once without native helper calls");
    direct["functions"][0]["outputs"][0]=parameter("Outer","int");
    reject("connection_invalid",[&]{compile_blueprint(direct,resolver);});
}
json event_asset() {
    auto source=asset(literal_function("retained-function"));source["parent_class"]="/Script/Engine.GameInstance";
    source["variables"]=json::array({parameter("Initialized","bool"),parameter("LastDelta","float")});
    auto init=node("init","event");init["function"]="ReceiveInit";
    auto shutdown=node("shutdown","event");shutdown["function"]="ReceiveShutdown";
    auto tick=node("tick","event");tick["function"]="ReceiveTick";
    auto set_init=node("set_init","variable_set");set_init["variable"]="Initialized";set_init["defaults"]={{"Initialized","true"}};
    auto set_shutdown=node("set_shutdown","variable_set");set_shutdown["variable"]="Initialized";set_shutdown["defaults"]={{"Initialized","false"}};
    auto set_tick=node("set_tick","variable_set");set_tick["variable"]="LastDelta";
    source["nodes"]=json::array({init,node("parent_init","call_parent"),set_init,shutdown,set_shutdown,tick,set_tick});
    source["connections"]=json::array({edge("init","then","parent_init","execute"),edge("parent_init","then","set_init","execute"),
        edge("shutdown","then","set_shutdown","execute"),edge("tick","then","set_tick","execute"),
        edge("tick","DeltaSeconds","set_tick","LastDelta")});
    return source;
}
void reflected_event_overrides() {
    const auto source=event_asset();const auto resolver=event_resolver();const auto compiled=compile_blueprint(source,resolver);
    require(compiled.functions.size()==4,"Three events and retained named function must each compile");
    auto find=[&](const char* name)->const CompiledFunction& {
        const auto found=std::find_if(compiled.functions.begin(),compiled.functions.end(),[&](const auto& function){return function.name==name;});
        require(found!=compiled.functions.end(),"Expected reflected event body");return *found;
    };
    const auto& init=find("ReceiveInit");const auto& tick=find("ReceiveTick");
    require(init.event_override&&init.super_function_symbol=="reflected:/Script/Engine.GameInstance.ReceiveInit","Event must retain exact inherited identity");
    require((init.flags&0x08000800u)==0x08000800u&&!(init.flags&0x2401u),"Generated event must inherit Event/BlueprintEvent but not Native/Static/Final");
    require((init.flags&8u)!=0,"Reflected cosmetic contract must remain inherited");
    require(tick.properties.size()==1&&tick.properties.front().name=="DeltaSeconds"&&tick.properties.front().role==PropertyRole::input,
        "Event input must match reflected parameter name/type/order");
    require(std::any_of(tick.relocations.begin(),tick.relocations.end(),[](const auto& relocation){return relocation.symbol=="function:ReceiveTick/parameter:DeltaSeconds";}),
        "Event parameter bytecode must use generated FProperty rather than parent property address");
    const auto parent=std::find_if(init.relocations.begin(),init.relocations.end(),[&](const auto& relocation){return relocation.symbol==init.super_function_symbol;});
    require(parent!=init.relocations.end()&&init.bytecode[parent->offset-1]==0x1c,"Parent event call must use direct EX_FinalFunction to prevent virtual recursion");
    require(!find("GetServiceVersion").event_override,"Named function must retain its existing independent contract");
    const auto bound=bind_script(init,binding);require(bound.referenced_identities.contains(init.super_function_symbol),"Saved event script must retain super function reference identity");
    auto explicit_self=source;explicit_self["nodes"].push_back(node("current","self"));
    explicit_self["connections"].push_back(edge("current","self","set_init","self"));
    explicit_self["connections"].push_back(edge("current","self","set_shutdown","self"));
    require(compile_blueprint(explicit_self,resolver).functions.size()==4,
        "Shared pure self value must be included in each dependent event body");
    auto event_only=source;event_only.erase("functions");require(compile_blueprint(event_only,resolver).functions.size()==3,"Event-only Blueprint must not require synthetic authored function");
    auto parent_event_only=source;parent_event_only.erase("functions");parent_event_only["nodes"]=json::array({source["nodes"][0],source["nodes"][1]});
    parent_event_only["connections"]=json::array({source["connections"][0]});
    auto non_callable_parent=resolver;
    non_callable_parent.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.flags&=~0x04000000u;return signature;};
    require(compile_blueprint(parent_event_only,non_callable_parent).functions.front().event_override,
        "call_parent must remain permitted by its BlueprintEvent contract without requiring ordinary BlueprintCallable");
    auto branch_source=source;
    branch_source["nodes"].push_back(node("init_branch","branch"));
    branch_source["nodes"].back()["defaults"]={{"Condition","true"}};
    branch_source["connections"][1]=edge("parent_init","then","init_branch","execute");
    branch_source["connections"].push_back(edge("init_branch","then","set_init","execute"));
    const auto branched=compile_blueprint(branch_source,resolver);
    const auto& branch_function=*std::find_if(branched.functions.begin(),branched.functions.end(),[](const auto& function){return function.name=="ReceiveInit";});
    const auto branch_range=std::find_if(branch_function.source_ranges.begin(),branch_function.source_ranges.end(),[](const auto& range){return range.node=="ReceiveInit/init_branch";});
    require(branch_range!=branch_function.source_ranges.end()&&branch_function.bytecode[branch_range->begin]==0x07,"Event branch must emit actual conditional jump");
    const auto fallthrough=u32(branch_function.bytecode,branch_range->begin+1);
    require(branch_function.bytecode[fallthrough]==0x06&&u32(branch_function.bytecode,fallthrough+1)==branch_function.bytecode.size()-3,
        "Unconnected event branch must fall through to real Return/Nothing without a function_result node");
    auto empty=event_only;empty["nodes"]=json::array();empty["connections"]=json::array();reject("empty_functions",[&]{compile_blueprint(empty,resolver);});
    auto invalid=source;invalid["nodes"][2]["defaults"]["Initialized"]=17;reject("invalid_pin_default",[&]{compile_blueprint(invalid,resolver);});
    invalid=source;invalid["connections"].push_back(edge("tick","DeltaSeconds","set_init","Initialized"));
    reject("cross_event_dependency",[&]{compile_blueprint(invalid,resolver);});
    invalid=source;invalid["nodes"].push_back(node("unused","variable_set"));invalid["nodes"].back()["variable"]="Initialized";
    reject("event_node_unreachable",[&]{compile_blueprint(invalid,resolver);});
    invalid=source;invalid["connections"].push_back(edge("tick","then","set_shutdown","execute"));
    reject("cross_event_dependency",[&]{compile_blueprint(invalid,resolver);});
    invalid=source;invalid["connections"].push_back(edge("init","then","missing","execute"));
    reject("node_unavailable",[&]{compile_blueprint(invalid,resolver);});
    auto altered=resolver;altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.flags&=~0x08000000u;return signature;};
    reject("event_not_overridable",[&]{compile_blueprint(source,altered);});
    altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.flags&=~0x800u;return signature;};
    reject("event_not_overridable",[&]{compile_blueprint(source,altered);});
    altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.flags|=1;return signature;};
    reject("event_not_overridable",[&]{compile_blueprint(source,altered);});
    altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.flags|=0x40;return signature;};
    reject("event_contract_unsupported",[&]{compile_blueprint(source,altered);});
    altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.latent=true;return signature;};
    reject("latent_event_unsupported",[&]{compile_blueprint(source,altered);});
    altered.function=[resolver](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);signature.parameters.push_back({"Result",signature.symbol+"/Result",Type{"bool"},Direction::output});return signature;};
    reject("event_parameter_unsupported",[&]{compile_blueprint(source,altered);});
    auto receiver_resolver=resolver;
    receiver_resolver.object=[](const std::string&,const Type&){return "object:AnotherGameInstance";};
    invalid=source;invalid["nodes"][1]["objects"]={{"self","/CompilerCheck.AnotherGameInstance"}};
    reject("parent_context_receiver_invalid",[&]{compile_blueprint(invalid,receiver_resolver);});
    unsigned init_resolutions{};altered=resolver;
    altered.function=[&](const std::string& owner,const std::string& name){auto signature=resolver.function(owner,name);
        if(name=="ReceiveInit"&&++init_resolutions>1)signature.parameters.push_back({"Changed",signature.symbol+"/Changed",Type{"float"},Direction::input});
        return signature;};
    reject("event_signature_changed",[&]{compile_blueprint(source,altered);});
    auto outside=asset(literal_function("value"));outside["functions"][0]["nodes"].push_back(node("super","call_parent"));
    reject("parent_call_outside_event",[&]{compile_blueprint(outside,resolver);});
}
}
int main() {
    try{literal_and_binding();graph_and_call();ordinary_call_admission();compound_literals_and_split();sequence_flow_and_wildcard();numeric_conversion_contract();explicit_self_value();reflected_event_overrides();std::cout<<"runtime_blueprint_compiler: "<<checks<<" meaningful checks passed\n";return 0;}
    catch(const CompilerError& error){std::cerr<<error.code<<": "<<error.what()<<" ["<<error.node<<"/"<<error.pin<<"]\n";return 1;}
    catch(const std::exception& error){std::cerr<<error.what()<<"\n";return 1;}
}
