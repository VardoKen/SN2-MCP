"""Run the applicable neutral Subnautica frontend regression suite.

The copied Gothic package/debugger/live-probe tests remain in the repository as
provenance. This runner exercises the registered runtime surface and its inherited
transport, values, observations and lifecycle without starting or loading a game.
"""

from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

MODULES = (
    "test_native_client", "test_snapshots", "test_server_lifespan",
    "test_subnautica_catalog", "test_subnautica_runtime", "test_subnautica_http",
    "test_mcp_effects", "test_mcp_function_contracts", "test_mcp_function_query",
    "test_mcp_objects", "test_mcp_observations", "test_mcp_native",
    "test_mcp_pod_reads", "test_mcp_string_inputs", "test_mcp_transport",
    "test_mcp_vectors", "test_mcp_const_vectors", "test_mcp_transforms",
    "test_mcp_const_transforms", "test_mcp_linear_colors", "test_mcp_name_inputs",
    "test_mcp_object_array_inputs", "test_mcp_scalar_outputs", "test_mcp_enums",
    "test_mcp_wide_integers", "test_mcp_classes",
    "test_source_workspace",
    "test_build_workflow", "test_cpp_diagnostics", "test_runtime_lifecycle", "test_command_executor",
    "test_mod_runtime", "test_mod_projects", "test_mcp_build_lifecycle", "test_mcp_mod_workflow",
    "test_compiled_mod_acceptance",
)

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromNames(["tests." + name for name in MODULES])
    outcome = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if outcome.wasSuccessful() else 1)
