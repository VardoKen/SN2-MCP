#pragma once
#include <g1r_mcp/transport.hpp>
#include <algorithm>
#include <cstdint>
#include <functional>
#include <limits>
#include <map>
#include <set>
#include <string>
#include <utility>

namespace g1r_mcp {
inline bool valid_signature_token(const std::string& value) noexcept {
    if(value.size()<5 || value.size()>24 || value.compare(0,4,"sig-") || value[4]=='0')return false;
    return std::all_of(value.begin()+4,value.end(),[](unsigned char c){return c>='0' && c<='9';});
}
inline const nlohmann::json& checked_function_contract(const std::map<std::string,nlohmann::json>& saved,
                                                     const nlohmann::json& token,
                                                     const std::string& session,const std::string& handle,
                                                     const std::string& function_name) {
    if(!token.is_string() || !valid_signature_token(token.get_ref<const std::string&>()))
        throw HandlerError("invalid_signature_token","An issued sig-N token is required");
    const auto found=saved.find(token.get<std::string>());
    if(found==saved.end())throw HandlerError("unknown_signature_token","The token is absent from the current native session; read the signature again");
    const auto& contract=found->second;
    if(contract.at("session")!=session || contract.at("target_handle")!=handle || contract.at("function_name")!=function_name)
        throw HandlerError("signature_token_mismatch","The token belongs to a different session, object or function");
    return contract;
}
inline void require_same_function_contract(const nlohmann::json& expected,const nlohmann::json& actual) {
    if(expected!=actual)throw HandlerError("signature_changed","The reflected signature changed after being read; the call did not execute");
}
inline nlohmann::json public_parameter_type_metadata(const std::string& type,const nlohmann::json& auxiliary) {
    nlohmann::json result=nlohmann::json::object();
    auto descriptor=[](const nlohmann::json& value) {
        return nlohmann::json{{"name",value.at("name")},{"path",value.at("path")}};
    };
    if(type=="ObjectProperty" || type=="ClassProperty") {
        result["declared_type"]=descriptor(auxiliary.at("declared_type"));
        if(type=="ClassProperty")result["meta_class"]=descriptor(auxiliary.at("meta_type"));
    } else if(type=="EnumProperty" || type=="ByteProperty") {
        nlohmann::json enumeration={{"path",nullptr},{"underlying_type",auxiliary.at("underlying_type")},
                                   {"entries",nlohmann::json::array()}};
        if(auxiliary.contains("enum") && !auxiliary.at("enum").is_null()) {
            const auto& captured=auxiliary.at("enum");
            enumeration["path"]=captured.at("path");
            for(const auto& entry:captured.at("entries"))
                enumeration["entries"].push_back({{"name",entry.at("label")},{"value",entry.at("value")}});
        }
        result["enum"]=std::move(enumeration);
    }
    return result;
}
inline std::string store_function_contract(std::map<std::string,nlohmann::json>& saved,std::uint64_t& sequence,
                                         const nlohmann::json& contract) {
    for(const auto& [token,known]:saved)if(known==contract)return token;
    if(sequence==std::numeric_limits<std::uint64_t>::max())
        throw HandlerError("signature_contract_limit","The token counter is exhausted");
    const auto token="sig-"+std::to_string(++sequence);
    saved.emplace(token,contract);return token;
}
// Include every field in the chain; CPF_Parm can change between reads.
template<class Reader>nlohmann::json capture_function_field_chain(std::uintptr_t head,Reader&& read_field) {
    nlohmann::json fields=nlohmann::json::array();std::set<std::uintptr_t> visited;
    while(head) {
        if(!visited.insert(head).second)
            throw HandlerError("unsupported_signature","The parameter chain contains a cycle");
        auto field=std::invoke(std::forward<Reader>(read_field),head);
        if(!field.is_object() || !field.contains("next") || !field.at("next").is_number_unsigned())
            throw HandlerError("unsupported_signature","The signature field entry is incomplete");
        fields.push_back(std::move(field));head=fields.back().at("next").get<std::uintptr_t>();
    }
    return fields;
}
}
