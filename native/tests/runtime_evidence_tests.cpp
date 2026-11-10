#include "sn2_mcp/runtime_evidence.hpp"
#include <iostream>
#include <stdexcept>
int main() {
    try {
        sn2_mcp::RuntimeEvidence evidence;
        const nlohmann::json shape={{"type","StrProperty"},{"direction","input"},{"element_size",16}};
        auto require=[](bool value,const char* text){if(!value)throw std::runtime_error(text);};
        evidence.observe("invoke",shape,1,true,false);
        evidence.observe("read_array",{{"element_type","NameProperty"}},2,false,true,false,false);
        evidence.observe("read_struct",{{"type","StructProperty"}},3,false,true,true);
        require(evidence.describe().at("shapes").empty(),"failed cleanup, empty or partial value became accepted");
        evidence.observe("invoke",shape,4,true,true);
        evidence.observe("invoke",shape,8,true,true);
        auto result=evidence.describe();
        require(result.at("shapes").size()==1,"duplicate shape stored per call");
        const auto item=result.at("shapes").at(0);
        require(item.at("successful_count")==2 && item.at("first_tick")==4 && item.at("last_tick")==8,"aggregate counts/ticks wrong");
        require(item.at("state")=="observed_success" && item.at("cleanup_confirmed")==true,"observed owned evidence lost");
        require(result.at("coverage")=="listed_shapes_and_directions_only","universal type claim");
        evidence.observe("invoke",{{"type","StrProperty"},{"direction","return"}},10,true,true);
        require(evidence.describe().at("shapes").size()==2,"different direction collapsed");
        require(!item.contains("value") && !item.contains("arguments"),"game values retained");
        std::cout<<"Runtime evidence aggregation checks passed\n";return 0;
    }catch(const std::exception& error){std::cerr<<error.what()<<'\n';return 1;}
}
