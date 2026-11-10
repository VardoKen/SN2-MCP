# Subnautica 2 MCP

Independent code repository for the Subnautica 2 native runtime MCP bridge in Unreal Game Runtime MCP.

Preparation is complete: current game evidence, G1R implementation/reuse analysis, proposed runtime tool contract, and concrete technical package with sequential acceptance gates are documented centrally. Implementation has not started and requires a separate operator assignment. New bridge implementation begins at `0.1.0`; preparation has no product version.

Detailed documentation and work tracking belong to the central repository:

- [Bridge preparation and state](../docs/bridges/subnautica-2/README.md).
- [Future implementation proposal](../docs/bridges/subnautica-2/implementation-plan.md).
- [Project rules](../AGENTS.md).
- [Development workflow](../docs/development.md).

The intended product is a dedicated native Subnautica 2 bridge using suitable G1R mechanisms as references. Common Unreal functionality may be reused/extracted within assigned development; discovery, UE5.6.1 ABI, loading, Tick/lifecycle and gameplay contracts need target validation. The first future step is the proposal's I0 target-binding checkpoint. No Gothic bridge copy, code scaffolding or installed-game change was made during preparation.
