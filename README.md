# Subnautica 2 MCP

Native runtime MCP bridge adapted from the copied G1R implementation. This independent repository owns the Python/MCP frontend, TCP transport, game-Tick dispatch, reflection, typed values, observations, loader, runtime Blueprint compiler, package writers and declared dependencies. Detailed documentation belongs to the central repository.

Current implementation **0.9.0** registers **77 `sn2_` tools**. The supplied frontend/native bridge and cooked `SN2MCPDevelopmentServices` component support workspace Blueprint specifications, synchronous named functions and exact inherited event overrides in Shipping, node/pin diagnostics, genuine Unreal package/container output and source-correlated Content plugins. Captured `Config/Game.ini` and `Config/Engine.ini` are preserved. Runtime authoring uses the supplied bridge and game without a consumer Engine, Modkit, Editor/cooker or bridge source checkout. The installed-wheel A/error/B/save/package/fresh-load cycle at **0.7.0** remains historical acceptance of the earlier synchronous-function subset; final-candidate autonomous startup and lifecycle evidence are recorded separately in the central documentation.

The compiler supports scalar members/defaults, by-value parameters, member get/set, branch/sequence, `self`, supported reflected callable functions and exact `call_parent` within a synchronous inherited override. Final/static/network/delegate/editor-only/latent events, reference/output event signatures, new Widget UI, generated enum/FText properties, struct/nonempty-array literals and persistent authored array/struct defaults remain unsupported. `DevelopmentOnly`, `BlueprintInternalUseOnly` and WorldContext metadata are not inspected; compile, direct invocation and save have distinct type/direction coverage. Compilation or mounting alone does not establish autonomous startup. Restart remains the package-replacement boundary; universal live package replacement is not established. The separate compatible Editor/cook path remains available for broader assets.

`sn2_runtime_release_blueprint` retires an in-memory authored revision using its recorded identity and root ownership. It preserves saved packages, compile evidence and other working revisions. Unknown outcomes require read-only reconciliation before further release/save actions. Retirement is distinct from cooked-package unload and does not replace natural game lifecycle.

The companion supplies text conversion, trace-result decomposition and strongly retained temporary UObject instances. It is part of the bridge toolset; a newly authored user mod is a separate Content/Blueprint package. Native DLLs provide bridge infrastructure.

The [mod settings menu example](development/examples/mod_settings_menu/README.md) uses the external Editor/cook environment to author **SN2ModSettingsTest 0.1.0**. Its Content-only plugin passed standalone menu button → empty page → Back → reopen without bridge/MCP. This example does not add runtime Widget UI authoring. The [central result](../docs/bridges/subnautica-2/mod-settings-test.md) owns its retained operator installation and process observations.

Entry points:

- [Bridge state and runtime entry](../docs/bridges/subnautica-2/README.md).
- [Tool catalog and contracts](../docs/bridges/subnautica-2/proposed-tool-contract.md).
- [Runtime authoring inputs, compiler and package receipts](../docs/bridges/subnautica-2/authoring-contract.md).
- [A/error/B/fresh-process acceptance](../docs/bridges/subnautica-2/content-blueprint-acceptance.md).
- [Mod settings test](../docs/bridges/subnautica-2/mod-settings-test.md).
- [Implementation plan and completion criteria](../docs/bridges/subnautica-2/implementation-plan.md).
- [Companion API and delivery](../docs/bridges/subnautica-2/runtime-services.md).
- [Target mechanisms and source provenance](../docs/bridges/subnautica-2/mod-mechanisms.md).
- [G1R reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Build environment](../docs/bridges/subnautica-2/authoring-environment.md).

Windows x64 target checkpoint: UE **5.6.1**, CL **131439**, build **3388**. Stdio and HTTP `127.0.0.1:27224/mcp` use native TCP **27223**. Transport readiness is separate from target/executor readiness.

Source provenance: G1R `21274888730c38b5b21e27e6f42137f3963a1a6b`, copied into destination baseline `77a6eb6e184433be27acceccd94d8b4de8b9ffee`. Historical Gothic modules and fixtures remain identifiable source provenance outside active Subnautica composition. This repository builds from its own source and declared dependencies. No public release is claimed.
