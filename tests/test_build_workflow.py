"""Neutral build evidence and Windows worker-lifetime checks, without a game."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from sn2_mcp.build_workflow import BuildError, BuildWorkflow, _guard_input_files, run_build_command


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pe_bytes():
    raw = bytearray(96)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 60, 64)
    raw[64:68] = b"PE\0\0"
    struct.pack_into("<H", raw, 68, 0x8664)
    struct.pack_into("<H", raw, 86, 0x2000)
    return bytes(raw)


class FixtureRunner:
    def __init__(self, root, failure=False, mutate=False):
        self.root, self.failure, self.mutate = root, failure, mutate
        self.calls = []

    def __call__(self, arguments, cwd, timeout, stdout, stderr):
        self.calls.append((arguments, timeout))
        output = ""
        state, code = "succeeded", 0
        if "-S" in arguments:
            self.source = Path(arguments[arguments.index("-S") + 1]).parent
            self.output = Path(arguments[arguments.index("-B") + 1])
            reply = self.output / ".cmake/api/v1/reply"
            reply.mkdir(parents=True, exist_ok=True)
            (reply / "index-1.json").write_text(json.dumps({"reply": {"codemodel-v2": {"jsonFile": "model.json"}}}))
            (reply / "model.json").write_text(json.dumps({"configurations": [{"name": "Release", "targets": [{"name": "sn2_mcp_native", "jsonFile": "target.json"}]}]}))
            (reply / "target.json").write_text(json.dumps({"artifacts": [{"path": "Release/SN2_MCP.dll"}]}))
        elif "--build" in arguments:
            if self.failure:
                output = str(self.source / "native/src/example.cpp") + "(7,3): error C2065: 'missing': undeclared identifier\n"
                state, code = "failed", 1
            else:
                artifacts = self.output / "Release"
                artifacts.mkdir(exist_ok=True)
                (artifacts / "SN2_MCP.dll").write_bytes(pe_bytes())
            if self.mutate:
                (self.root / "native/src/example.cpp").write_text("int changed = 2;\n")
        stdout.write_bytes(output.encode("utf-8"))
        stderr.write_bytes(b"")
        return {"state": state, "exit_code": code, "arguments": arguments,
                "tree_exit_confirmed": True, "output_complete": True,
                "stdout": {"path": str(stdout), "bytes": stdout.stat().st_size, "sha256": sha(stdout.read_bytes())},
                "stderr": {"path": str(stderr), "bytes": 0, "sha256": sha(b"")}}


class BuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "native/src").mkdir(parents=True)
        (self.root / "native/CMakeLists.txt").write_text(
            "cmake_minimum_required(VERSION 3.24)\nproject(Fixture)\n"
            "add_library(sn2_mcp_native SHARED src/example.cpp)\n"
            "add_executable(native_tests src/example.cpp)\n"
            "add_test(NAME fixture COMMAND native_tests)\n")
        (self.root / "native/src/example.cpp").write_text("int example = 1;\n")

    def test_build_preserves_exact_inputs_artifact_and_complete_reports(self):
        runner = FixtureRunner(self.root)
        workflow = BuildWorkflow(self.root, runner=runner)
        self.assertEqual(workflow.status()["state"], "not_built")
        receipt = workflow.build(target="sn2_mcp_native", timeout_seconds=10000)
        self.assertEqual(receipt["state"], "succeeded")
        self.assertTrue(receipt["exact_source_compiled"])
        self.assertTrue(receipt["workspace_current_revision_compiled"])
        artifact = receipt["artifacts"][0]
        self.assertEqual(artifact["sha256"], sha(pe_bytes()))
        self.assertEqual(artifact["pe"]["architecture"], "x64")
        self.assertEqual((self.root / "build/native/Release/SN2_MCP.dll").read_bytes(), pe_bytes())
        self.assertEqual(workflow.status(receipt["build_id"])["state"], "succeeded")
        self.assertFalse(workflow.status()["resident_worker"])
        self.assertTrue(workflow.read_report(receipt["build_id"], "build")["receipt_matches"])
        build_arguments = next(arguments for arguments, _ in runner.calls if "--build" in arguments)
        self.assertIn("native_tests", build_arguments)
        self.assertIn("/nodeReuse:false", build_arguments)
        self.assertGreater(runner.calls[0][1], 9000)

    def test_failure_retains_diagnostic_source_correlation_and_no_publication(self):
        workflow = BuildWorkflow(self.root, runner=FixtureRunner(self.root, failure=True))
        receipt = workflow.build()
        self.assertEqual(receipt["state"], "failed")
        self.assertFalse(receipt["compiled"])
        self.assertFalse(receipt["exact_source_compiled"])
        self.assertEqual(receipt["publication"], [])
        self.assertEqual(receipt["stages"]["test"]["state"], "not_started")
        diagnostic = receipt["compiler_reports"][2]["diagnostics"][0]
        self.assertTrue(diagnostic["source_matched"])
        self.assertEqual(diagnostic["source_sha256"], sha((self.root / "native/src/example.cpp").read_bytes()))
        reports = workflow.diagnostics(receipt["build_id"])["reports"]
        self.assertTrue(all(report["raw_report_receipt_matches"] for report in reports))

    def test_workspace_change_during_build_does_not_claim_current_revision(self):
        receipt = BuildWorkflow(self.root, runner=FixtureRunner(self.root, mutate=True)).build()
        self.assertEqual(receipt["state"], "succeeded")
        self.assertTrue(receipt["exact_source_compiled"])
        self.assertTrue(receipt["staged_source_stable"])
        self.assertFalse(receipt["workspace_source_stable"])
        self.assertFalse(receipt["workspace_current_revision_compiled"])
        staged = Path(receipt["staged_source_root"]) / "native/src/example.cpp"
        self.assertEqual(staged.read_text(), "int example = 1;\n")

    def test_target_and_deadline_validation_do_not_start_build(self):
        runner = FixtureRunner(self.root)
        workflow = BuildWorkflow(self.root, runner=runner)
        for timeout in (0, -1, True, float("inf")):
            with self.assertRaises(BuildError):
                workflow.build(timeout_seconds=timeout)
        with self.assertRaises(BuildError):
            workflow.build(target="missing")
        self.assertEqual(runner.calls, [])

    def test_modified_report_is_detected_on_read(self):
        workflow = BuildWorkflow(self.root, runner=FixtureRunner(self.root))
        receipt = workflow.build()
        Path(receipt["stages"]["build"]["stdout"]["path"]).write_bytes(b"changed")
        self.assertFalse(workflow.read_report(receipt["build_id"], "build")["receipt_matches"])

    def test_additional_staged_input_prevents_reusing_claimed_manifest(self):
        runner = FixtureRunner(self.root)
        workflow = BuildWorkflow(self.root, runner=runner)
        first = workflow.build()
        (Path(first["staged_source_root"]) / "unexpected.cpp").write_bytes(b"extra")
        count = len(runner.calls)
        second = workflow.build()
        self.assertEqual(second["state"], "staging_changed")
        self.assertFalse(second["exact_source_compiled"])
        self.assertEqual(len(runner.calls), count)

    def test_mod_project_in_separate_workspace_stages_sdk_and_exact_project(self):
        from sn2_mcp.mod_projects import ModProjects
        from sn2_mcp.workspace import SourceWorkspace
        repository = Path(__file__).resolve().parents[1]
        template = self.root / "development/mod_templates/widget_opacity"
        shutil.copytree(repository / "development/mod_templates/widget_opacity", template)
        workspace = self.root / "authoring"
        workspace.mkdir()
        ModProjects(SourceWorkspace(workspace), self.root).create("mods/Opacity", "Opacity")
        workflow = BuildWorkflow(self.root, workspace_root=workspace)
        recipe = workflow.recipe(project_path="mods/Opacity", target="sn2_mod", run_tests=False)
        self.assertEqual(recipe["project_path"], "mods/Opacity")
        self.assertEqual(recipe["staged_project_path"], "project")
        inputs = {item["path"]: item for item in recipe["inputs"]}
        self.assertEqual(inputs["project/src/mod.cpp"]["origin"], "workspace")
        self.assertEqual(inputs["native/src/example.cpp"]["origin"], "repository")
        source, output = workflow._stage(recipe, "a" * 32)
        self.assertEqual((source / "project/src/mod.cpp").read_bytes(), (workspace / "mods/Opacity/src/mod.cpp").read_bytes())
        self.assertEqual((source / "native/src/example.cpp").read_bytes(), (self.root / "native/src/example.cpp").read_bytes())

    def test_dependency_cache_requires_declared_origin_commit_and_clean_inputs(self):
        dependency = self.root / "build/native/_deps/fixture-src"
        dependency.mkdir(parents=True)
        def git(*arguments):
            result = subprocess.run(["git", "-C", str(dependency), *arguments], capture_output=True, check=True)
            return result.stdout.decode("utf-8").strip()
        git("init")
        (dependency / "CMakeLists.txt").write_bytes(b"add_library(fixture INTERFACE)\n")
        git("add", "CMakeLists.txt")
        git("-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-m", "Fixture input")
        git("tag", "v1")
        git("remote", "add", "origin", "https://example.invalid/fixture.git")
        declared = {"name": "fixture", "repository": "https://example.invalid/fixture.git", "declared_revision": "v1"}
        workflow = BuildWorkflow(self.root)
        reused = workflow._cached_dependencies({"dependencies": [declared]})
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0]["resolved_revision"], git("rev-parse", "HEAD"))
        self.assertEqual((Path(reused[0]["path"]) / "CMakeLists.txt").read_bytes(), b"add_library(fixture INTERFACE)\n")
        self.assertEqual(reused[0]["cmake_variable"], "FETCHCONTENT_SOURCE_DIR_FIXTURE")
        (dependency / "CMakeLists.txt").write_bytes(b"changed\n")
        self.assertIsNone(workflow._dependency_checkout(dependency, declared))
        # The retained exact cache remains usable even if the source checkout later changes.
        again = workflow._cached_dependencies({"dependencies": [declared]})
        self.assertEqual(again[0]["method"], "verified_existing_dependency_cache")
        (Path(again[0]["path"]) / "CMakeLists.txt").write_bytes(b"cache changed\n")
        with self.assertRaises(BuildError):
            workflow._cached_dependencies({"dependencies": [declared]})

    @unittest.skipUnless(os.name == "nt", "Windows input lifetime acceptance")
    def test_input_guard_allows_reads_but_prevents_write_and_rename(self):
        path = self.root / "native/src/example.cpp"
        before = path.read_bytes()
        with _guard_input_files([path]):
            self.assertEqual(path.read_bytes(), before)
            with self.assertRaises(PermissionError):
                path.write_bytes(b"changed")
            with self.assertRaises(PermissionError):
                path.rename(path.with_suffix(".moved"))
        self.assertEqual(path.read_bytes(), before)

    @unittest.skipUnless(os.name == "nt", "Windows Job Object interruption acceptance")
    def test_interruption_preserves_confirmed_cleanup_receipt(self):
        import subprocess
        original = subprocess.Popen.wait
        first = True

        def interrupt_once(process, *arguments, **keywords):
            nonlocal first
            if first:
                first = False
                raise KeyboardInterrupt()
            return original(process, *arguments, **keywords)

        with patch.object(subprocess.Popen, "wait", interrupt_once):
            with self.assertRaises(BuildError) as raised:
                run_build_command([sys.executable, "-I", "-S", "-c", "import time;time.sleep(90)"],
                                  self.root, 10, self.root / "stdout.log", self.root / "stderr.log")
        self.assertEqual(raised.exception.code, "interrupted")
        self.assertTrue(raised.exception.receipt["tree_exit_confirmed"])
        self.assertTrue(raised.exception.receipt["output_complete"])

    @unittest.skipUnless(os.name == "nt", "Windows Job Object acceptance")
    def test_timeout_confirms_nested_child_exit_and_retains_output(self):
        pid_path = self.root / "child.pid"
        code = ("import subprocess,sys,time; "
                "child=subprocess.Popen([sys.executable,'-I','-S','-c','import time;time.sleep(90)']); "
                "open(sys.argv[1],'w').write(str(child.pid)); "
                "print('worker-started',flush=True); time.sleep(90)")
        result = run_build_command([sys.executable, "-I", "-S", "-c", code, str(pid_path)],
                                   self.root, 1.5, self.root / "stdout.log", self.root / "stderr.log")
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["containment_assigned"])
        self.assertTrue(result["tree_exit_confirmed"])
        self.assertEqual(result["job_active_processes_after"], 0)
        self.assertTrue(result["output_complete"])
        self.assertIn(b"worker-started", (self.root / "stdout.log").read_bytes())
        self.assertTrue(pid_path.is_file())

    @unittest.skipUnless(os.name == "nt", "Windows Job Object acceptance")
    def test_success_cleans_residual_worker_after_root_exit(self):
        code = ("import subprocess,sys; subprocess.Popen([sys.executable,'-I','-S','-c',"
                "'import time;time.sleep(90)']); print('parent-complete',flush=True)")
        result = run_build_command([sys.executable, "-I", "-S", "-c", code], self.root, 10,
                                   self.root / "stdout.log", self.root / "stderr.log")
        self.assertEqual(result["state"], "succeeded")
        self.assertTrue(result["termination_requested"])
        self.assertTrue(result["tree_exit_confirmed"])
        self.assertTrue(result["output_complete"])


if __name__ == "__main__":
    unittest.main()
