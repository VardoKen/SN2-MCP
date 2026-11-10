"""Shared save loading through the existing reflected game interface."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any, Callable

from . import modlist_dialog


class SaveLoadingError(RuntimeError):
    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class SaveLoading:
    def __init__(self, request: Callable[..., dict[str, Any]]):
        self.request = request
        self._lock = threading.Lock()
        self._last: dict[str, Any] | None = None
        self._pending: asyncio.Task | None = None
        self._preparation: asyncio.Task | None = None

    def _objects(self, class_query: str, *, live: bool = True, timeout: float = 15.0) -> list[dict[str, Any]]:
        result = self.request("objects", {"class_query": class_query, "limit": 2147483647}, timeout)
        items = result.get("items", [])
        if live:
            items = [item for item in items if not item.get("name", "").startswith("Default__")
                     and ("/Engine/Transient" in item.get("path", "")
                          or ".PersistentLevel." in item.get("path", ""))]
        return items

    def _one(self, class_query: str) -> dict[str, Any]:
        items = self._objects(class_query)
        if len(items) != 1:
            raise SaveLoadingError("save_widget_unavailable", "One active game widget is required",
                                   {"class": class_query, "matches": items})
        return items[0]

    def _invoke(self, target: dict[str, Any], function: str, arguments: dict[str, Any] | None = None,
                timeout: float = 15.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        functions = self.request("functions", {"handle": target["handle"], "function_query": function,
                                     "limit": 2147483647}, timeout)
        matches = [item for item in functions.get("items", []) if item.get("name") == function]
        if len(matches) != 1:
            raise SaveLoadingError("save_function_unavailable", "The reflected save function is unavailable",
                                   {"function": function, "target": target})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SaveLoadingError("save_invoke_timeout", "Timed out reading the function signature before invocation",
                                   {"function": function, "timeout": timeout})
        return self.request("invoke", {"handle": target["handle"], "function": function,
                                       "arguments": arguments or {},
                                       "expected_signature_token": matches[0]["signature_token"]}, remaining)

    def _properties(self, target: dict[str, Any], names: list[str]) -> dict[str, Any]:
        result = self.request("properties", {"handle": target["handle"], "property_names": names}, 15.0)
        return {item["name"]: item.get("value") for item in result.get("items", [])
                if item.get("supported")}

    def _array(self, target: dict[str, Any], name: str) -> list[Any]:
        result = self.request("read_array", {"handle": target["handle"], "property_name": name,
                                             "offset": 0, "limit": 2147483647}, 15.0)
        return [item.get("value") for item in result.get("items", []) if item.get("supported")]

    def _prepare_page(self, profile_id: int) -> dict[str, Any]:
        main = self._objects("W_MainMenu_C")
        profiles = self._objects("W_ProfileSelectionPage_C")
        if not profiles:
            if not main:
                raise SaveLoadingError("save_menu_unavailable", "The main menu is required to select a profile")
            self._invoke(main[0], "OnLoadClicked")
            profiles = self._objects("W_ProfileSelectionPage_C")
        if len(profiles) != 1:
            raise SaveLoadingError("save_profile_page_unavailable", "The profile selection page was not created")
        result = self._invoke(profiles[0], "OnClicked", {"ProfileSlot": profile_id})
        if result.get("result") is False:
            raise SaveLoadingError("save_profile_unavailable", "The requested game profile could not be selected",
                                   {"profile_id": profile_id})
        pages = self._objects("W_SavedGamesPage_C")
        if not pages:
            classes = self._objects("BlueprintGeneratedClass", live=False)
            page_classes = [item for item in classes if item.get("name") == "W_SavedGamesPage_C"]
            if len(page_classes) != 1:
                raise SaveLoadingError("save_page_class_unavailable", "The saved-games page class is unavailable")
            stack = self._one("W_FrontEndStack_C")
            container = self._properties(stack, ["Stack_FrontEnd"]).get("Stack_FrontEnd")
            if not isinstance(container, dict) or "handle" not in container:
                raise SaveLoadingError("save_stack_unavailable", "The front-end widget stack is unavailable")
            self._invoke(container, "BP_AddWidget", {"ActivatableWidgetClass": {"handle": page_classes[0]["handle"]}})
            pages = self._objects("W_SavedGamesPage_C")
        if len(pages) != 1:
            raise SaveLoadingError("save_page_unavailable", "One saved-games page is required", {"matches": pages})
        self._invoke(pages[0], "UpdateInfo")
        return pages[0]

    def list_saves(self, profile_id: int = 2) -> dict[str, Any]:
        with self._lock:
            return self._list_saves(profile_id)

    def _list_saves(self, profile_id: int) -> dict[str, Any]:
        page = self._prepare_page(profile_id)
        saves: list[dict[str, Any]] = []
        for index, target in enumerate(self._array(page, "m_ArraySlotInfo")):
            if not isinstance(target, dict) or "handle" not in target:
                continue
            values = self._properties(target, ["SlotName", "IsAutosave", "IsQuicksave"])
            if values.get("SlotName"):
                saves.append({"index": index, "slot_name": values["SlotName"],
                              "autosave": values.get("IsAutosave"), "quicksave": values.get("IsQuicksave"),
                              "object": target})
        return {"profile_id": profile_id, "items": saves, "total_count": len(saves), "page": page,
                "profile_selected": True, "save_files_changed": False}

    def status(self) -> dict[str, Any]:
        if self._last is None:
            return {"state": "idle", "load_requested": False}
        result = self._last
        if (result.get("world_loaded") or not (result.get("load_call") or {}).get("executed")
                or not self._lock.acquire(blocking=False)):
            return dict(result)
        try:
            current = self.request("status", {}, 5.0)
            result["native_status"] = current
            if current.get("process_id") != result["process_id"]:
                result["state"] = "loading_process_changed"
                return dict(result)
            movieplayer = self.request("movieplayer_status", {}, 5.0)
            result["movieplayer_status"] = movieplayer
            if movieplayer.get("process_id") != result["process_id"]:
                result["state"] = "loading_process_changed"
                return dict(result)
            if current.get("ready") and not movieplayer.get("active_wait"):
                world_path = (current.get("world", {}).get("current") or {}).get("path")
                for player in self._objects("PlayerCharacterBP_C", timeout=5.0):
                    if world_path and not player.get("path", "").startswith(world_path + "."):
                        continue
                    if self._invoke(player, "BP_IsGameplayReady", timeout=5.0).get("result") is True:
                        result.update(state="loaded", world_loaded=True, player=player)
                        break
        except Exception as error:
            result.setdefault("observation_errors", []).append(self._load_error(error))
        finally:
            self._lock.release()
        return dict(result)

    async def load(self, profile_id: int, slot_name: str, accept_mod_mismatch: bool,
                   timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while not self._lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise SaveLoadingError("save_loading_busy", "Timed out waiting for the current save operation", {"timeout": timeout})
            await asyncio.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        self._pending = None
        self._preparation = None
        try:
            return await self._load(profile_id, slot_name, accept_mod_mismatch, max(0.0, deadline - time.monotonic()))
        finally:
            unfinished = next((task for task in (self._pending, self._preparation)
                               if task is not None and not task.done()), None)
            if unfinished is not None:
                def release_after_completion(task: asyncio.Task) -> None:
                    try:
                        if not task.cancelled():
                            task.exception()
                    finally:
                        self._lock.release()
                unfinished.add_done_callback(release_after_completion)
            else:
                self._lock.release()

    async def _load(self, profile_id: int, slot_name: str, accept_mod_mismatch: bool,
                    timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        self._preparation = asyncio.create_task(asyncio.to_thread(self._list_saves, profile_id))
        catalog = await asyncio.shield(self._preparation)
        items = catalog["items"]
        selected = next((item for item in items if item["slot_name"] == slot_name), None)
        if selected is None:
            raise SaveLoadingError("save_not_found", "The requested save is not present in the selected profile",
                                   {"profile_id": profile_id, "slot_name": slot_name})
        native = await asyncio.to_thread(self.request, "status", {}, 5.0)
        pid = native.get("process_id")
        if not isinstance(pid, int) or pid <= 0:
            raise SaveLoadingError("save_process_unavailable", "The current game process identity is unavailable", {"native_status": native})
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SaveLoadingError("save_preparation_timeout", "Timed out preparing the save load before it was requested", {"timeout": timeout})
        result = {"state": "loading", "profile_id": profile_id, "slot_name": selected["slot_name"],
                  "process_id": pid, "load_requested": True, "world_loaded": False,
                  "warnings": [], "mod_mismatch": None, "load_call": None}
        self._last = result
        pending = asyncio.create_task(asyncio.to_thread(self._invoke, catalog["page"], "StartLoadingSave",
                                                       {"SaveName": selected["slot_name"]}, remaining))
        self._pending = pending
        def completed(task: asyncio.Task) -> None:
            try:
                result["load_call"] = task.result()
            except Exception as error:
                result["load_call"] = self._load_error(error)
                result["state"] = "load_outcome_unknown" if result["load_call"].get("may_have_executed") else "load_failed"
        pending.add_done_callback(completed)
        accepted = False
        finish_attempted: set[str] = set()
        result["movieplayer_finish_requests"] = []
        result["observation_errors"] = []
        try:
            while time.monotonic() < deadline:
                modal = await asyncio.to_thread(modlist_dialog.status, pid)
                if modal.get("active"):
                    result["mod_mismatch"] = modal
                    warning = "The save's mod list differs from the current session; loading may have compatibility issues"
                    if warning not in result["warnings"]:
                        result["warnings"].append(warning)
                    if accept_mod_mismatch and not accepted:
                        result["mod_mismatch_continue"] = await asyncio.to_thread(modlist_dialog.accept_continue, pid)
                        accepted = result["mod_mismatch_continue"].get("status") == "continue_published"
                        if result["mod_mismatch_continue"].get("do_not_retry") and not accepted:
                            result["state"] = "mod_mismatch_outcome_unknown"
                            return result
                    elif not accept_mod_mismatch:
                        result["state"] = "waiting_for_mod_mismatch_choice"
                        return result
                if pending.done() and result["load_call"] is None:
                    try:
                        result["load_call"] = await pending
                    except Exception as error:
                        result["load_call"] = self._load_error(error)
                if isinstance(result["load_call"], dict) and "error" in result["load_call"]:
                    result["state"] = "load_outcome_unknown" if result["load_call"].get("may_have_executed") else "load_failed"
                    return result
                if not modal.get("active") and pending.done():
                    try:
                        current = await asyncio.to_thread(self.request, "status", {}, min(5.0, max(0.001, deadline - time.monotonic())))
                        result["native_status"] = current
                        if current.get("process_id") != pid:
                            result.update(state="loading_process_changed")
                            return result
                        movieplayer = await asyncio.to_thread(self.request, "movieplayer_status", {}, min(5.0, max(0.001, deadline - time.monotonic())))
                        result["movieplayer_status"] = movieplayer
                        if movieplayer.get("process_id") != pid:
                            result.update(state="loading_process_changed")
                            return result
                        if movieplayer.get("active_wait"):
                            wait_id = movieplayer.get("wait_id")
                            if (movieplayer.get("loading_finished") is True
                                    and isinstance(wait_id, str) and wait_id
                                    and wait_id not in finish_attempted):
                                finish_attempted.add(wait_id)
                                finish = {"wait_id": wait_id}
                                result["movieplayer_finish_requests"].append(finish)
                                try:
                                    finish["result"] = await asyncio.to_thread(
                                        self.request, "movieplayer_finish", {"expected_wait_id": wait_id},
                                        min(5.0, max(0.001, deadline - time.monotonic())))
                                except Exception as error:
                                    finish["error"] = self._load_error(error)
                            await asyncio.sleep(0.2)
                            continue
                        if current.get("ready") and time.monotonic() < deadline:
                            players = await asyncio.to_thread(self._objects, "PlayerCharacterBP_C", timeout=min(5.0, max(0.001, deadline - time.monotonic())))
                            world_path = (current.get("world", {}).get("current") or {}).get("path")
                            for player in players:
                                if world_path and not player.get("path", "").startswith(world_path + "."):
                                    continue
                                player_ready = await asyncio.to_thread(self._invoke, player, "BP_IsGameplayReady", timeout=min(5.0, max(0.001, deadline - time.monotonic())))
                                if player_ready.get("result") is True:
                                    if time.monotonic() >= deadline:
                                        result.update(state="loading_timeout")
                                    else:
                                        result.update(state="loaded", world_loaded=True, player=player)
                                    return result
                    except Exception as error:
                        observation_error = self._load_error(error)
                        if observation_error["code"] not in {"native_timeout", "game_not_pumping", "native_not_ready", "world_changed", "script_reloaded", "stale_handle", "save_invoke_timeout"}:
                            raise
                        result["observation_errors"].append(observation_error)
                await asyncio.sleep(0.2)
            result["state"] = "loading_timeout"
            return result
        except Exception as error:
            observation_error = self._load_error(error)
            result.update(state="loading_process_unavailable" if observation_error["code"] == "open_process_failed" else "loading_interrupted",
                          error={"message": observation_error["error"], "code": observation_error["code"],
                                 "details": observation_error.get("details", getattr(error, "details", {}))})
            return result
        finally:
            if pending.done():
                try:
                    pending.result()
                except Exception:
                    pass

    @staticmethod
    def _load_error(error: Exception) -> dict[str, Any]:
        message = (f"Save loading file operation failed (errno={error.errno}, winerror={getattr(error, 'winerror', None)})"
                   if isinstance(error, OSError) else str(error))
        result: dict[str, Any] = {"error": message, "code": getattr(error, "code", "save_load_failed"),
                                  "may_have_executed": bool(getattr(error, "may_have_executed", False))}
        try:
            details = json.loads(str(error))
        except (ValueError, TypeError):
            details = None
        if isinstance(details, dict):
            result.update(code=details.get("code", result["code"]),
                          may_have_executed=bool(details.get("may_have_executed", result["may_have_executed"])),
                          details=details)
        return result

