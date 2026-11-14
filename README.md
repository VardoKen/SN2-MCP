# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R implementation. This independent repository owns the Python/MCP frontend, TCP transport, game-Tick dispatch, reflection, typed values, observations, loader, runtime Blueprint compiler, package writers and declared dependencies. Detailed documentation belongs to the central repository.

Verified implementation **0.7.0** registers **76 `sn2_` tools**. The supplied frontend/native bridge and cooked `SN2MCPDevelopmentServices` component can create and edit a user Blueprint specification, compile synchronous named function graphs in Shipping, diagnose exact node/pin errors, save genuine Unreal packages, publish `.pak/.utoc/.ucas` Content plugins, and load and execute a saved revision in a fresh game. The installed-wheel A/error/B/fresh-load acceptance ran without an external Editor/cooker or bridge source checkout.

The checked compiler subset includes scalar members/defaults, parameters, member get/set, branch/sequence and supported reflected calls/conversions. Runtime EventGraph compilation, generated enum/FText properties, struct and nonempty array literals and persistent authored array/struct member defaults remain unsupported with explicit diagnostics. Restart iteration is verified; universal live package replacement is not established. The compatible external authoring/cook tools remain available for broader editable assets.

The companion supplies text conversion, trace-result decomposition and strongly retained temporary UObject instances. It is part of the bridge toolset; a newly authored user mod is a separate Content/Blueprint package. Native DLLs provide bridge infrastructure.

Entry points:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Tool catalog and contracts](../docs/bridges/subnautica-2/proposed-tool-contract.md).
- [Runtime authoring inputs, compiler and package receipts](../docs/bridges/subnautica-2/authoring-contract.md).
- [Actual A/error/B/fresh-process acceptance](../docs/bridges/subnautica-2/content-blueprint-acceptance.md).
- [Implementation plan and completion criteria](../docs/bridges/subnautica-2/implementation-plan.md).
- [Companion API and delivery](../docs/bridges/subnautica-2/runtime-services.md).
- [Target mechanisms and source provenance](../docs/bridges/subnautica-2/mod-mechanisms.md).
- [G1R reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Build environment](../docs/bridges/subnautica-2/authoring-environment.md).

Windows x64 target checkpoint: UE **5.6.1**, CL **131439**, build **3388**. Stdio and HTTP `127.0.0.1:27224/mcp` use native TCP **27223**. Transport readiness is separate from target/executor readiness.

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic modules and fixtures remain identifiable source provenance, outside active Subnautica composition. This repository builds from its own source and declared dependencies. No public release is claimed.
