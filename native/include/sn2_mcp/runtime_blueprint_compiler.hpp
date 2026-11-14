#pragma once
#include <nlohmann/json.hpp>
#include <cstdint>
#include <functional>
#include <map>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

namespace sn2_mcp::runtime_blueprint {
// UE 5.6.1 KismetCompilerVMBackend.cpp and ScriptCore.cpp define the emitted
// instruction contract. Graph input remains the bridge's authoring schema 1.
struct Type {
    std::string kind;
    std::string path;
    bool array{};
    bool operator==(const Type&) const = default;
};
enum class Direction { input, output, return_value, input_output };
enum class PropertyRole { member, input, output, local };
struct Parameter {
    std::string name;
    std::string symbol;
    Type type;
    Direction direction{Direction::input};
    std::string default_literal;
    bool default_to_self{};
    bool wildcard{};
    bool const_reference{};
};
struct FunctionSignature {
    std::string symbol;
    std::string name;
    std::string owner_path;
    std::uint32_t flags{};
    std::vector<Parameter> parameters;
    std::string default_object_symbol;
    bool latent{};
    bool editor_only{};
    bool pure{};
};
struct StructMember {std::string name;std::string symbol;Type type;std::int32_t array_dim{1};};
struct StructSignature {
    std::string symbol;
    std::int32_t size{};
    // Exact PropertyLink iteration order, excluding Transient/EditorOnly fields.
    // Fixed ArrayDim elements are serialized in order for each property.
    std::vector<StructMember> members;
};
struct Resolver {
    std::function<FunctionSignature(const std::string&,const std::string&)> function;
    std::function<StructSignature(const std::string&)> structure;
    std::function<std::int64_t(const std::string&,const std::string&)> enumeration;
    std::function<std::string(const std::string&,const Type&)> object;
    std::function<bool(const Type&,const Type&)> compatible;
    // For compound Unreal literals, import through the actual reflected type
    // and return typed JSON fields. Scalar literals have a portable fallback.
    std::function<nlohmann::json(const Type&,const std::string&)> literal;
};
struct PropertyDefinition {
    std::string name;
    std::string symbol;
    Type type;
    PropertyRole role{};
    bool const_reference{};
    std::string default_literal;
};
enum class RelocationKind { pointer, script_name };
struct Relocation {
    std::size_t offset{};
    RelocationKind kind{};
    std::string symbol;
    std::string node;
    std::string pin;
};
struct SourceRange {std::size_t begin{},end{};std::string node;};
struct CompiledFunction {
    std::string name;
    std::string symbol;
    std::uint32_t flags{};
    std::vector<PropertyDefinition> properties;
    std::vector<std::uint8_t> bytecode;
    std::vector<Relocation> relocations;
    std::vector<SourceRange> source_ranges;
    bool event_override{};
    std::string super_function_symbol;
};
struct Program {
    std::string class_path;
    std::string parent_path;
    std::vector<PropertyDefinition> members;
    std::vector<CompiledFunction> functions;
};
class CompilerError : public std::runtime_error {
public:
    CompilerError(std::string code,std::string message,std::string node={},std::string pin={});
    const std::string code,node,pin;
};
Program compile_blueprint(const nlohmann::json& asset,const Resolver& resolver);
struct BoundSymbol {
    std::uint64_t pointer{};
    std::uint32_t comparison_index{},display_index{},number{};
    std::string identity;
};
struct BoundScript {
    std::vector<std::uint8_t> bytecode;
    std::map<std::string,std::string> referenced_identities;
};
// Binding never accepts a zero pointer, missing identity, overlapping write or
// missing symbol. The caller validates each identity again before publication.
BoundScript bind_script(const CompiledFunction& function,
    const std::function<BoundSymbol(const Relocation&)>& resolve);
}
