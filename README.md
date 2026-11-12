# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R MCP implementation. Python/MCP frontend, framed TCP transport, game-Tick dispatch, reflection, typed values, observations, loader and tests remain in this independent code repository. Detailed state and acceptance belong to the central documentation.

Implementation version: **0.3.0**. The bridge supports game-interface research and mod development through **48 `sn2_` tools**: retained reflection/invocation/observations, exact source authoring, CMake/MSVC builds and diagnostics, compiled artifact packaging, game lifecycle and standalone mod initialization/status/disable/restart iteration. Stdio/shared HTTP (`127.0.0.1:27224/mcp`), native TCP 27223 and the Windows x64 UE5.6.1 / CL131439 foundation are retained.

The first compiled project creates a separate native widget-opacity DLL using the copied Unreal runtime SDK. Packages load from the workspace through their verified command ABI. Disable restores the baseline; the DLL and hook remain until game exit, and compiled revision replacement uses controlled restart. General runtime and source tools remain available beyond this template. Actual validation and operator acceptance are recorded centrally.

Detailed documentation belongs to the central repository:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Correction plan and required mod-development result](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).
- [Runtime acceptance and reproducible checks](../docs/bridges/subnautica-2/runtime-acceptance.md).
- [Mod-development correction and example](../docs/bridges/subnautica-2/mod-development-acceptance.md).

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic fixtures and optional modules remain as source provenance; they are absent from the active Subnautica runtime composition. The bridge builds from this repository and its declared dependencies.
