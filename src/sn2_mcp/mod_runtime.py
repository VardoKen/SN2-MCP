"""Control independently compiled native mods using the inherited process boundary."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import secrets

from .development import CommandResult, DevelopmentError, RuntimeService, _unique_json
from .workspace import WorkspaceError, _checked, _guard_directories, _read_bytes


class ModRuntime:
    """Load once, inspect or disable a process-lifetime workspace DLL."""

    def __init__(self, service: RuntimeService, workspace_root: Path | None = None):
        self.service = service
        try:
            self.workspace = _checked(workspace_root or service.repository, directory=True)
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None

    def _artifact(self, dll_path: str) -> tuple[Path, Path]:
        dll = _checked(Path(dll_path))
        if hasattr(self.service, "game") and isinstance(self.service.game, Path) and dll.is_relative_to(self.service.game):
            raise DevelopmentError("protected_path", "Installed game files are not mod workspace artifacts")
        for boundary in (self.workspace, self.service.repository):
            if dll.is_relative_to(boundary):
                return _checked(dll, boundary), boundary
        raise DevelopmentError("unsafe_path", "The mod DLL is outside the configured source workspace and repository")

    def load(self, target_process: dict, dll_path: str, expected_dll_sha256: str,
             *, timeout_seconds: float = 30.0) -> dict:
        return self._control("start", target_process, dll_path, expected_dll_sha256, timeout_seconds)

    def status(self, target_process: dict, dll_path: str, expected_dll_sha256: str,
               *, timeout_seconds: float = 30.0, request_id: int | None = None) -> dict:
        return self._control("status", target_process, dll_path, expected_dll_sha256, timeout_seconds, request_id)

    def disable(self, target_process: dict, dll_path: str, expected_dll_sha256: str,
                *, timeout_seconds: float = 30.0) -> dict:
        return self._control("disable", target_process, dll_path, expected_dll_sha256, timeout_seconds)

    def _control(self, operation: str, target_process: dict, dll_path: str,
                 expected_dll_sha256: str, timeout_seconds: float, queried_request_id: int | None = None) -> dict:
        if self.service._closed:
            raise DevelopmentError("service_closed", "The runtime service is closed")
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise DevelopmentError("invalid_timeout", "A positive finite command timeout is required")
        if timeout_seconds > ((1 << 63) - 1) / 1000:
            raise DevelopmentError("invalid_timeout", "The timeout cannot be represented by the native command clock")
        if (not isinstance(expected_dll_sha256, str) or len(expected_dll_sha256) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in expected_dll_sha256)):
            raise DevelopmentError("invalid_dll_hash", "The expected DLL SHA-256 is required")
        if not isinstance(dll_path, str) or not Path(dll_path).is_absolute():
            raise DevelopmentError("invalid_dll_path", "The DLL path must be absolute")
        if queried_request_id is not None and (type(queried_request_id) is not int or not 0 < queried_request_id < 1 << 64):
            raise DevelopmentError("invalid_request_id", "The queried command request ID must be a positive uint64")
        target = self.service.validate_target(target_process)
        try:
            dll, artifact_boundary = self._artifact(dll_path)
            controller = _checked(self.service.repository / "build/native/Release/sn2_mcp_mod_control.exe",
                                  self.service.repository)
            with _guard_directories(dll.parent, artifact_boundary):
                digest = hashlib.sha256(_read_bytes(dll, artifact_boundary, None)).hexdigest()
                if digest != expected_dll_sha256.lower():
                    raise DevelopmentError("dll_changed", "The DLL does not match its expected SHA-256")
                request_id = queried_request_id or secrets.randbits(64) or 1
                arguments = [str(controller), "--pid", str(target["pid"]), "--dll", str(dll),
                             "--expected-exe", target["executable"], "--expected-creation-filetime",
                             target["creation_time"], "--expected-sha256", digest,
                             "--operation", operation, "--request-id", str(request_id),
                             "--timeout-ms", str(max(1, math.ceil(timeout_seconds * 1000)))]
                try:
                    result = self.service.executor(arguments, timeout_seconds + 2.0)
                except DevelopmentError as error:
                    raise DevelopmentError(error.code,
                        "The local mod controller did not confirm completion; inspect the selected process before another mutation",
                        outcome_unknown=error.outcome_unknown,
                        receipt={"operation": operation, "request_id": request_id,
                                 "process": target, "dll_path": str(dll), "dll_sha256": digest,
                        "outcome_unknown": error.outcome_unknown,
                        "controller_error_receipt": error.receipt}) from None
        except WorkspaceError as error:
            raise DevelopmentError(error.code, str(error)) from None
        if not isinstance(result, CommandResult):
            raise DevelopmentError("tool_protocol_error", "The mod controller returned an invalid result",
                                   outcome_unknown=True)
        try:
            receipt = json.loads(result.stdout.lstrip("\ufeff"), object_pairs_hook=_unique_json)
        except (ValueError, TypeError):
            raise DevelopmentError("mod_control_unconfirmed", "The mod controller did not return a complete JSON receipt",
                outcome_unknown=True, receipt={"stdout": result.stdout, "stderr": result.stderr,
                                              "returncode": result.returncode, "request_id": request_id}) from None
        if not isinstance(receipt, dict) or type(receipt.get("outcome_unknown")) is not bool:
            raise DevelopmentError("mod_control_unconfirmed", "The mod controller receipt is malformed",
                                   outcome_unknown=True, receipt=receipt if isinstance(receipt, dict) else None)
        if result.returncode != 0 or receipt.get("ok") is not True:
            raise DevelopmentError(receipt.get("code", "mod_control_failed"),
                receipt.get("message", "The mod command did not complete"),
                outcome_unknown=receipt["outcome_unknown"], receipt=receipt)
        if (receipt.get("process") != target or receipt.get("dll_sha256") != digest
                or receipt.get("operation") != operation or receipt.get("request_id") != request_id
                or type(receipt.get("module_loaded")) is not bool):
            raise DevelopmentError("mod_identity_unconfirmed", "The mod command did not confirm the selected artifact and process",
                                   outcome_unknown=True, receipt=receipt)
        return {**receipt, "controller_stdout": result.stdout, "controller_stderr": result.stderr,
                "controller_returncode": result.returncode,
                "residency": "process_lifetime", "hot_unload_supported": False}
