"""Read a supplied diagnostic report in full; this is not a compiler API."""
from __future__ import annotations
import copy
import hashlib
import ntpath
import os
import re

from .development import DevelopmentError
from .workspace import WorkspaceError, _read_bytes, _parts, _checked

_FILE = r'''(?P<file>"[^"\r\n]+\.as"|'[^'\r\n]+\.as'|[^\r\n]+?\.as)'''
_ANGEL = re.compile(r"^" + _FILE + r"\s*\((?P<line>\d+)\s*[:,]\s*(?P<column>\d+)\)\s*:\s*(?P<rest>.+)$", re.I)
_COLON = re.compile(r"^" + _FILE + r":(?P<line>\d+)(?::(?P<column>\d+))?\s*:\s*(?P<rest>.+)$", re.I)
_HEADER = re.compile(r"^<RichTextBlock\.Bold>(?P<file>.+)</>:$")
_STYLE = re.compile(r"^\s*<(?P<style>Log\.Error|Log\.Warning|NormalText\.Subdued)>(?P<message>.*)$")
_POSITION = re.compile(r"^\((?P<line>\d+):(?P<column>\d+)\) (?P<message>[\s\S]*)$")


def parse_report(text: str, package_name: str, files: list[dict]) -> dict:
    known = {item["path"]: item["sha256"] for item in files if item["path"].lower().endswith(".as")}
    diagnostics = []
    unparsed = []
    current_file = None

    def append_diagnostic(location, line, column, severity, message):
        if location and location[0] in ('"', "'") and location[-1] == location[0]:
            location = location[1:-1]
        location = location.replace("\\", "/") if location else None
        # Do not expose absolute paths. Match the full suffix rather than the basename.
        options = [path for path in known if location == path
                   or (location is not None and location.endswith("/" + package_name + "/" + path))]
        bound = options[0] if len(options) == 1 else None
        diagnostics.append({"file": bound, "line": line, "column": column,
                            "severity": severity, "message": message, "message_truncated": False,
                            "source_sha256": known.get(bound), "package_source_matched": bound is not None,
                            "stage": "compiler_report"})

    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        header = _HEADER.fullmatch(raw.strip())
        if header:
            current_file = header["file"]
            continue
        style = _STYLE.fullmatch(raw)
        if style:
            message = style["message"]
            while not message.endswith("</>") and index < len(lines):
                message += "\n" + lines[index]
                index += 1
            if not message.endswith("</>"):
                unparsed.append(raw)
                continue
            message = message[:-3]
            position = _POSITION.fullmatch(message)
            line = column = None
            if position:
                try:
                    line, column = int(position["line"]), int(position["column"])
                except ValueError:
                    unparsed.append(raw)
                    continue
                message = position["message"]
            severity = {"Log.Error": "error", "Log.Warning": "warning", "NormalText.Subdued": "info"}[style["style"]]
            append_diagnostic(current_file, line, column, severity, message)
            continue
        if current_file is not None:
            message = raw.lstrip()
            position = _POSITION.fullmatch(message)
            if position and position["message"].startswith("Compiling "):
                append_diagnostic(current_file, int(position["line"]), int(position["column"]),
                                  "info", position["message"])
                continue
            if message.startswith("Compiling "):
                append_diagnostic(current_file, None, None, "info", message)
                continue
        match = _ANGEL.fullmatch(raw.strip()) or _COLON.fullmatch(raw.strip())
        if not match:
            if re.search(r"\b(error|warning|fatal)\b|\u043e\u0448\u0438\u0431\u043a", raw, re.I):
                unparsed.append(raw)
            continue
        try:
            line = int(match["line"])
            column = int(match["column"]) if match["column"] else None
        except ValueError:
            unparsed.append(raw)
            continue
        if line < 1 or (column is not None and column < 1):
            unparsed.append(raw)
            continue
        rest = match["rest"]
        severity = "error" if re.match(r"(?:error|fatal)\b", rest, re.I) else "warning" if re.match(r"warning\b", rest, re.I) else "unknown"
        message = re.sub(r"^(?:error|fatal|warning)\s*:\s*", "", rest, flags=re.I)
        append_diagnostic(match["file"], line, column, severity, message)
    return {"diagnostics": diagnostics, "truncated": False,
            "unparsed_truncated": False,
            "unparsed_error_lines": unparsed, "compiled": False,
            "compilation_state": "errors_reported" if any(d["severity"] == "error" for d in diagnostics) else "unknown",
            "compiler_api_available": False, "runtime_validation": "not_performed",
            "report_provenance": "caller_selected_workspace_file",
            "report_matches_runtime_compile": False, "exact_source_compiled": False}


