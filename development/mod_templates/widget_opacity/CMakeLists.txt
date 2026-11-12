cmake_minimum_required(VERSION 3.24)
project(@MOD_ID@ LANGUAGES C CXX)

if(NOT SN2_MCP_SDK_ROOT)
    message(FATAL_ERROR "Set SN2_MCP_SDK_ROOT to the recorded Subnautica 2 MCP SDK source inputs")
endif()
if(NOT SN2_MOD_SOURCE_REVISION)
    message(FATAL_ERROR "Set SN2_MOD_SOURCE_REVISION to the build input receipt revision")
endif()
set(SN2_MCP_MOD_SDK_ONLY ON CACHE BOOL "Build the standalone mod SDK only" FORCE)
add_subdirectory("${SN2_MCP_SDK_ROOT}/native" sn2-sdk)

add_library(sn2_mod SHARED src/mod.cpp)
set_target_properties(sn2_mod PROPERTIES OUTPUT_NAME "@MOD_ID@")
target_link_libraries(sn2_mod PRIVATE sn2_mod_runtime)
target_compile_definitions(sn2_mod PRIVATE
    SN2_MOD_ID="@MOD_ID@"
    SN2_MOD_SOURCE_REVISION="${SN2_MOD_SOURCE_REVISION}")
target_compile_options(sn2_mod PRIVATE /utf-8 /W4 /EHsc)
