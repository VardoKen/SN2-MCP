"""MSVC/CMake diagnostics preserve supplied output and exact source attribution."""

import hashlib
from pathlib import Path
import unittest

from sn2_mcp.compile_diagnostics import parse_build_report


class CppDiagnosticsTests(unittest.TestCase):
    root = Path("E:/isolated/native project")
    sources = [
        {"path": "native/src/entry.cpp", "sha256": "a" * 64},
        {"path": "native/include/bridge.hpp", "sha256": "b" * 64},
        {"path": "native/CMakeLists.txt", "sha256": "c" * 64},
    ]

    def parse(self, text, sources=None):
        return parse_build_report(text, self.sources if sources is None else sources, self.root)

    def test_msvc_absolute_relative_and_header_locations_preserve_severity_and_code(self):
        text = (
            '1>E:\\isolated\\native project\\native\\src\\entry.cpp(37,18): error C2065: '
            "'missing_symbol': undeclared identifier [E:\\build\\SN2_MCP.vcxproj]\r\n"
            "native/include/bridge.hpp(12): warning C4100: 'unused': unreferenced formal parameter\r\n"
            '"E:/ISOLATED/native project/native/src/entry.cpp"(8,2) : note: '
            "see declaration of 'FBridge'\n"
        )
        report = self.parse(text)
        first, warning, note = report["diagnostics"]
        self.assertEqual((first["file"], first["line"], first["column"]),
                         ("native/src/entry.cpp", 37, 18))
        self.assertEqual(first["reported_file"], "E:\\isolated\\native project\\native\\src\\entry.cpp")
        self.assertEqual((first["severity"], first["code"]), ("error", "C2065"))
        self.assertEqual(first["source_sha256"], "a" * 64)
        self.assertEqual(first["source_match_basis"], "exact_workspace_path")
        self.assertEqual(first["message"], "'missing_symbol': undeclared identifier")
        self.assertEqual(first["project_file"], "E:\\build\\SN2_MCP.vcxproj")
        self.assertEqual((warning["file"], warning["line"], warning["column"], warning["severity"]),
                         ("native/include/bridge.hpp", 12, None, "warning"))
        self.assertEqual(warning["source_sha256"], "b" * 64)
        self.assertEqual((note["line"], note["column"], note["severity"], note["code"]),
                         (8, 2, "note", None))
        self.assertEqual([item["report_line"] for item in report["diagnostics"]], [1, 2, 3])
        self.assertEqual(report["unparsed_lines"], [])
        self.assertFalse(report["compiled"])
        self.assertFalse(report["exact_source_compiled"])
        self.assertEqual(report["compilation_state"], "errors_reported")

    def test_tool_and_object_linker_diagnostics_do_not_invent_a_source_location(self):
        text = (
            "LINK : fatal error LNK1104: cannot open file 'SN2_MCP.dll'\n"
            "entry.obj : error LNK2019: unresolved external symbol Init referenced in function DllMain\n"
            "cl : command line warning D9002: ignoring unknown option '/fixture'\n"
            "fatal error C1060: compiler is out of heap space\n"
        )
        report = self.parse(text)
        link, object_error, command_warning, fatal = report["diagnostics"]
        self.assertEqual((link["tool"], link["file"], link["code"], link["severity"]),
                         ("LINK", None, "LNK1104", "error"))
        self.assertEqual((object_error["reported_file"], object_error["code"]),
                         ("entry.obj", "LNK2019"))
        self.assertFalse(object_error["source_matched"])
        self.assertIsNone(object_error["source_sha256"])
        self.assertEqual((command_warning["tool"], command_warning["scope"], command_warning["code"]),
                         ("cl", "command line", "D9002"))
        self.assertEqual((fatal["code"], fatal["line"], fatal["column"]), ("C1060", None, None))
        self.assertEqual(report["unparsed_lines"], [])

    def test_msbuild_range_and_message_brackets_preserve_actual_location_and_text(self):
        text = (
            "native/src/entry.cpp(4,5,6,7): error C2143: missing token [context] "
            "[E:/build/SN2_MCP.vcxproj]\n"
            "native/src/entry.cpp(8): warning C4100: trailing text  \t\r\n"
        )
        error, warning = self.parse(text)["diagnostics"]
        self.assertEqual((error["line"], error["column"], error["end_line"], error["end_column"]),
                         (4, 5, 6, 7))
        self.assertEqual(error["message"], "missing token [context]")
        self.assertEqual(error["project_file"], "E:/build/SN2_MCP.vcxproj")
        self.assertEqual(warning["message"], "trailing text  \t")
        self.assertIsNone(warning["column"])
        self.assertIsNone(warning["end_column"])

    def test_cmake_at_in_and_locationless_headers_preserve_multiline_body(self):
        text = (
            "-- Configuring\r\n"
            "CMake Error at E:/isolated/native project/native/CMakeLists.txt:32 (add_library):\r\n"
            "  Cannot find source file:\r\n"
            "\r\n"
            "    src/missing.cpp\r\n"
            "Call Stack (most recent call first):\r\n"
            "  native/CMakeLists.txt:40 (include)\r\n"
            "\r\n"
            "CMake Warning (dev) in native/CMakeLists.txt:\n"
            "  No project() command is present.\n"
            "CMake Deprecation Warning at native/CMakeLists.txt:1 (cmake_minimum_required):\n"
            "  Compatibility with CMake < 3.10 will be removed.\n"
            "CMake Error: CMAKE_CXX_COMPILER not set, after EnableLanguage\n"
            "-- Configuring incomplete, errors occurred!\n"
        )
        report = self.parse(text)
        error, warning, deprecation, locationless = report["diagnostics"]
        self.assertEqual((error["file"], error["line"], error["column"], error["command"]),
                         ("native/CMakeLists.txt", 32, None, "add_library"))
        self.assertEqual(error["source_sha256"], "c" * 64)
        self.assertEqual(error["message"],
                         "  Cannot find source file:\r\n\r\n    src/missing.cpp\r\n"
                         "Call Stack (most recent call first):\r\n"
                         "  native/CMakeLists.txt:40 (include)\r\n\r\n")
        self.assertEqual((warning["severity"], warning["category"], warning["line"]),
                         ("warning", "dev", None))
        self.assertEqual(deprecation["severity_text"], "Deprecation Warning")
        self.assertEqual(deprecation["line"], 1)
        self.assertEqual((locationless["file"], locationless["line"], locationless["code"]),
                         (None, None, None))
        self.assertEqual(locationless["message"], "CMAKE_CXX_COMPILER not set, after EnableLanguage")
        self.assertEqual(report["unparsed_output"],
                         "-- Configuring\r\n-- Configuring incomplete, errors occurred!\n")
        self.assertEqual([item["report_line"] for item in report["diagnostics"]], [2, 9, 11, 13])
        self.assertEqual(report["text"], text)
        self.assertEqual(report["report_sha256"], hashlib.sha256(text.encode("utf-8")).hexdigest())

    def test_no_basename_or_suffix_match_outside_exact_inventory_paths(self):
        sources = [
            {"path": "native/src/entry.cpp", "sha256": "a" * 64},
            {"path": "mods/probe/entry.cpp", "sha256": "b" * 64},
        ]
        text = (
            "entry.cpp(4,2): error C2065: no source working directory supplied\n"
            "E:/foreign/native project/native/src/entry.cpp(5): warning C4100: other checkout\n"
            "E:native/src/entry.cpp(6): error C2065: drive-relative path\n"
            "mods/probe/entry.cpp(7): error C2065: exact path\n"
            "native/src/../src/entry.cpp(8): error C2065: normalized exact path\n"
        )
        diagnostics = self.parse(text, sources)["diagnostics"]
        self.assertEqual([item["source_matched"] for item in diagnostics],
                         [False, False, False, True, True])
        self.assertEqual([item["source_sha256"] for item in diagnostics],
                         [None, None, None, "b" * 64, "a" * 64])
        self.assertEqual(diagnostics[0]["file"], "entry.cpp")
        self.assertEqual(diagnostics[1]["reported_file"],
                         "E:/foreign/native project/native/src/entry.cpp")

    def test_relative_cmake_location_uses_actual_source_directory_without_basename_search(self):
        sources = self.sources + [{"path": "mods/probe/CMakeLists.txt", "sha256": "e" * 64}]
        text = "CMake Error at CMakeLists.txt:8 (add_library):\n  No SOURCES given to target: Probe\n"
        unknown = self.parse(text, sources)["diagnostics"][0]
        self.assertFalse(unknown["source_matched"])
        matched = parse_build_report(text, sources, self.root,
                                     source_directory=self.root / "mods/probe")["diagnostics"][0]
        self.assertEqual(matched["reported_file"], "CMakeLists.txt")
        self.assertEqual(matched["file"], "mods/probe/CMakeLists.txt")
        self.assertEqual(matched["source_sha256"], "e" * 64)
        outside = parse_build_report(text, sources, self.root,
                                     source_directory=Path("E:/foreign/probe"))["diagnostics"][0]
        self.assertFalse(outside["source_matched"])
        # The CMake source directory must not change MSVC source resolution.
        msvc = parse_build_report("native/src/entry.cpp(8): error C2065: missing\n", sources, self.root,
                                  source_directory=self.root / "mods/probe")["diagnostics"][0]
        self.assertEqual(msvc["source_sha256"], "a" * 64)

    def test_ambiguous_duplicate_inventory_and_escaping_entries_are_not_attributed(self):
        sources = [
            {"path": "native/src/entry.cpp", "sha256": "a" * 64},
            {"path": "NATIVE/SRC/ENTRY.CPP", "sha256": "b" * 64},
            {"path": "../foreign/entry.cpp", "sha256": "c" * 64},
        ]
        text = (
            "native/src/entry.cpp(4): error C2065: ambiguous inventory\n"
            "../foreign/entry.cpp(5): error C2065: outside workspace\n"
        )
        first, second = self.parse(text, sources)["diagnostics"]
        self.assertEqual(first["source_match_status"], "ambiguous")
        self.assertEqual(second["source_match_status"], "unmatched")
        self.assertIsNone(first["source_sha256"])
        self.assertIsNone(second["source_sha256"])

    def test_supplied_localized_messages_and_paths_are_preserved(self):
        sources = [{"path": "native/Проба.cpp", "sha256": "d" * 64}]
        text = (
            "E:/isolated/native project/native/Проба.cpp(9,4): ошибка C2065: "
            "'неизвестный': необъявленный идентификатор\r\n"
            "native/Проба.cpp(12): предупреждение C4100: "
            "'параметр': неиспользованный формальный параметр\n"
            "native/Проба.cpp(3): fatal error C1083: Не удается открыть файл включение: "
            "'Отсутствует.hpp': No such file or directory\n"
        )
        error, warning, fatal = self.parse(text, sources)["diagnostics"]
        self.assertEqual((error["severity"], error["severity_text"]), ("error", "ошибка"))
        self.assertEqual(error["message"], "'неизвестный': необъявленный идентификатор")
        self.assertEqual(warning["severity"], "warning")
        self.assertEqual(warning["message"], "'параметр': неиспользованный формальный параметр")
        self.assertEqual(fatal["severity"], "error")
        self.assertTrue(all(item["source_sha256"] == "d" * 64 for item in (error, warning, fatal)))

    def test_actual_20261112_msvc_controlled_failure_binds_staged_source(self):
        # Observed MSBuild 17.14.51 output from the local controlled C2065
        # build. Workspace prefixes are replaced with isolated paths.
        root = Path("E:/isolated/bf/build/mcp/553d07f91ea09f76/s")
        source_sha = "a760c5024b05632c66e0b9df8055f752dc77cea11a5e26da4196c37e270f6597"
        text = (
            "Версия MSBuild 17.14.51+25f168cee для .NET Framework\r\n\r\n"
            "  1>Checking Build System\r\n"
            "  Building Custom Rule E:/isolated/bf/build/mcp/553d07f91ea09f76/s/native/CMakeLists.txt\r\n"
            "  example.cpp\r\n"
            "E:\\isolated\\bf\\build\\mcp\\553d07f91ea09f76\\s\\native\\src\\example.cpp(1,57): "
            "error C2065: missing_identifier: необъявленный идентификатор "
            "[E:\\isolated\\bf\\build\\mcp\\553d07f91ea09f76\\b\\sn2_mcp_native.vcxproj]\r\n"
        )
        report = parse_build_report(text, [{"path": "native/src/example.cpp", "sha256": source_sha}], root)
        self.assertEqual(len(report["diagnostics"]), 1)
        error = report["diagnostics"][0]
        self.assertEqual((error["file"], error["line"], error["column"], error["code"]),
                         ("native/src/example.cpp", 1, 57, "C2065"))
        self.assertEqual(error["source_sha256"], source_sha)
        self.assertEqual(error["message"], "missing_identifier: необъявленный идентификатор")
        self.assertEqual(report["text"], text)
        self.assertFalse(report["compiled"])
        self.assertFalse(report["exact_source_compiled"])

    def test_report_and_unparsed_output_are_complete_without_implied_success(self):
        message = "supplied diagnostic " + "x" * 20000
        text = (
            "Microsoft (R) Build Engine version 17.0\r\n"
            + "".join("native/src/entry.cpp(1): warning C4100: " + message + "\n" for _ in range(250))
            + "unsupported compiler output: Ошибка\r\n"
            + "Build succeeded.\n"
        )
        report = self.parse(text)
        self.assertEqual(len(report["diagnostics"]), 250)
        self.assertEqual(report["diagnostics"][-1]["message"], message)
        self.assertEqual(report["text"], text)
        self.assertEqual(report["unparsed_output"],
                         "Microsoft (R) Build Engine version 17.0\r\n"
                         "unsupported compiler output: Ошибка\r\nBuild succeeded.\n")
        self.assertFalse(report["truncated"])
        self.assertFalse(report["unparsed_truncated"])
        self.assertFalse(report["compiled"])
        self.assertFalse(report["exact_source_compiled"])
        self.assertEqual(report["compilation_state"], "unknown")
        empty = self.parse("Build succeeded.\n")
        self.assertEqual(empty["diagnostics"], [])
        self.assertFalse(empty["compiled"])
        self.assertEqual(empty["compilation_state"], "unknown")


if __name__ == "__main__":
    unittest.main()
