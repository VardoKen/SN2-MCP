"""Compose the existing SN2 process scripts without Gothic launch semantics."""

from __future__ import annotations

import copy
import math
import os
import time

from .development import DevelopmentError, RuntimeService


class RuntimeLifecycle:
    """One explicit launch/stop sequence with independently observed identities.

    Discovery establishes an internal process, not native executor readiness.
    Native loading and handshake checks belong to the caller's composition.
    """

    def __init__(self, service: RuntimeService):
        self.service = service

    @staticmethod
    def _timeout(value: float) -> float:
        try:
            valid = (not isinstance(value, bool) and isinstance(value, (int, float))
                     and math.isfinite(value) and value > 0)
        except OverflowError:
            valid = False
        if not valid:
            raise DevelopmentError("invalid_timeout", "Discovery timeout must be a positive finite number of seconds")
        return float(value)

    def _check_open(self) -> None:
        if self.service._closed:
            raise DevelopmentError("service_closed", "The runtime service is closed")

    def _allowed_paths(self) -> set[str]:
        return {os.path.normcase(str(self.service.bootstrap_executable)),
                os.path.normcase(str(self.service.inner_executable))}

    def _identity(self, value: object) -> dict:
        target = self.service._runtime_identity(value)
        if not target["creation_time"].isdecimal() or not 0 < int(target["creation_time"]) <= 0x7FFFFFFFFFFFFFFF:
            raise DevelopmentError("invalid_process_identity", "The process creation FILETIME is not a valid positive Windows timestamp")
        if os.path.normcase(target["executable"]) not in self._allowed_paths():
            raise DevelopmentError("process_path_mismatch", "Select an executable of the configured Subnautica 2 game")
        return target

    def _snapshot(self) -> dict:
        snapshot = self.service.process_state()
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("processes"), list):
            raise DevelopmentError("tool_protocol_error", "Invalid game process snapshot")
        processes = [self._identity(item) for item in snapshot["processes"]]
        paths = [os.path.normcase(item["executable"]) for item in processes]
        if len(set(paths)) != len(paths):
            raise DevelopmentError("ambiguous_game_process", "Multiple instances of the same game executable were found")
        return {**snapshot, "processes": processes, "running": bool(processes)}

    @staticmethod
    def _protocol_error(message: str, receipt: dict) -> DevelopmentError:
        return DevelopmentError("tool_protocol_error", message, outcome_unknown=True,
                                receipt=copy.deepcopy(receipt))

    def _launch_receipt(self, receipt: dict) -> None:
        if (receipt.get("state") == "not_started" and receipt.get("outcome_unknown") is False
                and isinstance(receipt.get("executable"), str)
                and os.path.normcase(receipt["executable"]) == os.path.normcase(str(self.service.bootstrap_executable))
                and receipt.get("arguments") == [] and receipt.get("restart_requested") is False):
            raise DevelopmentError("game_launch_not_started", "The launch script did not start the game",
                                   receipt=copy.deepcopy(receipt))
        if (receipt.get("state") != "launch_requested"
                or receipt.get("outcome_unknown") is not False
                or not isinstance(receipt.get("executable"), str)
                or os.path.normcase(receipt["executable"]) != os.path.normcase(str(self.service.bootstrap_executable))
                or receipt.get("arguments") != []
                or type(receipt.get("bootstrap_pid")) is not int
                or not 1 <= receipt["bootstrap_pid"] <= 0xFFFFFFFF):
            raise self._protocol_error("The launch receipt does not identify the configured bootstrap request", receipt)

    def _cleanup(self, value: object, receipt: dict) -> list[dict]:
        if not isinstance(value, list):
            raise self._protocol_error("The termination receipt has no cleanup list", receipt)
        processes = []
        paths = set()
        for item in value:
            if not isinstance(item, dict):
                raise self._protocol_error("Invalid termination cleanup entry", receipt)
            state, mode = item.get("state"), item.get("mode")
            if (item.get("outcome_unknown") is not False
                    or type(item.get("grace_requests")) is not int or item["grace_requests"] < 0
                    or not isinstance(state, str) or not isinstance(mode, str)
                    or not ((state == "already_exited" and mode == "none")
                            or (state == "stopped" and mode in {"graceful", "exited_before_force", "forced"}))):
                raise self._protocol_error("A cleanup entry does not confirm process termination", receipt)
            try:
                process = self._identity(item.get("process"))
            except DevelopmentError:
                raise self._protocol_error("Invalid cleanup process identity", receipt) from None
            path = os.path.normcase(process["executable"])
            if process in processes or path in paths:
                raise self._protocol_error("Duplicate cleanup process identity", receipt)
            processes.append(process)
            paths.add(path)
        return processes

    def _stop_receipt(self, receipt: dict, target: dict) -> None:
        try:
            process = self._identity(receipt.get("process"))
        except DevelopmentError:
            raise self._protocol_error("Invalid selected identity in the termination receipt", receipt) from None
        if (receipt.get("state") == "not_started" and receipt.get("outcome_unknown") is False
                and process == target and receipt.get("cleanup") == []):
            raise DevelopmentError("game_stop_not_started", "The stop script did not terminate the selected game session",
                                   receipt=copy.deepcopy(receipt))
        cleanup = self._cleanup(receipt.get("cleanup"), receipt)
        if (receipt.get("state") != "stopped" or receipt.get("outcome_unknown") is not False
                or process != target or target not in cleanup
                or receipt.get("mode") != receipt["cleanup"][0]["mode"]):
            raise self._protocol_error("The receipt does not confirm termination of the selected game session", receipt)

    def _stop_failure(self, error: DevelopmentError, target: dict) -> DevelopmentError:
        """Recognize the existing stop schema, which has no loader native_result."""
        receipt = error.receipt
        if not isinstance(receipt, dict):
            return error
        try:
            if (receipt.get("state") != "failed" or receipt.get("code") != "session_cleanup_incomplete"
                    or not isinstance(receipt.get("message"), str) or not receipt["message"]
                    or type(receipt.get("outcome_unknown")) is not bool):
                return error
            if receipt.get("process") is not None and self._identity(receipt["process"]) != target:
                return error
            cleanup = self._cleanup(receipt.get("cleanup"), receipt)
            if (not receipt["outcome_unknown"] and cleanup) or (receipt["outcome_unknown"] and receipt.get("process") is None):
                return error
            return DevelopmentError(receipt["code"], receipt["message"],
                                    outcome_unknown=receipt["outcome_unknown"], receipt=copy.deepcopy(receipt))
        except DevelopmentError:
            return error

    def _discover(self, timeout: float, launch: dict, previous_process: dict | None) -> dict:
        deadline = time.monotonic() + timeout
        last_snapshot = None
        while True:
            try:
                last_snapshot = self._snapshot()
            except DevelopmentError as error:
                raise DevelopmentError(error.code, str(error), outcome_unknown=True,
                                       receipt={"launch_receipt": copy.deepcopy(launch),
                                                "process_snapshot": copy.deepcopy(last_snapshot),
                                                "observation_error_receipt": copy.deepcopy(error.receipt)}) from None
            inner = [item for item in last_snapshot["processes"]
                     if os.path.normcase(item["executable"]) == os.path.normcase(str(self.service.inner_executable))]
            if inner:
                process = inner[0]
                if process == previous_process:
                    raise DevelopmentError("process_changed", "The original process identity appeared after confirmed termination",
                                           outcome_unknown=True,
                                           receipt={"launch_receipt": copy.deepcopy(launch),
                                                    "process_snapshot": copy.deepcopy(last_snapshot)})
                return {"state": "started", "process": process,
                        "launch_receipt": copy.deepcopy(launch), "process_snapshot": last_snapshot,
                        "native_readiness_checked": False, "outcome_unknown": False}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DevelopmentError("game_process_not_observed", "The launch request was accepted but no internal game process was observed before the discovery deadline",
                                       outcome_unknown=True,
                                       receipt={"launch_receipt": copy.deepcopy(launch),
                                                "process_snapshot": copy.deepcopy(last_snapshot)})
            time.sleep(min(0.1, remaining))

    def _start(self, timeout: float, previous_process: dict | None = None) -> dict:
        snapshot = self._snapshot()
        if snapshot["processes"]:
            raise DevelopmentError("game_already_running", "A configured game process is already running; select it for a controlled restart",
                                   receipt={"process_snapshot": snapshot})
        launch = self.service._command("start_sn2_development.ps1", ["-GameRoot", str(self.service.game)],
                                       mutating=True, preserve_failure_receipt=True)
        self._launch_receipt(launch)
        return self._discover(timeout, launch, previous_process)

    def start(self, *, discovery_timeout_seconds: float = 30.0) -> dict:
        """Launch once and discover a current internal process within the caller's deadline."""
        timeout = self._timeout(discovery_timeout_seconds)
        with self.service._observation_lock:
            self._check_open()
            return self._start(timeout)

    def _stop(self, expected_process: dict) -> dict:
        target = self._identity(expected_process)
        snapshot = self._snapshot()
        if target not in snapshot["processes"]:
            raise DevelopmentError("process_changed", "The selected PID, creation time or executable changed",
                                   receipt={"process_snapshot": snapshot})
        try:
            receipt = self.service._command("stop_sn2_development.ps1",
                                            ["-GameRoot", str(self.service.game), "-ExpectedPid", str(target["pid"]),
                                             "-ExpectedCreationTime", target["creation_time"]],
                                            mutating=True, preserve_failure_receipt=True)
        except DevelopmentError as error:
            raise self._stop_failure(error, target) from None
        self._stop_receipt(receipt, target)
        try:
            remaining = self._snapshot()
        except DevelopmentError as error:
            raise DevelopmentError(error.code, "Selected process termination was reported, but the remaining session could not be observed",
                                   outcome_unknown=True,
                                   receipt={"stop_receipt": copy.deepcopy(receipt),
                                            "observation_error_receipt": copy.deepcopy(error.receipt)}) from None
        if remaining["processes"]:
            raise DevelopmentError("session_cleanup_incomplete", "The selected session terminated but configured game processes remain",
                                   outcome_unknown=False,
                                   receipt={"stop_receipt": copy.deepcopy(receipt), "process_snapshot": remaining})
        return {"state": "stopped", "process": target, "stop_receipt": copy.deepcopy(receipt),
                "remaining_processes": [], "outcome_unknown": False}

    def stop(self, expected_process: dict) -> dict:
        """Terminate only the selected exact identity and its checked game family."""
        with self.service._observation_lock:
            self._check_open()
            return self._stop(expected_process)

    def restart(self, expected_process: dict, *, discovery_timeout_seconds: float = 30.0) -> dict:
        """Confirm the old session ended, launch once, and discover a fresh identity."""
        timeout = self._timeout(discovery_timeout_seconds)
        with self.service._observation_lock:
            self._check_open()
            stopped = self._stop(expected_process)
            try:
                started = self._start(timeout, stopped["process"])
            except DevelopmentError as error:
                raise DevelopmentError(error.code, str(error), outcome_unknown=error.outcome_unknown,
                                       receipt={"previous_process": stopped["process"],
                                                "stop_receipt": stopped["stop_receipt"],
                                                "launch_error_receipt": copy.deepcopy(error.receipt)}) from None
            return {**started, "state": "restarted", "previous_process": stopped["process"],
                    "stop_receipt": stopped["stop_receipt"]}
