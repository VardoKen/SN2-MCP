# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R MCP implementation. Python/MCP frontend, framed TCP transport, game-Tick dispatch, reflection, typed values, observations, loader and tests remain in this independent code repository. Detailed state and acceptance belong to the central documentation.

Implementation version: **0.1.0**. The runtime exposes 22 `sn2_` tools through stdio or shared HTTP (`127.0.0.1:27224/mcp`), with the native channel at `127.0.0.1:27223`. It targets Windows x64 Subnautica 2; the current acceptance target is UE 5.6.1 / CL131439. Consult the acceptance document for tested shapes and remaining coverage before using a reflected call.

Detailed documentation belongs to the central repository:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Adaptation plan](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).
- [Runtime acceptance and reproducible checks](../docs/bridges/subnautica-2/runtime-acceptance.md).

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic fixtures and optional modules remain as source provenance; they are absent from the active Subnautica runtime composition. The bridge builds from this repository and its declared dependencies.
