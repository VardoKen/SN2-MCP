#include "sn2_mcp/runtime_blueprint_compiler.hpp"
#include <algorithm>
#include <bit>
#include <charconv>
#include <cmath>
#include <cstring>
#include <limits>
#include <memory>
#include <set>
#include <sstream>
#include <utility>

namespace sn2_mcp::runtime_blueprint {
using json=nlohmann::json;
CompilerError::CompilerError(std::string c,std::string m,std::string n,std::string p)
    :std::runtime_error(std::move(m)),code(std::move(c)),node(std::move(n)),pin(std::move(p)){}
namespace {
constexpr std::uint32_t final_function=1,native_function=0x400,static_function=0x2000;
constexpr std::uint32_t public_callable_event=0x0c020000,pure_function=0x10000000;
enum Token : std::uint8_t {
    local=0x00,member=0x01,ret=0x04,jump=0x06,jump_if_not=0x07,nothing=0x0b,let=0x0f,
    let_bool=0x14,end_parameters=0x16,self=0x17,context=0x19,virtual_call=0x1b,
    final_call=0x1c,int_const=0x1d,float_const=0x1e,string_const=0x1f,object_const=0x20,
    name_const=0x21,rotation_const=0x22,vector_const=0x23,byte_const=0x24,
    true_const=0x27,false_const=0x28,text_const=0x29,no_object=0x2a,struct_const=0x2f,
    end_struct=0x30,set_array=0x31,end_array=0x32,unicode_const=0x34,int64_const=0x35,
    double_const=0x37,cast=0x38,struct_context=0x42,local_out=0x48,push_flow=0x4c,
    pop_flow=0x4d,end_script=0x53,let_object=0x5f,array_const=0x65,end_array_const=0x66
};
[[noreturn]] void fail(const std::string& code,const std::string& message,
    const std::string& node={},const std::string& pin={}){throw CompilerError(code,message,node,pin);}
std::string text(const json& value,const char* key,const std::string& node={}) {
    if(!value.is_object() || !value.contains(key) || !value.at(key).is_string()
       || value.at(key).get_ref<const std::string&>().empty())fail("invalid_spec",std::string("Nonempty string required: ")+key,node);
    return value.at(key).get<std::string>();
}
const json& array(const json& value,const char* key) {
    if(!value.is_object() || !value.contains(key) || !value.at(key).is_array())fail("invalid_spec",std::string("Array required: ")+key);
    return value.at(key);
}
bool valid_name(const std::string& name) {
    return !name.empty() && name.find_first_of("\"' ,/\\.:;{}[]()=+*&!?\t\r\n")==std::string::npos
        && name.find('\0')==std::string::npos;
}
Type type(const json& declaration) {
    Type result{text(declaration,"type"),{},false};
    if(result.kind=="object" || result.kind=="class")result.path=text(declaration,"class_path");
    else if(result.kind=="struct")result.path=text(declaration,"struct_path");
    else if(result.kind=="enum")result.path=text(declaration,"enum_path");
    else if(result.kind!="bool" && result.kind!="int" && result.kind!="int64" && result.kind!="float"
         && result.kind!="double" && result.kind!="string" && result.kind!="name" && result.kind!="text")
        fail("unsupported_variable_type","Unsupported authoring type: "+result.kind);
    if(declaration.contains("container")) {
        if(text(declaration,"container")!="array")fail("unsupported_container_type","Only schema 1 arrays are supported");
        result.array=true;
    }
    return result;
}
struct Expression {
    enum class Kind {property,literal,object,self,structure_field};
    Kind kind{Kind::property};Type type;std::string symbol,value,node,pin;
    std::shared_ptr<Expression> parent;
    std::string producer;
};
struct Pin {bool output{},exec{};Type type;Expression expression;};
struct Node {
    std::string id,kind;json spec;std::map<std::string,Pin> pins;
    std::optional<FunctionSignature> signature;std::string variable;bool pure{};
};
struct Edge {std::string from_node,from_pin,to_node,to_pin;};
bool same_source(const Expression& a,const Expression& b) {
    if(a.kind!=b.kind||a.type!=b.type||a.symbol!=b.symbol||a.value!=b.value)return false;
    if(bool(a.parent)!=bool(b.parent))return false;
    return !a.parent||same_source(*a.parent,*b.parent);
}
struct Writer {
    CompiledFunction& output;
    std::string node,pin;
    void token(Token value){output.bytecode.push_back(value);}
    template<class T>void value(T value) {
        static_assert(std::is_trivially_copyable_v<T>);
        if constexpr(std::endian::native!=std::endian::little)fail("unsupported_endianness","Unreal Win64 bytecode requires little-endian output");
        const auto* bytes=reinterpret_cast<const std::uint8_t*>(&value);
        output.bytecode.insert(output.bytecode.end(),bytes,bytes+sizeof(T));
    }
    std::size_t placeholder(){const auto index=output.bytecode.size();value(std::uint32_t{});return index;}
    void patch(std::size_t offset,std::size_t target) {
        if(target>std::numeric_limits<std::uint32_t>::max() || offset+4>output.bytecode.size())
            fail("bytecode_offset_overflow","Jump/context offset exceeds Unreal CodeSkipSizeType",node,pin);
        const auto value=static_cast<std::uint32_t>(target);
        for(unsigned i=0;i<4;++i)output.bytecode[offset+i]=static_cast<std::uint8_t>(value>>(8*i));
    }
    void relocation(const std::string& symbol,RelocationKind kind=RelocationKind::pointer) {
        if(symbol.empty())fail("unresolved_symbol","An emitted engine reference requires a symbol",node,pin);
        output.relocations.push_back({output.bytecode.size(),kind,symbol,node,pin});
        output.bytecode.resize(output.bytecode.size()+(kind==RelocationKind::pointer?8:12),0);
    }
    void string(const std::string& input) {
        if(input.find('\0')!=std::string::npos)fail("invalid_pin_default","String literals cannot contain embedded nulls",node,pin);
        if(std::all_of(input.begin(),input.end(),[](unsigned char c){return c<128;})) {
            token(string_const);output.bytecode.insert(output.bytecode.end(),input.begin(),input.end());output.bytecode.push_back(0);return;
        }
        token(unicode_const);
        for(std::size_t i=0;i<input.size();) {
            const auto first=static_cast<unsigned char>(input[i++]);std::uint32_t code{};unsigned count{};
            if(first<128){code=first;count=0;}
            else if((first&0xe0)==0xc0){code=first&0x1f;count=1;}
            else if((first&0xf0)==0xe0){code=first&0x0f;count=2;}
            else if((first&0xf8)==0xf0){code=first&7;count=3;}
            else fail("invalid_utf8","Invalid UTF-8 literal",node,pin);
            for(unsigned j=0;j<count;++j) {
                if(i==input.size() || (static_cast<unsigned char>(input[i])&0xc0)!=0x80)fail("invalid_utf8","Incomplete UTF-8 literal",node,pin);
                code=(code<<6)|(static_cast<unsigned char>(input[i++])&0x3f);
            }
            if((count==1 && code<128)||(count==2 && code<2048)||(count==3 && code<65536)
               ||code>0x10ffff||(code>=0xd800&&code<=0xdfff))fail("invalid_utf8","Noncanonical UTF-8 literal",node,pin);
            if(code<=65535)value(static_cast<std::uint16_t>(code));
            else {code-=65536;value(static_cast<std::uint16_t>(0xd800+(code>>10)));value(static_cast<std::uint16_t>(0xdc00+(code&1023)));}
        }
        value(std::uint16_t{});
    }
};
class GraphCompiler {
    const Resolver& resolver;Program& program;CompiledFunction& output;Writer writer;
    std::map<std::string,Node> nodes;std::vector<Edge> edges;
    std::map<std::string,Expression> members,inputs,outputs;
    std::map<std::string,std::size_t> labels;
    std::vector<std::pair<std::size_t,std::string>> fixups;
    std::set<std::string> pure_emitted,pure_active;
    bool flow_stack{};std::string entry_id,result_id;
    std::string qualified(const std::string& id)const{return output.name+"/"+id;}
    Expression property(const PropertyDefinition& definition)const {
        return {Expression::Kind::property,definition.type,definition.symbol,{}, {},{},nullptr,{}};
    }
    void add_pin(Node& node,const std::string& name,bool is_output,const Type& t,Expression expression={}) {
        if(node.pins.contains(name))fail("pin_collision","A generated pin name collides",qualified(node.id),name);
        expression.type=t;expression.node=qualified(node.id);expression.pin=name;
        node.pins.emplace(name,Pin{is_output,false,t,std::move(expression)});
    }
    void exec_pin(Node& node,const std::string& name,bool is_output) {
        if(node.pins.contains(name))fail("pin_collision","A control pin name collides",qualified(node.id),name);
        node.pins.emplace(name,Pin{is_output,true,{},{}});
    }
    Expression temp(Node& node,const Parameter& parameter) {
        const auto symbol=output.symbol+"/local:"+node.id+"."+parameter.name;
        PropertyDefinition definition{temporary_name(),symbol,parameter.type,PropertyRole::local,false,{}};
        output.properties.push_back(definition);auto expression=property(definition);expression.producer=node.id;return expression;
    }
    std::string temporary_name()const {
        std::size_t index{};
        for(;;++index) {
            const auto name="__SN2MCPTemp_"+std::to_string(index);
            if(std::none_of(output.properties.begin(),output.properties.end(),[&](const auto& definition){return definition.name==name;}))return name;
        }
    }
    bool compatible(const Type& a,const Type& b)const {
        if(a==b)return true;
        if(!a.array&&!b.array&&((a.kind=="float"&&b.kind=="double")||(a.kind=="double"&&b.kind=="float")))return true;
        return resolver.compatible && resolver.compatible(a,b);
    }
    bool wildcard_pin(const Node& node,const std::string& name)const {
        if(node.signature)for(const auto& parameter:node.signature->parameters)
            if(parameter.name==name&&parameter.wildcard)return true;
        return false;
    }
    Pin& pin(const std::string& node,const std::string& name,bool is_output) {
        auto found=nodes.find(node);if(found==nodes.end())fail("node_unavailable","Connection refers to a missing node",qualified(node),name);
        auto field=found->second.pins.find(name);
        if(field==found->second.pins.end() || field->second.output!=is_output)fail("pin_unavailable","Connection refers to a missing directional pin",qualified(node),name);
        return field->second;
    }
    const Edge* incoming(const std::string& node,const std::string& name)const {
        for(const auto& edge:edges)if(edge.to_node==node&&edge.to_pin==name)return &edge;return nullptr;
    }
    std::string target(const std::string& node,const std::string& name)const {
        for(const auto& edge:edges)if(edge.from_node==node&&edge.from_pin==name)return edge.to_node;return {};
    }
    Expression input(Node& node,const std::string& name) {
        auto& selected=pin(node.id,name,false);writer.node=qualified(node.id);writer.pin=name;
        if(selected.exec)fail("exec_as_value","Execution pins do not carry values",writer.node,name);
        if(const auto* edge=incoming(node.id,name)) {
            auto expression=pin(edge->from_node,edge->from_pin,true).expression;
            if(!expression.producer.empty())ensure_pure(expression.producer);
            if(!compatible(expression.type,selected.type))fail("connection_invalid","Linked value types are incompatible",writer.node,name);
            return expression;
        }
        auto expression=selected.expression;
        if(!expression.parent && expression.symbol.empty() && expression.value.empty()
           && expression.kind==Expression::Kind::property) {
            expression.kind=Expression::Kind::literal;
        }
        return expression;
    }
    void parse_node(const json& spec) {
        Node node;text(spec,"id");node.id=text(spec,"id");node.kind=text(spec,"kind",qualified(node.id));node.spec=spec;
        if(nodes.contains(node.id))fail("duplicate_node","Node identifiers must be unique",qualified(node.id));
        if(node.kind=="function_entry") {
            if(!entry_id.empty())fail("duplicate_terminator","Function entry is duplicated",qualified(node.id));entry_id=node.id;
            exec_pin(node,"then",true);for(const auto&[name,expression]:inputs)add_pin(node,name,true,expression.type,expression);
        } else if(node.kind=="function_result") {
            if(!result_id.empty())fail("duplicate_terminator","Function result is duplicated",qualified(node.id));result_id=node.id;
            exec_pin(node,"execute",false);for(const auto&[name,expression]:outputs)add_pin(node,name,false,expression.type);
        } else if(node.kind=="variable_get"||node.kind=="variable_set") {
            node.variable=text(spec,"variable",qualified(node.id));const auto found=members.find(node.variable);
            if(found==members.end())fail("variable_unavailable","Variable is not declared in this Blueprint",qualified(node.id),node.variable);
            add_pin(node,"self",false,Type{"object",program.parent_path,false},Expression{Expression::Kind::self});
            if(node.kind=="variable_get") {node.pure=true;auto expression=found->second;expression.producer=node.id;add_pin(node,node.variable,true,found->second.type,expression);}
            else {exec_pin(node,"execute",false);exec_pin(node,"then",true);add_pin(node,node.variable,false,found->second.type);add_pin(node,"Output_Get",true,found->second.type,found->second);}
        } else if(node.kind=="call_function") {
            if(!resolver.function)fail("function_resolver_missing","Runtime reflection is required to resolve call_function",qualified(node.id));
            auto signature=resolver.function(text(spec,"class",qualified(node.id)),text(spec,"function",qualified(node.id)));
            if(signature.symbol.empty()||signature.owner_path.empty()||signature.name.empty())fail("function_unavailable","Resolved function has no exact identity",qualified(node.id));
            if(signature.latent)fail("latent_function_in_function_graph","Synchronous runtime functions cannot contain latent calls",qualified(node.id));
            if(signature.editor_only)fail("function_not_shipping","Editor-only functions cannot execute in Shipping",qualified(node.id));
            node.pure=signature.pure||(signature.flags&pure_function);
            if(!node.pure){exec_pin(node,"execute",false);exec_pin(node,"then",true);}
            Expression receiver{Expression::Kind::self};
            if(signature.flags&static_function) {receiver.kind=Expression::Kind::object;receiver.symbol=signature.default_object_symbol;
                if(receiver.symbol.empty())fail("function_default_object_missing","Static calls require the reflected owner CDO symbol",qualified(node.id),"self");}
            add_pin(node,"self",false,Type{"object",signature.owner_path,false},receiver);
            for(const auto& parameter:signature.parameters) {
                if(parameter.direction==Direction::input||parameter.direction==Direction::input_output) {
                    Expression expression{parameter.default_to_self?Expression::Kind::self:Expression::Kind::literal};
                    expression.value=parameter.default_literal;
                    add_pin(node,parameter.name,false,parameter.type,expression);
                }
                if(parameter.direction==Direction::output||parameter.direction==Direction::return_value) {
                    add_pin(node,parameter.name,true,parameter.type,temp(node,parameter));
                }
            }
            node.signature=std::move(signature);
        } else if(node.kind=="branch") {
            exec_pin(node,"execute",false);exec_pin(node,"then",true);exec_pin(node,"else",true);add_pin(node,"Condition",false,Type{"bool",{},false});
        } else if(node.kind=="sequence") {
            exec_pin(node,"execute",false);int count=2;
            if(spec.contains("outputs")) {
                if(!spec.at("outputs").is_number())fail("invalid_sequence_outputs","Sequence outputs must be numeric",qualified(node.id));
                const auto number=spec.at("outputs").get<double>();
                if(!std::isfinite(number)||number<1.5||number>std::numeric_limits<int>::max())fail("invalid_sequence_outputs","Sequence needs at least two outputs",qualified(node.id));
                count=static_cast<int>(std::round(number));
            }
            for(int i=0;i<count;++i)exec_pin(node,"then_"+std::to_string(i),true);flow_stack=true;
        } else fail("unsupported_node","Unsupported schema 1 node kind: "+node.kind,qualified(node.id));
        if(spec.contains("split_pins"))for(const auto& item:array(spec,"split_pins")) {
            if(!item.is_string())fail("invalid_split_pin","Split pins must contain names",qualified(node.id));
            const auto name=item.get<std::string>();auto found=node.pins.find(name);
            if(found==node.pins.end()||found->second.exec||found->second.type.array||found->second.type.kind!="struct")fail("invalid_split_pin","Split requires an existing scalar struct pin",qualified(node.id),name);
            if(!resolver.structure)fail("struct_resolver_missing","Split pins require runtime struct reflection",qualified(node.id),name);
            const auto structure=resolver.structure(found->second.type.path);
            if(structure.symbol.empty()||structure.size<=0)fail("struct_unavailable","Struct resolver did not return a complete identity",qualified(node.id),name);
            const auto parent=found->second.expression;const auto is_output=found->second.output;
            for(const auto& field:structure.members) {
                if(field.array_dim!=1)fail("split_fixed_array_not_supported","Split output pins require scalar reflected struct members",qualified(node.id),name+"_"+field.name);
                Expression expression{Expression::Kind::structure_field,field.type,field.symbol,{}, {},{},std::make_shared<Expression>(parent),parent.producer};
                add_pin(node,name+"_"+field.name,is_output,field.type,expression);
            }
            if(!is_output)fail("split_input_not_supported","Runtime compiler currently requires a whole struct input; output split pins are supported",qualified(node.id),name);
        }
        for(const char* values:{"defaults","objects"})if(spec.contains(values)) {
            if(!spec.at(values).is_object())fail("invalid_pin_defaults","Pin defaults must be an object",qualified(node.id));
            for(const auto&[name,value]:spec.at(values).items()) {
                auto found=node.pins.find(name);
                if(found==node.pins.end()||found->second.output||found->second.exec)fail("pin_unavailable","Default requires an actual data input",qualified(node.id),name);
                if(!value.is_string())fail("invalid_pin_default","Pin literals must be strings",qualified(node.id),name);
                auto& expression=found->second.expression;expression.value=value.get<std::string>();expression.kind=Expression::Kind::literal;
                if(std::strcmp(values,"objects")==0) {
                    if(!resolver.object)fail("object_resolver_missing","Object defaults require runtime object resolution",qualified(node.id),name);
                    expression.kind=Expression::Kind::object;expression.symbol=resolver.object(expression.value,found->second.type);
                    if(expression.symbol.empty())fail("object_unavailable","Default object has no resolved identity",qualified(node.id),name);
                }
            }
        }
        nodes.emplace(node.id,std::move(node));
    }
    void connect(const json& spec) {
        for(const auto& item:array(spec,"connections")) {
            if(!item.is_object()||!item.contains("from")||!item.contains("to"))fail("invalid_connection","Connection requires from/to objects");
            Edge edge{text(item.at("from"),"node"),text(item.at("from"),"pin"),text(item.at("to"),"node"),text(item.at("to"),"pin")};
            auto& from=pin(edge.from_node,edge.from_pin,true);auto& to=pin(edge.to_node,edge.to_pin,false);
            if(from.exec!=to.exec)fail("connection_invalid","Execution and data pins cannot connect",qualified(edge.to_node),edge.to_pin);
            if(incoming(edge.to_node,edge.to_pin)&&!to.exec)fail("connection_replacement","Data input already has a connection",qualified(edge.to_node),edge.to_pin);
            if(from.exec&&!target(edge.from_node,edge.from_pin).empty())fail("connection_replacement","Execution output already has a connection",qualified(edge.from_node),edge.from_pin);
            if(!from.exec&&!compatible(from.type,to.type)) {
                const bool wildcard=wildcard_pin(nodes.at(edge.to_node),edge.to_pin)||wildcard_pin(nodes.at(edge.from_node),edge.from_pin);
                if(!wildcard)fail("connection_invalid","Data pin types are incompatible",qualified(edge.to_node),edge.to_pin);
            }
            if(nodes.at(edge.to_node).signature)for(const auto& parameter:nodes.at(edge.to_node).signature->parameters)
                if(parameter.name==edge.to_pin&&parameter.direction==Direction::input_output&&!parameter.wildcard&&from.type!=to.type)
                    fail("mutable_reference_type_mismatch","Mutable references require the exact reflected value type",qualified(edge.to_node),edge.to_pin);
            edges.push_back(std::move(edge));
        }
        // Array wildcard signatures carry placeholder int properties. Infer the
        // dependent arguments from the connected actual array, as CallArrayFunction
        // does, before argument validation or emission.
        for(auto&[id,node]:nodes)if(node.signature) {
            std::optional<Type> element;
            for(const auto& parameter:node.signature->parameters)if(parameter.wildcard&&parameter.type.array) {
                if(const auto* edge=incoming(id,parameter.name)) {element=pin(edge->from_node,edge->from_pin,true).type;element->array=false;}
            }
            if(element)for(const auto& parameter:node.signature->parameters)if(parameter.wildcard) {
                auto inferred=*element;inferred.array=parameter.type.array;
                auto field=node.pins.find(parameter.name);if(field!=node.pins.end()){field->second.type=inferred;field->second.expression.type=inferred;}
                if(field!=node.pins.end()&&field->second.output)
                    for(auto& definition:output.properties)if(definition.symbol==field->second.expression.symbol)definition.type=inferred;
            }
        }
        for(const auto& edge:edges) {
            const auto& from=pin(edge.from_node,edge.from_pin,true);const auto& to=pin(edge.to_node,edge.to_pin,false);
            if(!from.exec&&(wildcard_pin(nodes.at(edge.to_node),edge.to_pin)||wildcard_pin(nodes.at(edge.from_node),edge.from_pin))&&from.type!=to.type)
                fail("wildcard_type_mismatch","Wildcard operands require the exact inferred reflected type",qualified(edge.to_node),edge.to_pin);
            if(!from.exec&&!compatible(from.type,to.type))fail("connection_invalid","Inferred data pin types are incompatible",qualified(edge.to_node),edge.to_pin);
            if(nodes.at(edge.to_node).signature)for(const auto& parameter:nodes.at(edge.to_node).signature->parameters)
                if(parameter.name==edge.to_pin&&parameter.direction==Direction::input_output&&from.type!=to.type)
                    fail("mutable_reference_type_mismatch","Mutable references require the exact inferred type",qualified(edge.to_node),edge.to_pin);
        }
    }
    json literal_value(const Expression& expression) {
        if(resolver.literal)return resolver.literal(expression.type,expression.value);
        const auto& t=expression.type;const auto& input=expression.value;
        if(t.array) {
            if(input.empty()||input=="()"||input=="[]")return json::array();
            fail("literal_import_required","Nonempty array literals require the real Unreal property importer",writer.node,writer.pin);
        }
        if(t.kind=="string"||t.kind=="text"||t.kind=="name"||t.kind=="object"||t.kind=="class"||t.kind=="enum")return input;
        if(t.kind=="bool") {if(input.empty()||input=="false"||input=="False"||input=="0")return false;if(input=="true"||input=="True"||input=="1")return true;fail("invalid_pin_default","Boolean literal is invalid",writer.node,writer.pin);}
        if(t.kind=="int"||t.kind=="int64") {
            if(input.empty())return std::int64_t{};std::int64_t value{};const auto parsed=std::from_chars(input.data(),input.data()+input.size(),value);
            if(parsed.ec!=std::errc{}||parsed.ptr!=input.data()+input.size()||(t.kind=="int"&&(value<std::numeric_limits<std::int32_t>::min()||value>std::numeric_limits<std::int32_t>::max())))fail("invalid_pin_default","Integer literal is outside its reflected type",writer.node,writer.pin);return value;
        }
        if(t.kind=="float"||t.kind=="double") {
            if(input.empty())return 0.0;double value{};const auto parsed=std::from_chars(input.data(),input.data()+input.size(),value);
            if(parsed.ec!=std::errc{}||parsed.ptr!=input.data()+input.size()||!std::isfinite(value)||(t.kind=="float"&&!std::isfinite(static_cast<float>(value))))fail("invalid_pin_default","Real literal is invalid",writer.node,writer.pin);return value;
        }
        fail("literal_import_required","Struct literals require the actual Unreal property importer",writer.node,writer.pin);
    }
    void emit_literal(const Type& t,const json& value,const std::string& property_symbol={}) {
        if(t.array) {
            if(!value.is_array())fail("invalid_literal_import","Imported array literal must be a JSON array",writer.node,writer.pin);
            if(property_symbol.empty())fail("array_literal_property_missing","Array constants require an exact inner FProperty symbol",writer.node,writer.pin);
            writer.token(array_const);writer.relocation(property_symbol+"/inner");writer.value(static_cast<std::int32_t>(value.size()));
            auto inner=t;inner.array=false;for(const auto& item:value)emit_literal(inner,item);writer.token(end_array_const);return;
        }
        if(t.kind=="bool")writer.token(value.get<bool>()?true_const:false_const);
        else if(t.kind=="int"){writer.token(int_const);writer.value(value.get<std::int32_t>());}
        else if(t.kind=="int64"){writer.token(int64_const);writer.value(value.get<std::int64_t>());}
        else if(t.kind=="float"){writer.token(float_const);writer.value(value.get<float>());}
        else if(t.kind=="double"){writer.token(double_const);writer.value(value.get<double>());}
        else if(t.kind=="string")writer.string(value.get<std::string>());
        else if(t.kind=="text") {const auto literal=value.get<std::string>();writer.token(text_const);writer.value(std::uint8_t{literal.empty()?std::uint8_t{0}:std::uint8_t{2}});if(!literal.empty())writer.string(literal);}
        else if(t.kind=="name") {writer.token(name_const);writer.relocation("name:"+value.get<std::string>(),RelocationKind::script_name);}
        else if(t.kind=="enum") {
            if(!resolver.enumeration)fail("enum_resolver_missing","Enum literals require runtime enum resolution",writer.node,writer.pin);
            const auto v=resolver.enumeration(t.path,value.get<std::string>());
            if(v<0||v>255)fail("invalid_pin_default","K2 byte-enum literal is out of range",writer.node,writer.pin);
            writer.token(byte_const);writer.value(static_cast<std::uint8_t>(v));
        } else if(t.kind=="object"||t.kind=="class") {
            const auto path=value.is_null()?std::string{}:value.get<std::string>();
            if(path.empty()||path=="None")writer.token(no_object);
            else {if(!resolver.object)fail("object_resolver_missing","Object literals require runtime object resolution",writer.node,writer.pin);writer.token(object_const);writer.relocation(resolver.object(path,t));}
        } else if(t.kind=="struct") {
            if(!resolver.structure)fail("struct_resolver_missing","Struct literals require runtime reflection",writer.node,writer.pin);
            const auto structure=resolver.structure(t.path);
            if(!value.is_object()||structure.symbol.empty()||structure.size<=0)fail("invalid_literal_import","Imported struct must contain reflected typed fields",writer.node,writer.pin);
            writer.token(struct_const);writer.relocation(structure.symbol);writer.value(structure.size);
            for(const auto& field:structure.members) {
                if(!value.contains(field.name))fail("invalid_literal_import","Imported struct field is absent: "+field.name,writer.node,writer.pin);
                if(field.array_dim<1)fail("invalid_struct_layout","Struct member ArrayDim must be positive",writer.node,writer.pin);
                if(field.array_dim==1)emit_literal(field.type,value.at(field.name),field.symbol);
                else {
                    const auto& elements=value.at(field.name);
                    if(!elements.is_array()||elements.size()!=static_cast<std::size_t>(field.array_dim))fail("invalid_literal_import","Fixed-size struct member has the wrong element count",writer.node,writer.pin);
                    for(const auto& element:elements)emit_literal(field.type,element,field.symbol);
                }
            }
            writer.token(end_struct);
        } else fail("unsupported_literal","Unsupported literal type",writer.node,writer.pin);
    }
    void emit_expression(const Expression& expression,const Type* target_type=nullptr,const std::string& coerce_property={}) {
        const bool real_conversion=target_type&&!expression.type.array&&!target_type->array&&expression.type.kind!=target_type->kind
           &&((expression.type.kind=="float"&&target_type->kind=="double")||(expression.type.kind=="double"&&target_type->kind=="float"));
        if(real_conversion&&expression.kind==Expression::Kind::literal) {
            const auto value=literal_value(expression);
            if(!value.is_number()||!std::isfinite(value.get<double>())
               ||(target_type->kind=="float"&&!std::isfinite(static_cast<float>(value.get<double>()))))
                fail("invalid_pin_default","Real literal conversion is not finite",writer.node,writer.pin);
            emit_literal(*target_type,value,coerce_property);return;
        }
        if(real_conversion) {
            writer.token(cast);writer.value(std::uint8_t{expression.type.kind=="double"?std::uint8_t{3}:std::uint8_t{4}});
        }
        if(expression.kind==Expression::Kind::self){writer.token(self);return;}
        if(expression.kind==Expression::Kind::object){writer.token(object_const);writer.relocation(expression.symbol);return;}
        if(expression.kind==Expression::Kind::literal){emit_literal(expression.type,literal_value(expression),coerce_property);return;}
        if(expression.kind==Expression::Kind::structure_field) {
            writer.token(struct_context);writer.relocation(expression.symbol);emit_expression(*expression.parent);return;
        }
        const auto found=std::find_if(output.properties.begin(),output.properties.end(),[&](const auto& definition){return definition.symbol==expression.symbol;});
        if(expression.symbol.starts_with("member:"))writer.token(member);
        else if(found!=output.properties.end()&&(found->role==PropertyRole::output||found->const_reference))writer.token(local_out);
        else writer.token(local);
        writer.relocation(expression.symbol);
    }
    void destination(const Expression& expression) {
        if(expression.type.array){writer.token(let);writer.relocation(expression.symbol);}
        else if(expression.type.kind=="bool")writer.token(let_bool);
        else if(expression.type.kind=="object"||expression.type.kind=="class")writer.token(let_object);
        else {writer.token(let);writer.relocation(expression.symbol);}
        emit_expression(expression);
    }
    void emit_assignment(const Expression& to,const Expression& from) {
        destination(to);emit_expression(from,&to.type,to.symbol);
    }
    void ensure_pure(const std::string& id) {
        auto& node=nodes.at(id);if(!node.pure)return;
        if(pure_emitted.contains(id))return;
        if(!pure_active.insert(id).second)fail("data_dependency_cycle","Pure data dependency contains a cycle",qualified(id));
        if(node.signature)call(node);
        else if(node.kind=="variable_get" && input(node,"self").kind!=Expression::Kind::self)
            fail("external_member_receiver_not_supported","Member getters currently require the compiled instance",qualified(node.id),"self");
        pure_active.erase(id);pure_emitted.insert(id);
    }
    void call(Node& node) {
        const auto& signature=*node.signature;
        std::vector<Expression> arguments;std::optional<Expression> returned;
        for(const auto& parameter:signature.parameters) {
            if(parameter.direction==Direction::return_value)returned=pin(node.id,parameter.name,true).expression;
            else if(parameter.direction==Direction::output)arguments.push_back(pin(node.id,parameter.name,true).expression);
            else {
                auto argument=input(node,parameter.name);
                const auto expected=pin(node.id,parameter.name,false).type;
                const bool addressable=argument.kind==Expression::Kind::property||argument.kind==Expression::Kind::structure_field;
                if(parameter.const_reference&&argument.type!=expected
                   ||(parameter.const_reference&&!(signature.flags&native_function)||parameter.wildcard&&parameter.type.array)&&!addressable) {
                    PropertyDefinition temporary{temporary_name(),
                        output.symbol+"/local:"+node.id+"."+parameter.name+".reference",expected,PropertyRole::local,false,{}};
                    const auto existing=std::find_if(output.properties.begin(),output.properties.end(),[&](const auto& definition){return definition.symbol==temporary.symbol;});
                    if(existing==output.properties.end())output.properties.push_back(temporary);
                    else if(existing->type!=temporary.type)fail("temporary_type_changed","A compiler temporary changed its reflected type",qualified(node.id),parameter.name);
                    const auto address=property(temporary);emit_assignment(address,argument);argument=address;
                }
                if(parameter.direction==Direction::input_output && argument.kind!=Expression::Kind::property
                   && argument.kind!=Expression::Kind::structure_field)
                    fail("mutable_reference_requires_variable","A mutable reference argument requires a linked writable property",qualified(node.id),parameter.name);
                if(parameter.direction==Direction::input_output) {
                    const Expression* root=&argument;while(root->parent)root=root->parent.get();
                    const auto definition=std::find_if(output.properties.begin(),output.properties.end(),[&](const auto& item){return item.symbol==root->symbol;});
                    if(definition!=output.properties.end()&&definition->const_reference)
                        fail("read_only_reference_argument","A read-only function input cannot be passed to a mutable native reference",qualified(node.id),parameter.name);
                }
                arguments.push_back(std::move(argument));
            }
        }
        auto receiver=input(node,"self");writer.node=qualified(node.id);writer.pin={};
        if(receiver.kind==Expression::Kind::self && !(signature.flags&static_function)
           &&!compatible(Type{"object",program.parent_path,false},Type{"object",signature.owner_path,false}))
            fail("incompatible_self_receiver","The compiled Blueprint instance is incompatible with the called function owner",qualified(node.id),"self");
        std::size_t checked_argument{};
        for(const auto& parameter:signature.parameters)if(parameter.direction!=Direction::return_value) {
            if(parameter.direction==Direction::input_output && arguments[checked_argument].type.array)
                for(std::size_t prior=0;prior<checked_argument;++prior)
                if(same_source(arguments[prior],arguments[checked_argument]))
                    fail("aliased_mutable_argument","A mutable native argument aliases another parameter",qualified(node.id),parameter.name);
            ++checked_argument;
        }
        if(signature.flags&native_function) {
            std::size_t argument_index{};
            for(const auto& parameter:signature.parameters)if(parameter.direction!=Direction::return_value) {
                const auto& argument=arguments[argument_index++];
                if(parameter.direction==Direction::output&&parameter.type.array) {
                    writer.token(set_array);emit_expression(argument);writer.token(end_array);
                }
            }
        }
        if(returned)destination(*returned);
        writer.token(context);emit_expression(receiver);const auto skip=writer.placeholder();
        if(returned)writer.relocation(returned->symbol);else writer.value(std::uint64_t{});
        const auto context_start=output.bytecode.size();
        if(signature.flags&final_function){writer.token(final_call);writer.relocation(signature.symbol);}
        else {writer.token(virtual_call);writer.relocation("name:"+signature.name,RelocationKind::script_name);}
        std::size_t index{};
        for(const auto& parameter:signature.parameters)if(parameter.direction!=Direction::return_value) {
            writer.pin=parameter.name;const auto& argument=arguments[index++];
            emit_expression(argument,parameter.wildcard?nullptr:&parameter.type,parameter.symbol);
        }
        writer.token(end_parameters);writer.patch(skip,output.bytecode.size()-context_start);
    }
    void goto_label(const std::string& label,Token opcode=jump) {
        writer.token(opcode);fixups.emplace_back(writer.placeholder(),label);
    }
    void continue_at(const std::string& label) {
        if(label.empty()){if(flow_stack)writer.token(pop_flow);else goto_label("$return");}
        else goto_label(label);
    }
    void emit_node(Node& node) {
        const auto begin=output.bytecode.size();writer.node=qualified(node.id);writer.pin={};pure_emitted.clear();
        if(node.kind=="function_entry")continue_at(target(node.id,"then"));
        else if(node.kind=="function_result") {
            const auto declarations=output.properties;
            for(const auto& definition:declarations)if(definition.role==PropertyRole::output)
                emit_assignment(property(definition),input(node,definition.name));
            goto_label("$return");
        } else if(node.kind=="variable_set") {
            auto value=input(node,node.variable);auto receiver=input(node,"self");
            if(receiver.kind!=Expression::Kind::self)fail("external_member_receiver_not_supported","Member setters currently require the compiled instance",qualified(node.id),"self");
            emit_assignment(members.at(node.variable),value);continue_at(target(node.id,"then"));
        } else if(node.kind=="call_function") {call(node);continue_at(target(node.id,"then"));}
        else if(node.kind=="branch") {
            auto condition=input(node,"Condition");writer.node=qualified(node.id);writer.pin="Condition";
            goto_label(target(node.id,"else").empty()?"$fallthrough":target(node.id,"else"),jump_if_not);emit_expression(condition);
            continue_at(target(node.id,"then"));
        } else if(node.kind=="sequence") {
            std::vector<std::string> branches;
            for(const auto&[name,p]:node.pins)if(p.exec&&p.output) {const auto next=target(node.id,name);if(!next.empty())branches.push_back(next);}
            // Pin names are sorted lexically; preserve the actual numeric order.
            branches.clear();for(std::size_t i=0;i+1<node.pins.size();++i) {const auto next=target(node.id,"then_"+std::to_string(i));if(!next.empty())branches.push_back(next);}
            for(std::size_t i=branches.size();i>1;--i)goto_label(branches[i-1],push_flow);
            continue_at(branches.empty()?std::string{}:branches.front());
        }
        output.source_ranges.push_back({begin,output.bytecode.size(),qualified(node.id)});
    }
public:
    GraphCompiler(const Resolver& r,Program& p,CompiledFunction& f):resolver(r),program(p),output(f),writer{f} {
        for(const auto& definition:program.members)members.emplace(definition.name,property(definition));
        for(const auto& definition:output.properties) {
            if(definition.role==PropertyRole::input)inputs.emplace(definition.name,property(definition));
            else if(definition.role==PropertyRole::output)outputs.emplace(definition.name,property(definition));
        }
    }
    void compile(const json& spec) {
        for(const auto& item:array(spec,"nodes"))parse_node(item);
        if(entry_id.empty()||result_id.empty())fail("function_terminator_unmapped","Function graph requires exactly one entry and result",output.name);
        connect(spec);
        std::vector<std::string> execution;std::set<std::string> visited;
        std::function<void(const std::string&)> visit=[&](const std::string& id) {
            if(id.empty()||!visited.insert(id).second)return;auto& node=nodes.at(id);
            if(node.pure)fail("pure_execution_connection","Pure nodes cannot appear in the execution chain",qualified(id));
            execution.push_back(id);
            for(const auto& edge:edges)if(edge.from_node==id&&pin(edge.from_node,edge.from_pin,true).exec)visit(edge.to_node);
        };
        visit(entry_id);
        if(flow_stack)goto_label("$return",push_flow);
        for(const auto& id:execution){labels[id]=output.bytecode.size();emit_node(nodes.at(id));}
        labels["$fallthrough"]=output.bytecode.size();if(flow_stack)writer.token(pop_flow);else goto_label("$return");
        labels["$return"]=output.bytecode.size();writer.token(ret);writer.token(nothing);writer.token(end_script);
        for(const auto&[offset,label]:fixups) {
            const auto found=labels.find(label);if(found==labels.end())fail("unresolved_execution_target","Execution target was not emitted: "+label,output.name);
            writer.patch(offset,found->second);
        }
        for(const auto& definition:output.properties) {
            if(definition.role==PropertyRole::output || definition.const_reference)output.flags|=0x00400000; // FUNC_HasOutParms
            if(definition.role==PropertyRole::local)output.flags|=0x00800000; // FUNC_HasDefaults
        }
    }
};
}
Program compile_blueprint(const json& asset,const Resolver& resolver) {
    if(text(asset,"kind")!="blueprint")fail("unsupported_asset_kind","Runtime Blueprint compiler requires a blueprint asset");
    Program program{text(asset,"path"),text(asset,"parent_class"),{}, {}};
    std::set<std::string> names;
    for(const auto& declaration:array(asset,"variables")) {
        const auto name=text(declaration,"name");if(!valid_name(name)||!names.insert(name).second)fail("variable_collision","Member names must be valid and unique",{},name);
        std::string literal;if(declaration.contains("default")) {if(!declaration.at("default").is_string())fail("invalid_variable_default","Member default must be an Unreal property literal",{},name);literal=declaration.at("default").get<std::string>();}
        program.members.push_back({name,"member:"+name,type(declaration),PropertyRole::member,false,literal});
    }
    // The established asset schema always contains base EventGraph arrays.
    array(asset,"nodes");array(asset,"connections");
    if(!array(asset,"nodes").empty())fail("event_graph_compilation_pending","Runtime compiler currently accepts synchronous named functions; EventGraph lowering must be integrated before event-graph publication");
    if(!asset.contains("functions"))fail("empty_functions","Runtime compilation requires an authored function graph");
    for(const auto& spec:array(asset,"functions")) {
        const auto name=text(spec,"name");
        if(!valid_name(name)||name=="EventGraph"||!names.insert(name).second)fail("function_name_collision","Function names must be valid and distinct from members",name);
        CompiledFunction function;function.name=name;function.symbol="function:"+name;function.flags=public_callable_event;
        std::set<std::string> parameters;
        for(const auto* key:{"inputs","outputs"})for(const auto& declaration:array(spec,key)) {
            const auto parameter=text(declaration,"name");
            if(!valid_name(parameter)||parameter=="execute"||parameter=="then"||parameter=="self"||!parameters.insert(parameter).second)
                fail("function_parameter_collision","Function parameters must be unique and distinct from control pins",name,parameter);
            const auto t=type(declaration);const bool input=std::strcmp(key,"inputs")==0;
            function.properties.push_back({parameter,function.symbol+"/parameter:"+parameter,t,input?PropertyRole::input:PropertyRole::output,input&&t.array,{}});
        }
        GraphCompiler(resolver,program,function).compile(spec);program.functions.push_back(std::move(function));
    }
    if(program.functions.empty())fail("empty_functions","Runtime compilation requires at least one function");
    return program;
}
BoundScript bind_script(const CompiledFunction& function,const std::function<BoundSymbol(const Relocation&)>& resolve) {
    if(!resolve)fail("symbol_resolver_missing","Binding requires actual engine symbol identities");
    BoundScript result{function.bytecode,{}};std::set<std::size_t> occupied;
    std::map<std::string,BoundSymbol> bound_values;
    for(const auto& relocation:function.relocations) {
        const auto width=relocation.kind==RelocationKind::pointer?8u:12u;
        if(relocation.offset>result.bytecode.size()||width>result.bytecode.size()-relocation.offset)fail("invalid_relocation","Reference lies outside bytecode",relocation.node,relocation.pin);
        for(std::size_t i=0;i<width;++i)if(!occupied.insert(relocation.offset+i).second)fail("overlapping_relocation","Engine references overlap",relocation.node,relocation.pin);
        const auto symbol=resolve(relocation);
        if(symbol.identity.empty()||(relocation.kind==RelocationKind::pointer&&!symbol.pointer))fail("unresolved_symbol","Reference requires a live nonzero identity: "+relocation.symbol,relocation.node,relocation.pin);
        auto [found,inserted]=result.referenced_identities.emplace(relocation.symbol,symbol.identity);
        if(!inserted&&found->second!=symbol.identity)fail("symbol_changed","An engine reference changed during binding",relocation.node,relocation.pin);
        const auto [value,first]=bound_values.emplace(relocation.symbol,symbol);
        if(!first&&(value->second.pointer!=symbol.pointer||value->second.comparison_index!=symbol.comparison_index
            ||value->second.display_index!=symbol.display_index||value->second.number!=symbol.number))
            fail("symbol_changed","An engine reference value changed during binding",relocation.node,relocation.pin);
        auto write=[&](std::size_t offset,std::uint64_t value,std::size_t bytes){for(std::size_t i=0;i<bytes;++i)result.bytecode[offset+i]=static_cast<std::uint8_t>(value>>(i*8));};
        if(relocation.kind==RelocationKind::pointer)write(relocation.offset,symbol.pointer,8);
        else {write(relocation.offset,symbol.comparison_index,4);write(relocation.offset+4,symbol.display_index,4);write(relocation.offset+8,symbol.number,4);}
    }
    return result;
}
}
