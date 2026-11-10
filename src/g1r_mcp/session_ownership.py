"""Shared-session metadata and explicitly selected parallel-work protection."""
from __future__ import annotations
from datetime import datetime, timezone
from copy import deepcopy
import threading
import uuid
from typing import Any
from .development import DevelopmentError


class SessionOwnership:
    def __init__(self):
        self.lock = threading.RLock()
        self._record: dict | None = None
        self._protection: dict | None = None
        self._protection_owner: Any = None

    def status(self, owner: Any = None) -> dict:
        with self.lock:
            return {"claimed": self._record is not None,
                    "claim": deepcopy(self._record),
                    "protection": deepcopy(self._protection),
                    "protected_tools": list(self._protection["protected_tools"]) if self._protection else [],
                    "caller_owns_protection": self._protection is not None and owner == self._protection_owner,
                    "scope": "optional_tool_protection" if self._protection else "informational",
                    "control_required": False,
                    "ownership_enforced": self._protection is not None,
                    "disconnect_releases_claim": False,
                    "automatic_game_stop": False}

    def acquire(self, owner: Any, reason: str, *, process: dict | None = None,
                package: str | None = None, expected_claim_id: str | None = None,
                protected_tools: list[str] | None = None, takeover: bool = False) -> dict:
        with self.lock:
            if not isinstance(reason, str):
                raise DevelopmentError("invalid_session_reason", "The session description must be a string")
            if protected_tools is not None:
                if not isinstance(protected_tools, list) or not protected_tools or any(
                        not isinstance(tool, str) or not tool for tool in protected_tools):
                    raise DevelopmentError("invalid_protected_tools", "Select a non-empty list of protectable tool names; use g1r_session_release to clear protection")
                if not reason.strip():
                    raise DevelopmentError("invalid_session_reason", "Explicit protection requires a description of the ongoing work")
                self._check_expected(expected_claim_id)
                if self._protection is not None and owner != self._protection_owner and not takeover:
                    self._conflict()
                selected = sorted(set(protected_tools))
                if (self._protection is None or owner != self._protection_owner or
                        selected != self._protection["protected_tools"] or reason != self._protection["reason"]):
                    self._protection = {"claim_id": str(uuid.uuid4()), "owner_id": str(owner),
                                        "reason": reason, "protected_tools": selected,
                                        "claimed_at": datetime.now(timezone.utc).isoformat()}
                    self._protection_owner = owner
            if self._record is None:
                self._record = {"claim_id": str(uuid.uuid4()), "reason": reason,
                                "claimed_at": datetime.now(timezone.utc).isoformat(),
                                "process": dict(process) if process is not None else None,
                                "relative_package": package, "state": "claimed"}
            elif process is not None:
                self._record["process"] = dict(process)
            result = self.status(owner)
            if expected_claim_id is not None:
                result["resumed"] = True
            return result

    def _check_expected(self, expected_claim_id: str | None) -> None:
        if (expected_claim_id is not None and self._protection is not None and
                expected_claim_id != self._protection["claim_id"]):
            raise DevelopmentError("stale_session_claim", "The selected protection has changed; read g1r_session_status before replacing it")

    def _conflict(self, tool_name: str | None = None) -> None:
        error = DevelopmentError("session_protected", "Another MCP client explicitly protected this operation; read g1r_session_status for its reason, or explicitly release or take over the protection")
        error.receipt = {"protection": deepcopy(self._protection), "tool": tool_name,
                         "release_tool": "g1r_session_release", "takeover_parameter": "takeover"}
        raise error

    def check(self, owner: Any, tool_name: str) -> None:
        """Check only operations explicitly selected by a client, at admission."""
        with self.lock:
            if (self._protection is not None and tool_name in self._protection["protected_tools"]
                    and owner != self._protection_owner):
                self._conflict(tool_name)

    def require(self, owner: Any, process: dict | None = None) -> None:
        """Compatibility entry point; session metadata does not authorize control."""

    def bind(self, owner: Any, process: dict, state: str) -> None:
        with self.lock:
            if self._record is None:
                self.acquire(owner, "Native bridge attachment", process=process)
            self._record.update(process=dict(process), state=state)

    def launched(self, owner: Any, package: str) -> None:
        with self.lock:
            if self._record is None:
                self.acquire(owner, "Development launch", package=package)
            self._record.update(process=None, relative_package=package, state="launch_requested")

    def clear_information(self) -> bool:
        """Clear process metadata after a stop without changing explicit protection."""
        with self.lock:
            previous = self._record is not None
            self._record = None
            return previous

    def release(self, owner: Any, *, expected_claim_id: str | None = None) -> dict:
        with self.lock:
            self._check_expected(expected_claim_id)
            previous = self.status()["claim"]
            previous_protection = deepcopy(self._protection)
            self._record = None
            self._protection = None
            self._protection_owner = None
            return {"released": previous is not None or previous_protection is not None,
                    "previous_claim": previous, "previous_protection": previous_protection,
                    "game_stopped": False}

    def release_abandoned(self, expected_claim_id: str, *, game_running: bool) -> dict:
        """Clear optional metadata without requiring an absent game process."""
        with self.lock:
            return {**self.release(None, expected_claim_id=expected_claim_id), "recovery": "informational_claim_release"}