def read_report(service, relative_package: str, expected_manifest_sha256: str | None,
                report_path: str, enable_ingame_tools: bool = False, *,
                show_script_errors: bool = False) -> dict:
    try:
        target = service.workspace._unprotected(_checked(service.workspace.root.joinpath(*_parts(report_path)), service.workspace.root))
        if target.suffix.lower() not in (".txt", ".log"):
            raise DevelopmentError("invalid_report_path", "A .txt or .log file inside the workspace is required")
        raw = _read_bytes(target, service.workspace.root, target.stat().st_size)
        text = raw.decode("utf-8-sig", errors="strict")
    except WorkspaceError as error:
        raise DevelopmentError(error.code, str(error)) from None
    except OSError:
        raise DevelopmentError("access_error", "Could not read the diagnostic report") from None
    except UnicodeError:
        raise DevelopmentError("invalid_report_encoding", "The diagnostic report must be UTF-8") from None
    manifest = service._manifest(relative_package, enable_ingame_tools=enable_ingame_tools,
                                 show_script_errors=show_script_errors)
    return {"package_name": manifest["package_name"], "manifest_sha256": manifest["manifest_sha256"],
            "report_sha256": hashlib.sha256(raw).hexdigest(),
            **parse_report(text, manifest["package_name"], manifest["files"]),
            "source_current_correlation": "unknown"}


def read_runtime_report(service, relative_package: str, expected_manifest_sha256: str | None, native_call,
                        *, enable_ingame_tools: bool = False, show_script_errors: bool = False) -> dict:
    before = _package_observation(service, relative_package, enable_ingame_tools, show_script_errors)
    native_report = native_call()
    text = native_report["text"]
    after = _package_observation(service, relative_package, enable_ingame_tools, show_script_errors)
    manifest = after["selected"]
    return {"package_name": manifest["package_name"], "manifest_sha256": manifest["manifest_sha256"],
            "report_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            **parse_report(text, manifest["package_name"], manifest["files"]),
            "text": text, "native_report": native_report, "process_id": native_report.get("process_id"),
            "source": native_report.get("source"), "status": native_report.get("status"),
            "compiler_api_available": True, "report_provenance": "native_compiler_formatter",
            "source_current_correlation": "unknown", "compiled": False,
            "report_matches_runtime_compile": False,
            **_reload_observation(service, before, after, native_report, expected_manifest_sha256)}


def _path_identity(path: str) -> str:
    # Native paths are Windows paths even when fixtures run on another host.
    return ntpath.normcase(ntpath.normpath(path))


def _package_observation(service, relative_package: str, enable_ingame_tools: bool,
                         show_script_errors: bool) -> dict:
    selected = service._manifest(relative_package, enable_ingame_tools=enable_ingame_tools,
                                 show_script_errors=show_script_errors)
    with service._observation_lock:
        record = copy.deepcopy(service._launch_observation)
    selected_path = selected["package_path"]
    packages = {selected_path: selected}
    unavailable = {}
    def package_identity(path):
        return os.path.normcase(os.path.abspath(service.workspace.root.joinpath(path)))
    selected_identity = package_identity(selected_path)
    active = None
    if (record and record.get("stage") == "process_running"
            and isinstance(record.get("process"), dict)):
        staging = record.get("development_staging") or {}
        matches = [item for item in staging.get("packages", [])
                   if any(package_identity(path) == selected_identity
                          for path in (item.get("source_package_path"), item.get("relative_package"))
                          if isinstance(path, str))]
        if len(matches) == 1:
            item = matches[0]
            active = {"launch_id": record["launch_id"], "process": record["process"],
                      "source_package_path": item["source_package_path"],
                      "staged_package_path": item["relative_package"]}
            for path in (active["source_package_path"], active["staged_package_path"]):
                if package_identity(path) == selected_identity:
                    packages[path] = selected
                elif path not in packages:
                    try:
                        packages[path] = service._manifest(path, enable_ingame_tools=enable_ingame_tools,
                                                           show_script_errors=show_script_errors)
                    except DevelopmentError as error:
                        unavailable[path] = {"code": error.code, "message": str(error)}
    return {"selected": selected, "packages": packages, "active": active, "unavailable": unavailable}


