"""Access the cooked in-game development service through the existing native bridge.

This module neither builds the service nor substitutes Editor operations for its
game functions. Package revision strings are activation correlation labels;
installation and package-byte verification remain separate operations.
"""
from __future__ import annotations

from typing import Any, Callable

SERVICE_ID = "SN2MCPDevelopmentServices"
SERVICE_CLASS = "BP_DevelopmentServices_C"
SERVICE_CLASS_PATH = (
    "/SN2MCPDevelopmentServices/BP_DevelopmentServices.BP_DevelopmentServices_C"
)

Request = Callable[[str, dict[str, Any], float | None, dict[str, Any] | None], dict[str, Any]]


class ServiceModError(Exception):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = {"executed": False, "may_have_executed": False,
                        "process_event_returned": False, **(details or {})}


class ServiceMod:
    """Resolve the live service actor on each request, including after reconnect."""

    def __init__(self, request: Request):
        self.request = request

    def _status(self, timeout: float | None, target: dict[str, Any] | None) -> dict[str, Any]:
        return self.request("blueprint_mod_status", {"mod_id": SERVICE_ID}, timeout, target)

    @staticmethod
    def _actor(blueprint: dict[str, Any]) -> dict[str, Any] | None:
        actor = blueprint.get("actor")
        descriptor = actor.get("object") if isinstance(actor, dict) else None
        observation = blueprint.get("actor_observation")
        if (blueprint.get("state") != "actor_spawned"
                or not isinstance(observation, dict) or observation.get("state") != "alive"
                or not isinstance(actor, dict) or actor.get("entry_class_path") != SERVICE_CLASS_PATH
                or not isinstance(descriptor, dict) or descriptor.get("class") != SERVICE_CLASS
                or not isinstance(descriptor.get("handle"), str)):
            return None
        return descriptor

    def _functions(self, handle: str, timeout: float | None,
                   target: dict[str, Any] | None, query: str = "") -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        pages: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = self.request("functions", {
                "handle": handle, "limit": 64, "offset": offset,
                "function_query": query,
            }, timeout, target)
            pages.append({key: value for key, value in page.items() if key != "items"})
            items.extend(item for item in page.get("items", [])
                         if (isinstance(item, dict) and item.get("owner") == SERVICE_CLASS
                             and type(item.get("flags")) is int
                             and item["flags"] & 0x04000000))
            if page.get("scan_complete") is True:
                return {"items": items, "scan_complete": True, "pages": pages}
            next_offset = page.get("next_offset")
            if (page.get("scan_truncated") or type(next_offset) is not int
                    or next_offset <= offset):
                return {"items": items, "scan_complete": False, "pages": pages}
            offset = next_offset

    def status(self, timeout: float | None = None,
               target: dict[str, Any] | None = None) -> dict[str, Any]:
        blueprint = self._status(timeout, target)
        actor = self._actor(blueprint)
        functions = (self._functions(actor["handle"], timeout, target) if actor is not None
                     else {"items": [], "scan_complete": False, "pages": []})
        return {"service_id": SERVICE_ID, "entry_class_path": SERVICE_CLASS_PATH,
                "blueprint": blueprint, "actor": actor, "functions": functions,
                "available": actor is not None,
                "availability_evidence": "live_actor_identity_and_reflected_own_functions",
                "function_behavior_checked": False, "package_bytes_verified": False}

    def enable(self, package_revision: str, source_revision: str | None = None,
               timeout: float | None = None,
               target: dict[str, Any] | None = None) -> dict[str, Any]:
        if (not isinstance(package_revision, str) or not package_revision
                or "\0" in package_revision):
            raise ServiceModError("invalid_package_revision", "A nonempty package revision label is required")
        if source_revision is not None and (not isinstance(source_revision, str)
                or not source_revision or "\0" in source_revision):
            raise ServiceModError("invalid_source_revision", "A supplied source revision must be nonempty")
        params = {"mod_id": SERVICE_ID, "entry_class_path": SERVICE_CLASS_PATH,
                  "package_revision": package_revision}
        if source_revision is not None:
            params["source_revision"] = source_revision
        blueprint = self.request("blueprint_mod_enable", params, timeout, target)
        return {"service_id": SERVICE_ID, "entry_class_path": SERVICE_CLASS_PATH,
                "blueprint": blueprint, "package_revision_role": "caller_correlation_label",
                "package_bytes_verified": False, "installation_performed": False}

    def disable(self, timeout: float | None = None,
                target: dict[str, Any] | None = None) -> dict[str, Any]:
        blueprint = self._status(timeout, target)
        if blueprint.get("state") != "not_tracked":
            actor = blueprint.get("actor")
            actual_path = actor.get("entry_class_path") if isinstance(actor, dict) else None
            if (blueprint.get("entry_class_path") != SERVICE_CLASS_PATH
                    or (actor is not None and actual_path != SERVICE_CLASS_PATH)):
                raise ServiceModError("service_actor_identity_mismatch",
                                      "The tracked mod does not identify the development service class",
                                      {"blueprint": blueprint})
        return {"service_id": SERVICE_ID,
                "blueprint": self.request("blueprint_mod_disable", {"mod_id": SERVICE_ID}, timeout, target)}

    def call(self, function: str, arguments: dict[str, Any] | None = None,
             timeout: float | None = None,
             target: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(function, str) or not function or "\0" in function:
            raise ServiceModError("invalid_service_function", "Use an exact reflected service function name")
        if arguments is not None and not isinstance(arguments, dict):
            raise ServiceModError("invalid_service_arguments", "Service arguments must be an object")
        blueprint = self._status(timeout, target)
        actor = self._actor(blueprint)
        if actor is None:
            raise ServiceModError("service_actor_unavailable", "The service has no confirmed live actor",
                                  {"blueprint": blueprint, "executed": False})
        functions = self._functions(actor["handle"], timeout, target, function)
        if not functions["scan_complete"]:
            raise ServiceModError("service_function_scan_incomplete", "The reflected service function scan is incomplete",
                                  {"functions": functions, "executed": False})
        matches = [item for item in functions["items"] if item.get("name") == function]
        if len(matches) != 1:
            raise ServiceModError("service_function_unavailable", "The exact own service function was not found",
                                  {"function": function, "functions": functions, "executed": False})
        metadata = matches[0]
        params = {"handle": actor["handle"], "function": function, "arguments": arguments or {}}
        token = metadata.get("signature_token")
        if isinstance(token, str) and token:
            params["expected_signature_token"] = token
        result = self.request("invoke", params, timeout, target)
        return {"service_id": SERVICE_ID, "actor": actor, "function": metadata,
                "invocation": result, "automatic_retry": False}
