"""Отчёт компилятора не заменяет runtime-подтверждение и связан с байтами пакета."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from g1r_mcp.compile_diagnostics import parse_report, read_report
from g1r_mcp.development import DevelopmentService, DevelopmentError


class CompileDiagnosticsTests(unittest.TestCase):
    def test_formats_bind_full_package_suffix_and_do_not_claim_compile_success(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        report = parse_report('C:\\workspace\\Probe\\Script\\Probe.as (48:1) : Error: Missing override\nScript/Probe.as:12:3: Warning: unused\nOther/Probe.as (4,2) : Error: foreign', "Probe", files)
        self.assertEqual([d["file"] for d in report["diagnostics"]], ["Script/Probe.as", "Script/Probe.as", None])
        self.assertEqual(report["diagnostics"][0]["source_sha256"], "a" * 64)
        self.assertEqual(report["diagnostics"][0]["line"], 48)
        self.assertEqual(report["compilation_state"], "errors_reported")
        self.assertFalse(report["compiled"])
        self.assertFalse(report["exact_source_compiled"])
        self.assertFalse(report["report_matches_runtime_compile"])
        empty = parse_report("Everything compiled", "Probe", files)
        self.assertEqual(empty["compilation_state"], "unknown")
        self.assertFalse(empty["compiled"])

    def test_read_report_bounds_paths_and_reports_current_changed_package_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); workspace = root / "workspace"; package = workspace / "Probe"
            (package / "Script").mkdir(parents=True)
            for name in ("repo", "modkit", "game"): (root / name).mkdir()
            (package / "GothicMod.json").write_text(json.dumps({"name":"Probe","title":"Probe","author":"Test","modVersion":"1.0.0"}), encoding="utf-8")
            script = package / "Script/Probe.as"; script.write_text("class UProbe {}", encoding="utf-8")
            (workspace / "diagnostic.txt").write_text("Script/Probe.as (3:1) : Error: fixture", encoding="utf-8")
            service = DevelopmentService(repository_root=root / "repo", workspace_root=workspace, modkit_root=root / "modkit", game_root=root / "game")
            manifest = service._manifest("Probe")
            result = read_report(service, "Probe", manifest["manifest_sha256"], "diagnostic.txt")
            self.assertEqual(result["manifest_sha256"], manifest["manifest_sha256"])
            self.assertEqual(result["diagnostics"][0]["source_sha256"], manifest["files"][1]["sha256"])
            for path in ("../diagnostic.txt", str(workspace / "diagnostic.txt"), "Probe/Script/Probe.as"):
                with self.assertRaises(DevelopmentError): read_report(service, "Probe", manifest["manifest_sha256"], path)
            script.write_text("class UChanged {}", encoding="utf-8")
            result = read_report(service, "Probe", manifest["manifest_sha256"], "diagnostic.txt")
            self.assertEqual(result["manifest_sha256"], service._manifest("Probe")["manifest_sha256"])
            self.assertEqual(result["diagnostics"][0]["source_sha256"], hashlib.sha256(script.read_bytes()).hexdigest())
            self.assertEqual(result["source_current_correlation"], "unknown")

    def test_all_diagnostics_and_unparsed_lines_are_returned(self):
        text = "\n".join("Script/A.as (1:1) : Error: item" for _ in range(205))
        report = parse_report(text + "\nFATAL unknown format", "Probe", [{"path":"Script/A.as","sha256":"a" * 64}])
        self.assertEqual(len(report["diagnostics"]), 205)
        self.assertFalse(report["truncated"])
        self.assertEqual(report["unparsed_error_lines"], ["FATAL unknown format"])

    def test_error_and_fatal_after_many_warnings_are_returned_and_set_compilation_state(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        prefix = "\n".join("Script/Probe.as (1:1) : Warning: item " + str(index)
                           for index in range(200))
        for severity in ("Error", "Fatal"):
            with self.subTest(severity=severity):
                report = parse_report(prefix + "\nScript/Probe.as (2:1) : " + severity + ": final error",
                                      "Probe", files)
                self.assertEqual(report["compilation_state"], "errors_reported")
                self.assertEqual(len(report["diagnostics"]), 201)
                self.assertTrue(all(item["severity"] == "warning" for item in report["diagnostics"][:200]))
                self.assertEqual(report["diagnostics"][-1]["severity"], "error")
                self.assertEqual(report["diagnostics"][-1]["message"], "final error")
                self.assertFalse(report["truncated"])
                self.assertEqual(report["unparsed_error_lines"], [])
                self.assertFalse(report["unparsed_truncated"])
                self.assertFalse(report["compiled"])
                self.assertFalse(report["report_matches_runtime_compile"])
                self.assertEqual(report["report_provenance"], "caller_selected_workspace_file")

    def test_all_warnings_are_returned_without_claiming_compile_success(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        text = "\n".join("Script/Probe.as (1:1) : Warning: item " + str(index)
                         for index in range(201))
        report = parse_report(text, "Probe", files)
        self.assertEqual(report["compilation_state"], "unknown")
        self.assertEqual(len(report["diagnostics"]), 201)
        self.assertFalse(report["truncated"])
        self.assertFalse(report["compiled"])
        self.assertFalse(report["report_matches_runtime_compile"])

    def test_invalid_error_coordinates_after_many_warnings_are_preserved_in_full(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        prefix = "\n".join("Script/Probe.as (1:1) : Warning: item " + str(index)
                           for index in range(200))
        invalid = ["Script/Probe.as (0:1) : Error: zero line",
                   "Script/Probe.as:1:0: Fatal: zero column",
                   "Script/Probe.as (" + "9" * 5000 + ":1) : Error: invalid integer"]
        report = parse_report(prefix + "\n" + "\n".join(invalid), "Probe", files)
        self.assertEqual(report["compilation_state"], "unknown")
        self.assertEqual(len(report["diagnostics"]), 200)
        self.assertTrue(all(item["severity"] == "warning" for item in report["diagnostics"]))
        self.assertEqual(len(report["unparsed_error_lines"]), 3)
        self.assertEqual(report["unparsed_error_lines"], invalid)
        self.assertFalse(report["unparsed_truncated"])
        self.assertFalse(report["truncated"])
        self.assertFalse(report["compiled"])
        self.assertFalse(report["report_matches_runtime_compile"])

    def test_long_messages_and_all_unparsed_errors_are_returned_in_full(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        message = parse_report("Script/Probe.as (1:1) : Error: " + "x" * 5000, "Probe", files)
        self.assertFalse(message["truncated"])
        self.assertFalse(message["diagnostics"][0]["message_truncated"])
        self.assertEqual(message["diagnostics"][0]["message"], "x" * 5000)
        self.assertFalse(message["unparsed_truncated"])
        many = parse_report("\n".join("Error: item " + str(i) for i in range(25)), "Probe", files)
        self.assertFalse(many["truncated"])
        self.assertFalse(many["unparsed_truncated"])
        self.assertEqual(many["unparsed_error_lines"], ["Error: item " + str(i) for i in range(25)])
        long_raw = parse_report("FATAL " + "x" * 3000, "Probe", files)
        self.assertFalse(long_raw["truncated"])
        self.assertFalse(long_raw["unparsed_truncated"])
        self.assertEqual(long_raw["unparsed_error_lines"], ["FATAL " + "x" * 3000])
        exact = parse_report("Script/Probe.as (1:1) : Error: " + "x" * 4096, "Probe", files)
        self.assertFalse(exact["truncated"])
        self.assertFalse(exact["diagnostics"][0]["message_truncated"])
        self.assertFalse(exact["compiled"])
        self.assertFalse(exact["report_matches_runtime_compile"])

    def test_quoted_windows_and_unicode_paths_bind_without_exposing_absolute_path(self):
        files = [{"path": "Script/Проба.as", "sha256": "a" * 64}]
        text = '"C:\\workspace\\Probe\\Script\\Проба.as" (48:1) : Error: missing\n\'Script/Проба.as\':12:3: Warning: unused'
        report = parse_report(text, "Probe", files)
        self.assertEqual([item["file"] for item in report["diagnostics"]], ["Script/Проба.as", "Script/Проба.as"])
        self.assertEqual([item["severity"] for item in report["diagnostics"]], ["error", "warning"])
        self.assertEqual([item["line"] for item in report["diagnostics"]], [48, 12])
        self.assertTrue(all(item["source_sha256"] == "a" * 64 for item in report["diagnostics"]))
        self.assertFalse(report["truncated"])
        self.assertFalse(report["report_matches_runtime_compile"])
        apostrophe = parse_report("Script/O'Neil.as (1:1) : Error: legal filename", "Probe", [{"path": "Script/O'Neil.as", "sha256": "b" * 64}])
        self.assertEqual(apostrophe["diagnostics"][0]["file"], "Script/O'Neil.as")

    def test_zero_coordinates_are_preserved_as_unparsed_unknown(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        text = "Script/Probe.as (0:1) : Error: zero line\nScript/Probe.as:1:0: Error: zero column\nScript/Probe.as:2: Warning: valid absent column"
        report = parse_report(text, "Probe", files)
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["line"], 2)
        self.assertIsNone(report["diagnostics"][0]["column"])
        self.assertEqual(len(report["unparsed_error_lines"]), 2)
        self.assertEqual(report["compilation_state"], "unknown")
        self.assertFalse(report["compiled"])

    def test_unparseable_huge_coordinate_does_not_abort_following_diagnostic(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        text = "Script/Probe.as (" + "9" * 5000 + ":1) : Error: malformed\nScript/Probe.as (2:3) : Error: valid"
        report = parse_report(text, "Probe", files)
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["line"], 2)
        self.assertEqual(report["diagnostics"][0]["column"], 3)
        self.assertEqual(len(report["unparsed_error_lines"]), 1)
        self.assertEqual(report["unparsed_error_lines"][0], text.splitlines()[0])
        self.assertFalse(report["unparsed_truncated"])
        self.assertFalse(report["truncated"])
        self.assertFalse(report["compiled"])

    def test_report_larger_than_one_megabyte_preserves_full_utf8_message_and_raw_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            package = workspace / "Probe"
            (package / "Script").mkdir(parents=True)
            for name in ("repo", "modkit", "game"):
                (root / name).mkdir()
            (package / "GothicMod.json").write_text(json.dumps({
                "name": "Probe", "title": "Probe", "author": "Test", "modVersion": "1.0.0"
            }), encoding="utf-8")
            (package / "Script" / "Probe.as").write_text("class UProbe {}\n", encoding="utf-8")
            message = ("Подробная ошибка 🐉 " * 50000).rstrip()
            raw = ("\ufeffScript/Probe.as (3:1) : Error: " + message + "\n").encode("utf-8")
            self.assertGreater(len(raw), 1024 * 1024)
            (workspace / "large.log").write_bytes(raw)
            service = DevelopmentService(repository_root=root / "repo", workspace_root=workspace,
                                         modkit_root=root / "modkit", game_root=root / "game")
            self.addCleanup(service.close)
            manifest = service._manifest("Probe")
            report = read_report(service, "Probe", manifest["manifest_sha256"], "large.log")
            self.assertEqual(report["report_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(report["manifest_sha256"], manifest["manifest_sha256"])
            self.assertEqual(len(report["diagnostics"]), 1)
            self.assertEqual(report["diagnostics"][0]["message"], message)
            self.assertEqual(report["compilation_state"], "errors_reported")
            self.assertFalse(report["truncated"])
            self.assertFalse(report["diagnostics"][0]["message_truncated"])
            self.assertFalse(report["compiled"])
            self.assertFalse(report["report_matches_runtime_compile"])

    def test_native_formatter_headers_bind_styled_diagnostics_and_preserve_message_markup(self):
        # Формат текущего FormatDiagnostics, RVA 0x417d2b0: отдельный bold header,
        # два пробела, известный style, опциональные координаты и закрывающее </>.
        files = [{"path": "Script/Проба.as", "sha256": "a" * 64},
                 {"path": "Script/Other.as", "sha256": "b" * 64}]
        text = ("Angelscript code failed to compile at engine startup. Please fix the errors and save the script files to proceed.\n\n"
                "<RichTextBlock.Bold>C:\\workspace\\Probe\\Script\\Проба.as</>:\n"
                "  <Log.Error>(48:1) No matching symbol 'G1RMCP_CompilerErrorProbe_20261031'</>\n"
                "  <Log.Warning>(12:3) Type TArray<AActor> contains <Log.Unknown> literal</>\n"
                "  <NormalText.Subdued>(0:5) Note without a line</>\n"
                "  Compiling Проба\n"
                "\n<RichTextBlock.Bold>Script/Other.as</>:\n"
                "  <Log.Error>Module error without coordinates</>\n"
                "\n<RichTextBlock.Bold>C:/foreign/OtherPackage/Script/Проба.as</>:\n"
                "  <Log.Warning>(1:0) Foreign file</>\n")
        report = parse_report(text, "Probe", files)
        self.assertEqual([item["file"] for item in report["diagnostics"]],
                         ["Script/Проба.as"] * 4 + ["Script/Other.as", None])
        self.assertEqual([item["severity"] for item in report["diagnostics"]],
                         ["error", "warning", "info", "info", "error", "warning"])
        self.assertEqual([item["line"] for item in report["diagnostics"]], [48, 12, 0, None, None, 1])
        self.assertEqual([item["column"] for item in report["diagnostics"]], [1, 3, 5, None, None, 0])
        self.assertEqual(report["diagnostics"][1]["message"],
                         "Type TArray<AActor> contains <Log.Unknown> literal")
        self.assertEqual([item["source_sha256"] for item in report["diagnostics"]],
                         ["a" * 64] * 4 + ["b" * 64, None])
        self.assertEqual(report["compilation_state"], "errors_reported")
        self.assertFalse(report["truncated"])
        self.assertFalse(report["compiled"])
        self.assertFalse(report["report_matches_runtime_compile"])

    def test_native_formatter_preserves_full_multiline_message(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        message = "Первое сообщение TArray<AActor>\n  continuation <Unknown.Style> literal\n" + "подробности 🐉 " * 1000
        text = "<RichTextBlock.Bold>Script/Probe.as</>:\n  <Log.Error>(7:9) " + message + "</>\n"
        report = parse_report(text, "Probe", files)
        self.assertEqual(len(report["diagnostics"]), 1)
        self.assertEqual(report["diagnostics"][0]["message"], message)
        self.assertEqual(report["diagnostics"][0]["file"], "Script/Probe.as")
        self.assertEqual(report["diagnostics"][0]["line"], 7)
        self.assertEqual(report["diagnostics"][0]["column"], 9)
        self.assertFalse(report["diagnostics"][0]["message_truncated"])
        self.assertFalse(report["truncated"])

    def test_native_formatter_does_not_assign_later_header_to_earlier_styled_message(self):
        files = [{"path": "Script/Probe.as", "sha256": "a" * 64}]
        text = ("  <Log.Error>(2:1) Unbound before header</>\n"
                "<RichTextBlock.Bold>Script/Probe.as</>:\n"
                "  <Log.Warning>(3:1) Bound after header</>\n")
        report = parse_report(text, "Probe", files)
        self.assertEqual([item["file"] for item in report["diagnostics"]], [None, "Script/Probe.as"])
        self.assertEqual([item["source_sha256"] for item in report["diagnostics"]], [None, "a" * 64])

    def test_actual_dev27_native_formatter_compiling_context_and_error_coordinates(self):
        # 31.10.2026, artifacts/dev27-native-diagnostics-result.json:
        # compiler_diagnostics.text; личный префикс пути заменён изолированным.
        source_sha256 = "ba36d9f121fb9974a1707fce6dc9aad3aa4781bec9c7a357a75f479478ce297b"
        files = [{"path": "Script/G1RMCPProbe.as", "sha256": source_sha256}]
        text = ("\n<RichTextBlock.Bold>C:/isolated/artifacts/compiler-error-startup/G1RMCPProbe/Script/G1RMCPProbe.as</>:\n"
                "  (7:5) Compiling int UG1RMCPCompileProbe::ReadProbeValue()\n"
                "    <Log.Error>(9:16) 'G1RMCP_CompilerErrorProbe_20261031' is not declared</>\n")
        report = parse_report(text, "G1RMCPProbe", files)
        self.assertEqual(len(report["diagnostics"]), 2)
        self.assertEqual([item["file"] for item in report["diagnostics"]], ["Script/G1RMCPProbe.as"] * 2)
        self.assertEqual([item["source_sha256"] for item in report["diagnostics"]], [source_sha256] * 2)
        self.assertEqual([item["severity"] for item in report["diagnostics"]], ["info", "error"])
        self.assertEqual([(item["line"], item["column"]) for item in report["diagnostics"]], [(7, 5), (9, 16)])
        self.assertEqual([item["message"] for item in report["diagnostics"]],
                         ["Compiling int UG1RMCPCompileProbe::ReadProbeValue()",
                          "'G1RMCP_CompilerErrorProbe_20261031' is not declared"])
