# Subnautica 2 MCP

Independent code repository containing the existing G1R MCP code as the working base for future Subnautica 2 adaptation. The Python frontend, native implementation, tests and Gothic fixtures, tools, helper package, build metadata and dependency lock were copied from G1R MCP. The destination retains its own Git history.

The code currently retains G1R identifiers, target paths, Gothic-specific integration and historical Python `1.0.0-dev.116` / native `1.0.0-dev.115` metadata. It has not yet been adapted or accepted for Subnautica 2. The first assigned adaptation will establish the Subnautica implementation version `0.1.0`.

Detailed documentation belongs to the central repository:

- [Bridge state and copied baseline](../docs/bridges/subnautica-2/README.md).
- [Adaptation plan](../docs/bridges/subnautica-2/implementation-plan.md).
- [Source analysis and reuse map](../docs/bridges/subnautica-2/g1r-reuse-map.md).
- [Game evidence](../docs/bridges/subnautica-2/baseline-and-evidence.md).

Future development adapts this copy in this repository. No separate shared-code repository/package or G1R refactoring is part of the selected approach. Game launch, loading, installation and runtime adaptation have not been performed by preparation.
