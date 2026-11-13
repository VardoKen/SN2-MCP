# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R MCP implementation. Python/MCP frontend, framed TCP transport, game-Tick dispatch, reflection, typed values, observations, loader and tests remain in this independent code repository. Detailed state and acceptance belong to the central documentation.

Current implementation version: **0.4.1**, with **61 `sn2_` registrations**. Content/Blueprint project validation, cook/report/package operations, stopped-game deployment and native asset/class/actor lifecycle complement retained reflection/invocation/observations, source authoring and bridge builds. Stdio/shared HTTP (`127.0.0.1:27224/mcp`), native TCP 27223 and the Windows x64 UE5.6.1 / CL131439 foundation are retained.

The operator selected editable Content/Blueprint plugins with authoring on **D:**. Focused native asset/class/actor checks passed, including activation without duplication, reconnect and independently observed destruction. Actual authored cooking, new content and Blueprint revision effects remain incomplete because the compatible custom engine is absent at `D:/SN2Modding/Engine`; no real content package has been installed. The rejected `0.3.0` widget-DLL experiment remains historical evidence. The native bridge DLL is infrastructure, while the selected mod artifacts are content plugins and cooked containers.

Detailed documentation belongs to the central repository:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Correction plan and required mod-development result](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).
- [Current Content/Blueprint checks and remaining acceptance](../docs/bridges/subnautica-2/content-blueprint-acceptance.md).
- [Runtime acceptance and reproducible checks](../docs/bridges/subnautica-2/runtime-acceptance.md).
- [Historical source/prototype evidence](../docs/bridges/subnautica-2/mod-development-acceptance.md).

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic fixtures and optional modules remain as source provenance; they are absent from the active Subnautica runtime composition. The bridge builds from this repository and its declared dependencies.
