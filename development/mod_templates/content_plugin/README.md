# @PLUGIN_ID@ content and Blueprint project

This directory is an editable, content-only Unreal plugin. Its `.uplugin`
enables plugin content and declares no native modules. `mod.json` records the
bridge's plugin identity and optional generated-class entry. The initial
`Content/` directory contains no authored assets: project creation is source
preparation, not Blueprint creation, compilation or a working mod.

For native runtime authoring, write a schema-1 JSON specification in this
project's `Authoring/` directory using the bridge's ordinary source tools.
Inspect `sn2_runtime_authoring_recipe`, then compile the selected Blueprint with
`sn2_runtime_compile_blueprint` and inspect its status/report. A compiled live
revision is not yet saved: use `sn2_runtime_save_package`, inspect
`sn2_runtime_build_status`, then package its genuine outputs with
`sn2_package_runtime_artifact` and verify the package. This path uses the supplied
native bridge, cooked development companion and running game without a consumer
Engine, Modkit, Editor/cooker or source checkout. The current backend contract
determines supported graph, event, type and persistence shapes; the broader
external Editor schema does not imply runtime support.

The separate external path uses the compatible custom Unreal Editor and Modkit to author and
save mod-owned assets under this plugin's `Content/` directory. Retain the
Modkit's game stubs and references in the configured authoring environment;
they are neither original game source nor payload to copy into this plugin.
For MCP authoring, save a schema-1 JSON specification in this project's
`Authoring/` directory. `sn2_authoring_recipe(project_path, spec_path)` identifies
the selected project, exact specification, required tools and helper build.
`sn2_author_mod` creates editable data assets and Blueprint graphs in an isolated
Modkit capture, compiles them and publishes saved assets only after complete
success and source-revision validation. Inspect `sn2_authoring_status` and
`sn2_authoring_report` for full output and asset/node/pin diagnostics.
The Editor-only C++ helper belongs to the build environment; it is not a native
module of this content plugin. `development/authoring/SN2MCPAuthoring` owns its
source and authoring specification contract in the bridge repository.

For the externally authored entry-Actor path, create an Actor Blueprint when the mod requires executable logic, author its
behavior, compile it and save its `.uasset`. New game content must use the
target's actual registration mechanism; mounting an archive alone does not
register an item, recipe or buildable.

Set `entry_class` in `mod.json` only to the exact generated-class reference for
the authored actor, for example
`/@PLUGIN_ID@/Blueprints/BP_ModEntry.BP_ModEntry_C`. The corresponding editable
file is `Content/Blueprints/BP_ModEntry.uasset`. The bridge validates this source
path and inventories every authored input, but the path itself does not prove
successful compilation, loading or execution. Leave `entry_class` null for a
content plugin that has no actor activation entry.

For externally authored assets, use the bridge's content cook workflow to capture immutable inputs and run the
compatible Modkit/custom-engine cooker for Win64 Shipping against the selected
game base. Inspect full reports and the cook receipt before packaging. A
successful selected IoStore build provides a plugin descriptor and complete
`.pak/.utoc/.ucas` container sets. Source-only exports and prebuilt example
copies are not successful cooking of this project.

Package and verify the selected runtime-build or external-cook output separately
from installation. Plugin discovery, class loading, entry activation and
independently observed authored effects are distinct. A declared Actor entry
is a bridge activation contract; it does not by itself establish autonomous
startup. A mod intended to run without MCP must use the candidate's verified
natural startup/lifecycle contract and carry its declared runtime dependencies.
Do not infer automatic execution merely from mounted containers.
Then install the selected revision through the bridge, start a fresh game
process, inspect discovery/mount state, and independently observe target
content and Blueprint effects. Inspect mod objects and watches while debugging.
For revision changes, save new inputs and repeat runtime compilation/save or
external authoring/cook, verification,
controlled game restart and activation. Disable authored behavior, perform
natural actor teardown where applicable, confirm process exit and remove only
the selected test plugin at the established lifecycle boundary.
