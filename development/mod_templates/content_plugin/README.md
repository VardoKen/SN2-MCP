# @PLUGIN_ID@ content and Blueprint project

This directory is an editable, content-only Unreal plugin. Its `.uplugin`
enables plugin content and declares no native modules. `mod.json` records the
bridge's plugin identity and optional generated-class entry. The initial
`Content/` directory contains no authored assets: project creation is source
preparation, not Blueprint creation, compilation or a working mod.

Use the compatible Subnautica 2 custom Unreal Editor and Modkit to author and
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

Create an Actor Blueprint when the mod requires executable logic, author its
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

Use the bridge's content cook workflow to capture immutable inputs and run the
compatible Modkit/custom-engine cooker for Win64 Shipping against the selected
game base. Inspect full reports and the cook receipt before packaging. A
successful selected IoStore build provides a plugin descriptor and complete
`.pak/.utoc/.ucas` container sets. Source-only exports and prebuilt example
copies are not successful cooking of this project.

Package and verify the identified cook output separately from installation.
Then install the selected revision through the bridge, start a fresh game
process, inspect discovery/mount state, and independently observe target
content and Blueprint effects. Inspect mod objects and watches while debugging.
For revision changes, save new authoring inputs and repeat cook, verification,
controlled game restart and activation. Disable authored behavior, perform
natural actor teardown where applicable, confirm process exit and remove only
the selected test plugin at the established lifecycle boundary.
