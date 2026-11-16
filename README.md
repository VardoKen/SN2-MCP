# Subnautica 2 MCP

Connect an MCP-compatible AI assistant to a running Subnautica 2 game to inspect Unreal objects, research game interfaces, prototype changes, and create Content/Blueprint mods.

**Current version: 0.9.0 · Windows x64 · 77 MCP tools.** The tested game checkpoint is Unreal Engine 5.6.1, changelist 131439, game build 3388. A game update may require new runtime bindings; compatibility with a different build must be checked.

## What it can do

- Find game objects and inspect properties, classes, and callable functions.
- Read and modify supported values, invoke reflected functions, and observe changes.
- Compile supported Blueprint functions and inherited events in the running game, then save and package the result as a mod.
- Manage mod packages and temporary authored objects, with receipts for the operations performed.

The supplied bridge and its in-game companion support runtime mod authoring without Unreal Editor or Modkit. A packaged mod can run its own authored logic after installation without MCP. This was verified through creation, revision, packaging, a fresh game launch, and natural shutdown.

Supported graphs are a defined subset: scalar members, by-value parameters, member access, branches/sequences, supported reflected calls, `self`, and synchronous inherited Blueprint events with parent calls. New Widget UI, latent graphs, arbitrary custom events/delegates, and universal live replacement of packages are outside this version. Replace installed packages while the game is closed. Mods that replace the GameInstance configuration slot do not automatically compose with other replacements.

## Install and use

1. Download **`SN2-MCP-0.9.0-windows-x64.zip`** from [Releases](https://github.com/VardoKen/SN2-MCP/releases). GitHub's automatic **Source code** archives do not contain the ready-to-use bridge or cooked companion.
2. Follow [Manual installation](INSTALL.md). You need Windows x64, the compatible game, Python 3.11 or newer, `uv`, and the Microsoft Visual C++ x64 Runtime. A compiler and Unreal Editor are not needed for this route.
3. Connect your MCP client using the command and arguments in that guide, or the optional local HTTP endpoint. The guide also explains how to load the bridge for each game session.

An AI agent can follow [Agent installation and source build instructions](AGENTS.md). Those instructions include building the Python frontend, native DLL/loader, and cooked companion from source. The agent configures its actual MCP application using the supplied command and arguments.

## How the pieces fit together

| Component | Purpose |
| --- | --- |
| Python MCP frontend | Publishes the tools to your MCP client. |
| `SN2_MCP.dll` and loader | Connect to the selected game process and execute supported work on its Tick. |
| `SN2MCPDevelopmentServices` | Cooked in-game companion providing text conversion, trace helpers, and retained temporary objects. It is part of the bridge. |
| Your mod workspace | Stores the mods you author, specifications, packages, and operation receipts separately from the game. |

The default native connection is local TCP port **27223**. Optional MCP HTTP runs at **`http://127.0.0.1:27224/mcp`**. A working MCP connection alone does not mean the native executor or companion is ready; the installation guide checks each component.

## Source repository

`src/sn2_mcp/` owns the frontend; `native/` owns the native bridge; `development/` contains the companion, authoring helper, templates and examples; `tools/` and `tests/` contain build and verification tooling. Generated builds, environments and local artifacts are excluded from Git. The release ZIP is a separate runtime distribution.

The implementation began from the G1R source base at `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into the independent Subnautica baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical donor modules and tests remain identifiable in the source tree. BLAKE3/CityHash source attribution and dependency notices are retained; this repository does not declare a separate project-wide license.
