# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R MCP implementation. Python/MCP frontend, framed TCP transport, game-Tick dispatch, reflection, typed values, observations, loader and tests remain in this independent code repository. Detailed state and acceptance belong to the central documentation.

Implementation version: **0.5.1**, with **65 `sn2_` registrations**, including four editable-asset authoring tools. Content/Blueprint authoring, cook/report/package operations, stopped-game deployment and native asset/class/actor lifecycle complement retained reflection/invocation/observations, source tools and bridge builds. Stdio/shared HTTP (`127.0.0.1:27224/mcp`), native TCP 27223 and the Windows x64 UE5.6.1 / CL131439 foundation are retained.

The selected editable Content/Blueprint workflow passed real revision A → attributed authoring failure → repaired revision B, with exact source/cook/package identities, installation and restart replacement, target recipe registration, natural Lobby BeginPlay/EndPlay effects, independent restoration and test-plugin removal. A/B authoring and cooking used `0.5.0`; the final B runtime check used `0.5.1`. A genuine Fabricator service classified both the mod recipe and a stock recipe with verified ownership. The selected game and temporary actors/components were closed or removed. The Editor-only helper remains outside Shipping packages; mods are content plugins and cooked containers.

The checked result supports mod authoring and runtime iteration in the documented schema and target. Full crafting consumption/output and interactive crafting UI were not exercised. Cooked class/package replacement uses confirmed restart; universal live asset unload, every Blueprint node family and source-level debugging are not claimed. Detailed evidence preserves these limits and the separate historical experiments.

Detailed documentation belongs to the central repository:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Correction plan and required mod-development result](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).
- [Authoring environment and applicable dependencies](../docs/bridges/subnautica-2/authoring-environment.md).
- [Editable asset authoring contract and initial verified scope](../docs/bridges/subnautica-2/authoring-contract.md).
- [Current Content/Blueprint checks and remaining acceptance](../docs/bridges/subnautica-2/content-blueprint-acceptance.md).
- [Runtime acceptance and reproducible checks](../docs/bridges/subnautica-2/runtime-acceptance.md).
- [Historical source/prototype evidence](../docs/bridges/subnautica-2/mod-development-acceptance.md).

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic fixtures and optional modules remain as source provenance; they are absent from the active Subnautica runtime composition. The bridge builds from this repository and its declared dependencies.
