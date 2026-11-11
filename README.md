# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R MCP implementation. Python/MCP frontend, framed TCP transport, game-Tick dispatch, reflection, typed values, observations, loader and tests remain in this independent code repository. Detailed state and acceptance belong to the central documentation.

Implementation version: **0.2.2**. The bridge supports game-interface research and mod development/prototyping through **33 `sn2_` tools**: the retained 22 runtime tools plus workspace source identity/search/exact editing, source/runtime recipes and verified source export. Stdio/shared HTTP (`127.0.0.1:27224/mcp`), native TCP 27223 and the existing Windows x64 UE5.6.1 / CL131439 implementation are retained.

The correction includes a reproducible UI modification prototype: two source revisions change a live start-screen label's opacity, are independently checked through getter/snapshots/watches, and restore the original value. This is a development client and source archive, not an installable mod format. The operator rejected the earlier `0.1.0` overall result; operator acceptance of this correction has not been recorded.

Detailed documentation belongs to the central repository:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Correction plan and required mod-development result](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).
- [Runtime acceptance and reproducible checks](../docs/bridges/subnautica-2/runtime-acceptance.md).
- [Mod-development correction and example](../docs/bridges/subnautica-2/mod-development-acceptance.md).

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic fixtures and optional modules remain as source provenance; they are absent from the active Subnautica runtime composition. The bridge builds from this repository and its declared dependencies.
