# SN2 MCP Development Services

This Content/Blueprint plugin is the in-game companion of Subnautica 2 MCP. It exposes text conversion, trace result decomposition, and strongly retained temporary UObject prototypes through cooked Blueprint functions. Its `HeldObjects` references are cleared on EndPlay.

Build this component with the acquired compatible Modkit/custom Engine and `tools/build_development_services.py`. Consumers use its verified cooked package with the native bridge; they do not compile the service in the installed game. New persistent Blueprint/package authoring remains a distinct capability and is not supplied by these wrappers.

The [central implementation plan](../../../../../docs/bridges/subnautica-2/implementation-plan.md) owns the functional scope and acceptance.