def _reload_observation(service, before: dict, after: dict, native_report: dict,
                        expected_manifest_sha256: str | None) -> dict:
    selected = after["selected"]
    selected_path = selected["package_path"]
    active = after["active"]
    active_matches_process = (active is not None and active == before["active"]
                              and active["process"].get("pid") == native_report.get("process_id"))
    source_path = active["source_package_path"] if active_matches_process else selected_path
    staged_path = active["staged_package_path"] if active_matches_process else None
    paths = {source_path: "canonical" if active_matches_process else "selected"}
    if staged_path:
        paths[staged_path] = "staged"
    observations = []
    for path, role in paths.items():
        previous = before["packages"].get(path)
        current = after["packages"].get(path)
        observations.append({"package_path": path, "role": role,
                             "available_before": previous is not None, "available_after": current is not None,
                             "error_before": before["unavailable"].get(path),
                             "error_after": after["unavailable"].get(path),
                             "manifest_sha256_before": previous["manifest_sha256"] if previous else None,
                             "manifest_sha256_after": current["manifest_sha256"] if current else None,
                             "manifest_stable_during_read": bool(previous and current
                                 and previous["manifest_sha256"] == current["manifest_sha256"])})

    known = {}
    for path, role in paths.items():
        package = after["packages"].get(path)
        if package is None:
            continue
        previous_files = {item["path"]: item["sha256"] for item in before["packages"].get(path, {}).get("files", [])}
        for item in package["files"]:
            if item["path"].lower().endswith(".as"):
                absolute = str(service.workspace.root.joinpath(path, item["path"]))
                known[_path_identity(absolute)] = {"package_path": path, "role": role,
                    "file_path": item["path"], "current_file_sha256": item["sha256"],
                    "file_sha256_before": previous_files.get(item["path"]),
                    "file_stable_during_read": previous_files.get(item["path"]) == item["sha256"]}

    receipt = native_report.get("last_completed_reload")
    matches = []
    if isinstance(receipt, dict):
        for index, pair in enumerate(receipt.get("changed_files", [])):
            for field in ("first", "second"):
                value = pair.get(field)
                # A suffix or basename does not identify the selected package.
                if isinstance(value, str) and ntpath.isabs(value):
                    match = known.get(_path_identity(value))
                    if match is not None:
                        matches.append({"changed_file_index": index, "pair_field": field, **match})
    before_manifest = before["selected"]["manifest_sha256"]
    after_manifest = selected["manifest_sha256"]
    return {"last_completed_reload": receipt,
            "reload_attempt_started": native_report.get("reload_attempt_started"),
            "reload_attempt_completed": native_report.get("reload_attempt_completed"),
            "observed_successful_reload": isinstance(receipt, dict) and receipt.get("success") is True,
            "reload_event_matches_selected_file": bool(matches),
            "reload_event_matches_selected_package": bool(matches),
            "reload_event_file_matches": matches,
            "reload_event_match_basis": "full_absolute_known_package_file_path",
            "reload_event_file_pair_semantics": "unknown",
            "compiler_read_reload_context_consistent": native_report.get("compiler_read_reload_context_consistent"),
            "manifest_sha256_before": before_manifest, "manifest_sha256_after": after_manifest,
            "manifest_stable_during_read": before_manifest == after_manifest,
            "expected_manifest_matches_current": (expected_manifest_sha256.lower() == after_manifest
                if isinstance(expected_manifest_sha256, str) else None),
            "package_observations": observations,
            "package_observations_complete": all(item["available_before"] and item["available_after"]
                                                 for item in observations),
            # File identity and observed reload success do not identify compiled bytes.
            "exact_source_compiled": False}
