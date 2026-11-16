# Widget opacity native modification

This editable C++ project changes the live start-screen Continue label's render opacity. The project compiles the existing Subnautica Tick, reflection, typed invocation and handle-validation algorithms into its own DLL. It creates no TCP server and has no dependency on `SN2_MCP.dll`.

Edit `Opacity` in `src/mod.cpp`. Build with Windows x64 CMake/MSVC, `SN2_MCP_SDK_ROOT` pointing to the recorded bridge SDK source inputs and `SN2_MOD_SOURCE_REVISION` supplied by the build input manifest. Package the compiled DLL and its build receipt through the bridge's mod-project workflow.

The checked loader loads the DLL from its workspace package and invokes `SN2ModCommand` ABI 1. `START` initializes its own worker and Tick/reflection bindings, selects the live Continue label, records its original opacity, applies the compiled value and verifies the getter. Loading a module alone does not establish active modification state. `STATUS` reports initialization, target selection, effect and command receipts. `DISABLE` stops applying the modification and attempts to restore the recorded value on the same valid object.

While enabled, the modification maintains its compiled opacity. A successful fresh getter showing a different value admits one new setter application and another getter, with maintenance evidence in status. An unknown setter outcome stops further applications; it is never automatically replayed. Game animation can briefly produce a `maintaining` state until the requested value is independently observed.

The DLL and its Tick trampoline remain resident until the selected game process exits. Disable does not unload code or remove the hook. A newly built revision uses controlled game restart and a fresh process identity; arbitrary DLL hot unload/reload is unsupported. A missing or destroyed target is reported explicitly and cannot be claimed as successfully restored.

This is a historical native-DLL template, separate from the current Content/Blueprint runtime authoring route. See the repository [overview](../../../README.md) and [agent instructions](../../../AGENTS.md) for the supported bridge and build workflow.
