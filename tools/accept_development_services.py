"""Exercise the cooked in-game companion through registered MCP tools.

This check installs its identified synthetic service package, launches the selected
game, loads the bridge, exercises real Shipping functions, then stops the selected
process and removes only its identified installation. It does not run Editor,
create a gameplay session, or request edits to existing saves or configuration.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import sys
import sysconfig
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "SN2MCPDevelopmentServices"
FUNCTIONS = {
    "GetServiceVersion", "ReadWidgetText", "WriteWidgetText",
    "SetObjectTextProperty", "TraceImpactPoint", "CreateHeldObject",
    "GetHeldObjects", "ReleaseHeldObject", "ClearHeldObjects",
}
OUT = {"out": True}


class AcceptanceError(RuntimeError):
    pass


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while fragment := stream.read(1024 * 1024):
            digest.update(fragment)
    return digest.hexdigest()


def object_arg(value: dict) -> dict:
    if not isinstance(value, dict) or not isinstance(value.get("handle"), str):
        raise AcceptanceError("A current runtime object descriptor is required")
    return {"handle": value["handle"]}


def identity(value: dict) -> tuple:
    return tuple(value.get(key) for key in ("index", "path", "class"))


def array_items(value: Any) -> list[dict]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict) and value.get("partial") is False:
        items = value.get("items")
        if value.get("total_count") != len(items or []):
            raise AcceptanceError("The service object array is incomplete")
    else:
        raise AcceptanceError("The service did not return a complete object array")
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise AcceptanceError("Invalid service object array descriptors")
    descriptors = []
    for item in items:
        descriptor = item.get("value") if "value" in item else item
        if (item.get("supported", True) is not True or not isinstance(descriptor, dict)
                or not isinstance(descriptor.get("handle"), str)):
            raise AcceptanceError("The service array has no complete current object descriptor")
        descriptors.append(descriptor)
    return descriptors


def outputs(result: dict) -> dict:
    invocation = result.get("invocation")
    if (not isinstance(invocation, dict) or invocation.get("executed") is not True
            or invocation.get("process_event_returned") is not True):
        raise AcceptanceError("The service call has no confirmed returned ProcessEvent")
    return invocation.get("out_parameters", {})


def error_payload(texts: list[str]) -> dict:
    for text in texts:
        try:
            value = json.loads(text[text.index("{"):])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


async def run(args: argparse.Namespace) -> int:
    repository = args.repository_root.resolve(strict=True)
    workspace = args.workspace_root.resolve(strict=True)
    game = args.game_root.resolve(strict=True)
    modkit = (args.modkit_root or workspace / "Modkit").absolute()
    engine = (args.engine_root or workspace / "Engine").absolute()
    if args.require_no_authoring_environment and (modkit.exists() or engine.exists()):
        raise AcceptanceError("The consumer check requires absent configured Modkit and Engine paths")
    output = args.output_root.absolute()
    output.mkdir(parents=True, exist_ok=False)
    if not args.use_installed_package:
        sys.path.insert(0, str(repository / "src"))
    from mcp.server.mcpserver.exceptions import ToolError
    import sn2_mcp
    import sn2_mcp.server as server_module
    from sn2_mcp.server import create_server
    module_file = Path(sn2_mcp.__file__).resolve(strict=True)
    server_file = Path(server_module.__file__).resolve(strict=True)
    installed_roots = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
    if args.use_installed_package and not all(
            any(path.is_relative_to(root) for root in installed_roots)
            for path in (module_file, server_file)):
        raise AcceptanceError("The consumer frontend must be imported from installed site-packages")
    __version__ = sn2_mcp.__version__
    distribution_version = importlib.metadata.version("sn2-mcp")
    if args.use_installed_package and distribution_version != __version__:
        raise AcceptanceError("The installed wheel metadata and imported frontend versions differ")

    package_path = Path(args.package_path)
    if package_path.is_absolute() or ".." in package_path.parts:
        raise AcceptanceError("The package path must be relative to the selected workspace")
    package_root = (workspace / package_path).resolve(strict=True)
    if not package_root.is_relative_to(workspace):
        raise AcceptanceError("The selected package escapes the workspace")
    bridge = repository / "build/native/Release/SN2_MCP.dll"
    bridge_hash = sha256(bridge)
    server = create_server(workspace, runtime_root=repository, game_root=game,
                           modkit_root=modkit, engine_root=engine)
    sequence = 0
    summary: dict[str, Any] = {
        "kind": "sn2_development_services_acceptance", "version": __version__,
        "repository_root": str(repository), "workspace_root": str(workspace),
        "game_root": str(game), "package_path": args.package_path,
        "bridge_path": str(bridge), "bridge_sha256": bridge_hash,
        "authoring_environment": {"modkit_root": str(modkit), "modkit_exists": modkit.exists(),
            "engine_root": str(engine), "engine_exists": engine.exists(),
            "absence_required": args.require_no_authoring_environment,
            "authoring_tools_called": False,
            "runtime_python_origin": str(module_file),
            "server_module_origin": str(server_file),
            "package_import_mode": "installed_site_packages" if args.use_installed_package else "repository_sources",
            "distribution_version": distribution_version,
            "runtime_dependency_route": "installed_wheel_and_supplied_runtime_bundle" if args.use_installed_package else "repository_sources_and_runtime_artifacts",
            "repository_src_inserted_into_import_path": not args.use_installed_package,
            "frontend_imported_from_repository_src": module_file.is_relative_to(repository / "src"),
            "server_imported_from_repository_src": server_file.is_relative_to(repository / "src"),
            "supplied_python_sources_present": (repository / "src").exists(),
            "supplied_native_sources_present": (repository / "native/src").exists(),
            "source_build_requested": False,
            "source_presence_limit": "This check permits source files in the supplied bundle; it does not require their physical absence"},
        "state": "running", "editor_invoked": False, "gameplay_session_created": False,
        "existing_save_operations_requested": False, "checks": {}, "cleanup": {},
    }
    selected: dict | None = None
    installation_requested = False
    launch_requested = False
    package: dict | None = None
    primary_error: BaseException | None = None
    collision: dict | None = None
    collision_destroy_attempted = False
    clear_held_attempted = False
    service_actor: dict | None = None
    watches: list[dict] = []
    endplay_control: dict | None = None

    async def call(name: str, arguments: dict | None = None,
                   *, expected_error: bool = False, allow_error: bool = False,
                   cleanup: bool = False) -> dict:
        nonlocal sequence
        sequence += 1
        started = time.monotonic()
        values = arguments or {}
        try:
            response = await server.call_tool(name, values)
            texts = [item.text for item in response.content if item.type == "text"]
            is_error = response.is_error
            value = response.structured_content
            if value is None:
                value = error_payload(texts)
        except ToolError as error:
            texts, is_error = [str(error)], True
            value = error_payload(texts)
        record = {"tool": name, "arguments": values, "is_error": is_error,
                  "result": value, "text": texts, "cleanup": cleanup,
                  "elapsed_seconds": time.monotonic() - started}
        write_json(output / f"mcp-{sequence:04d}.json", record)
        if not isinstance(value, dict):
            raise AcceptanceError(f"MCP call {sequence} did not return a structured object")
        if expected_error:
            if not is_error:
                raise AcceptanceError(f"MCP call {sequence} unexpectedly executed the unsupported path")
            return value
        if is_error and not allow_error:
            raise AcceptanceError(f"MCP call {sequence} failed: {value.get('code', 'inspect_receipt')}")
        return value

    async def exact_object(path: str, class_query: str = "") -> dict:
        result = await call("sn2_find_objects", {"exact_path": path, "class_query": class_query,
                                                "limit": 8, "timeout_seconds": 30.0})
        items = [item for item in result.get("items", []) if item.get("path") == path]
        if len(items) != 1 or not result.get("scan_complete"):
            raise AcceptanceError(f"Exact runtime object is not unique: {path}")
        return items[0]

    async def function(object_: dict, name: str, *, complete: bool = True) -> dict:
        result = await call("sn2_object_functions", {"handle": object_["handle"],
            "function_query": name, "limit": 64, "timeout_seconds": 30.0})
        matches = [item for item in result.get("items", []) if item.get("name") == name]
        if len(matches) != 1 or not result.get("scan_complete"):
            raise AcceptanceError(f"Exact reflected function is not unique: {name}")
        if complete and matches[0].get("signature_status") != "complete":
            raise AcceptanceError(f"The actual {name} signature is unsupported")
        return matches[0]

    async def invoke(object_: dict, name: str, arguments: dict | None = None) -> dict:
        metadata = await function(object_, name)
        result = await call("sn2_invoke", {"handle": object_["handle"], "function": name,
            "arguments": arguments or {}, "expected_signature_token": metadata["signature_token"],
            "timeout_seconds": 30.0})
        if result.get("executed") is not True or result.get("process_event_returned") is not True:
            raise AcceptanceError(f"No confirmed returned native call: {name}")
        return result

    async def service(name: str, arguments: dict | None = None) -> dict:
        return await call("sn2_service_call", {"function": name, "arguments": arguments or {},
            "target_process": selected, "timeout_seconds": 30.0})

    async def held() -> list[dict]:
        return array_items(outputs(await service("GetHeldObjects", {"Objects": OUT})).get("Objects"))

    async def rejected_service(name: str, arguments: dict, expected_code: str) -> dict:
        before = await held()
        result = await call("sn2_service_call", {"function": name, "arguments": arguments,
            "target_process": selected, "timeout_seconds": 30.0}, expected_error=True)
        effects = result.get("effect", result.get("details", {}))
        if (result.get("code") != expected_code or result.get("may_have_executed") is not False
                or effects.get("executed") is not False or effects.get("may_have_executed") is not False
                or effects.get("process_event_returned") is not False):
            raise AcceptanceError(f"The negative {name} case did not reject before ProcessEvent: {expected_code}")
        after = await held()
        if [identity(item) for item in after] != [identity(item) for item in before]:
            raise AcceptanceError(f"The rejected {name} case changed the held object inventory")
        return {"rejection": result, "held_before": before, "held_after": after}

    def enum_value(metadata: dict, parameter: str, member: str) -> int:
        fields = [item for item in metadata.get("parameters", []) if item.get("name") == parameter]
        if len(fields) != 1:
            raise AcceptanceError(f"The actual enum parameter was not found: {parameter}")
        entries = fields[0].get("enum", {}).get("entries", [])
        matches = [item for item in entries if str(item.get("name", "")).split("::")[-1] == member]
        if len(matches) != 1 or type(matches[0].get("value")) is not int:
            raise AcceptanceError(f"The actual enum member was not uniquely reflected: {member}")
        return matches[0]["value"]

    def snapshot_property(snapshot: dict, handle: str, property_name: str) -> Any:
        objects = [item for item in snapshot.get("objects", []) if item.get("handle") == handle]
        if len(objects) != 1 or snapshot.get("partial") or snapshot.get("truncated"):
            raise AcceptanceError("The prototype snapshot did not fully cover its selected object")
        properties = [item for item in objects[0].get("properties", [])
                      if item.get("name") == property_name and item.get("supported") is True]
        if len(properties) != 1:
            raise AcceptanceError("The prototype snapshot did not include its supported property")
        return properties[0].get("value")

    async def stop_watch(watch: dict, *, cleanup: bool = False) -> dict:
        stopped = await call("sn2_watch_stop", {"watch_id": watch["watch_id"],
            "expected_session_id": watch["session_id"], "timeout_seconds": 30.0}, cleanup=cleanup)
        events = await call("sn2_watch_read", {"watch_id": watch["watch_id"],
            "expected_session_id": watch["session_id"], "timeout_seconds": 30.0}, cleanup=cleanup)
        forgotten = await call("sn2_watch_forget", {"watch_id": watch["watch_id"],
            "expected_session_id": watch["session_id"], "timeout_seconds": 30.0}, cleanup=cleanup)
        if (stopped.get("cleanup_complete") is not True or forgotten.get("forgotten") is not True
                or stopped.get("state") != "stopped" or stopped.get("reason") != "explicit_stop"
                or not any(item.get("kind") == "terminal" and item.get("state") == "stopped"
                           and item.get("reason") == "explicit_stop" for item in events.get("events", []))):
            raise AcceptanceError("The prototype watch did not stop and release its observation")
        watches.remove(watch)
        return {"stop": stopped, "events": events, "forget": forgotten}

    async def await_absent(object_: dict, properties: list[str], *, cleanup: bool = False) -> dict:
        """Observe invalidation after one request without repeating its effect."""
        deadline = time.monotonic() + 30.0
        while True:
            rejected = await call("sn2_object_properties", {"handle": object_["handle"],
                "property_names": properties, "timeout_seconds": 30.0},
                allow_error=True, cleanup=cleanup)
            code = rejected.get("code")
            if code is not None and code not in {"stale_handle", "object_destroying"}:
                raise AcceptanceError("The object invalidation check failed outside expected lifecycle states")
            absence = await call("sn2_find_objects", {"exact_path": object_["path"],
                "limit": 8, "timeout_seconds": 30.0}, cleanup=cleanup)
            if (code in {"stale_handle", "object_destroying"}
                    and absence.get("scan_complete") is True and not absence.get("items")):
                return {"object": object_, "invalidated_handle": rejected,
                        "registry_absence": absence,
                        "absence_scope": "complete scan of inspectable exact-path objects"}
            if time.monotonic() >= deadline:
                raise AcceptanceError("The requested lifecycle did not invalidate the selected object identity")
            await asyncio.sleep(0.1)

    async with AsyncExitStack() as server_lifetimes:
        await server_lifetimes.enter_async_context(server._lowlevel_server.lifespan(server._lowlevel_server))
        try:
            local = await call("sn2_status")
            tools = await server.list_tools()
            names = {item.name for item in tools}
            if not {"sn2_service_status", "sn2_service_enable", "sn2_service_disable", "sn2_service_call"}.issubset(names):
                raise AcceptanceError("The runtime companion tools are not registered")
            summary["checks"]["registration"] = {"tool_count": len(names), "frontend": local}
            package = await call("sn2_verify_artifact_package", {"package_path": args.package_path})
            if package.get("plugin_id") != SERVICE or package.get("valid") is not True:
                raise AcceptanceError("The package is not the verified development companion")
            summary["package"] = package
            before_processes = await call("sn2_target_processes")
            if before_processes.get("running") is not False or before_processes.get("processes") != []:
                raise AcceptanceError("The isolated acceptance requires a stopped selected game")
            before_install = await call("sn2_mod_deployment_status", {"plugin_id": SERVICE})
            if before_install.get("installed_files"):
                raise AcceptanceError("The companion already has an installation; preserve it and inspect separately")
            installation_requested = True
            installed = await call("sn2_mod_install", {"package_path": args.package_path,
                                                       "expected_manifest_sha256": package["manifest_sha256"]})
            summary["installation"] = installed
            launch_requested = True
            started = await call("sn2_development_start", {"discovery_timeout_seconds": 60.0})
            selected = started["process"]
            summary["process"] = selected
            summary["start"] = started
            summary["load"] = await call("sn2_native_load", {"target_process": selected,
                "dll_path": str(bridge), "expected_dll_sha256": bridge_hash})
            deadline = time.monotonic() + args.readiness_timeout_seconds
            while True:
                state = await call("sn2_native_status", {"target_process": selected}, allow_error=True)
                if state.get("session_id"):
                    break
                if state.get("code") not in {"native_unavailable", "native_busy", "native_timeout"}:
                    raise AcceptanceError("The native handshake failed outside transport-startup readiness")
                if time.monotonic() >= deadline:
                    raise AcceptanceError("The selected native handshake was not observed")
                await asyncio.sleep(0.5)
            enabled = await call("sn2_native_enable", {"target_process": selected,
                "expected_native_session_id": state["session_id"]})
            while True:
                state = await call("sn2_native_status", {"target_process": selected})
                world = state.get("world", {})
                if state.get("ready") and world.get("state") == "observed":
                    break
                if time.monotonic() >= deadline:
                    raise AcceptanceError("Tick/reflection and a selected world were not ready")
                await asyncio.sleep(0.5)
            summary["native"] = {"enabled": enabled, "ready": state}
            world_path = world.get("current", {}).get("path")
            if world_path != "/Game/Maps/L_ClientLobby.L_ClientLobby":
                raise AcceptanceError("The selected world is not the known isolated client lobby")
            world_object = await exact_object(world_path, "World")
            gameplay = await exact_object("/Script/Engine.Default__GameplayStatics")
            game_state = (await invoke(gameplay, "GetGameState", {
                "WorldContextObject": object_arg(world_object)})).get("result")
            if not isinstance(game_state, dict):
                raise AcceptanceError("The lobby GameState is unavailable")
            while (await invoke(game_state, "HasBegunPlay")).get("result") is not True:
                if time.monotonic() >= deadline:
                    raise AcceptanceError("The lobby did not establish BeginPlay")
                await asyncio.sleep(0.5)
            # Exact APIs are declared by Engine PluginBlueprintLibrary.h and
            # read IPluginManager / FPackageName mount state at runtime.
            plugin_library = await exact_object("/Script/Engine.Default__PluginBlueprintLibrary")
            enabled_plugins = await invoke(plugin_library, "GetEnabledPluginNames")
            plugin_array = enabled_plugins.get("result")
            if (not isinstance(plugin_array, dict) or plugin_array.get("partial") is not False
                    or not isinstance(plugin_array.get("items"), list)
                    or plugin_array.get("total_count") != len(plugin_array["items"])
                    or any(item.get("supported") is not True or not isinstance(item.get("value"), str)
                           for item in plugin_array["items"] if isinstance(item, dict))
                    or any(not isinstance(item, dict) for item in plugin_array["items"])):
                raise AcceptanceError("The runtime enabled-plugin name array was incomplete")
            if [item["value"] for item in plugin_array["items"]].count(SERVICE) != 1:
                raise AcceptanceError("The supplied development plugin is not uniquely enabled in the game")
            mount = await invoke(plugin_library, "IsPluginMounted", {"PluginName": SERVICE})
            base = await invoke(plugin_library, "GetPluginBaseDir", {"PluginName": SERVICE, "OutBaseDir": OUT})
            asset_root = await invoke(plugin_library, "GetPluginMountedAssetPath", {
                "PluginName": SERVICE, "OutAssetPath": OUT})
            content = await invoke(plugin_library, "GetPluginContentDir", {
                "PluginName": SERVICE, "OutContentDir": OUT})
            expected_base = game / "Subnautica2/Mods" / SERVICE
            working_base = game / "Subnautica2/Binaries/Win64"
            reported_base = base.get("out_parameters", {}).get("OutBaseDir")
            reported_content = content.get("out_parameters", {}).get("OutContentDir")
            if (any(item.get("result") is not True for item in (mount, base, asset_root, content))
                    or not isinstance(reported_base, str) or not isinstance(reported_content, str)
                    or (working_base / reported_base).resolve() != expected_base.resolve()
                    or (working_base / reported_content).resolve() != (expected_base / "Content").resolve()
                    or asset_root.get("out_parameters", {}).get("OutAssetPath") != f"/{SERVICE}/"
                    or enabled_plugins.get("cleanup_confirmed") is not True
                    or enabled_plugins.get("return_cleanup_confirmed") is not True
                    or any(item.get("cleanup_confirmed") is not True
                           or item.get("input_cleanup_confirmed") is not True
                           for item in (mount, base, asset_root, content))):
                raise AcceptanceError("The supplied plugin discovery/mount paths were not confirmed by Shipping")
            summary["checks"]["shipping_plugin_discovery_and_mount"] = {
                "library": plugin_library, "enabled_plugins": enabled_plugins,
                "mounted": mount, "base": base, "asset_root": asset_root, "content": content,
                "expected_installation_base": str(expected_base),
                "relative_path_resolution_base": str(working_base),
                "evidence_scope": "enabled plugin identity and asset mount point; service behavior is checked separately"}
            summary["service_enable"] = await call("sn2_service_enable", {
                "package_revision": package["manifest_sha256"], "source_revision": package["source_revision"],
                "target_process": selected, "timeout_seconds": 30.0})
            while True:
                state = await call("sn2_service_status", {"target_process": selected, "timeout_seconds": 30.0})
                if state.get("available"):
                    break
                if time.monotonic() >= deadline:
                    raise AcceptanceError("The supplied cooked companion actor did not become active")
                await asyncio.sleep(0.1)
            catalog = state.get("functions", {})
            actual = {item.get("name") for item in catalog.get("items", [])}
            if not state.get("available") or not catalog.get("scan_complete") or not FUNCTIONS.issubset(actual):
                raise AcceptanceError("The live cooked companion does not expose its complete intended API")
            unsupported = [item["name"] for item in catalog["items"]
                           if item["name"] in FUNCTIONS and item.get("signature_status") != "complete"]
            if unsupported:
                raise AcceptanceError("The companion still has unsupported public signatures: " + ", ".join(unsupported))
            summary["checks"]["live_service"] = state
            service_actor = state["actor"]
            version = outputs(await service("GetServiceVersion", {"Version": OUT})).get("Version")
            if version != __version__:
                raise AcceptanceError("The compiled companion is not correlated with the selected bridge revision")
            summary["checks"]["service_version"] = version

            negative_creation = {
                "null_class": await rejected_service("CreateHeldObject", {
                    "ObjectClass": None, "Index": OUT}, "service_class_required"),
                "null_widget": await rejected_service("ReadWidgetText", {
                    "Widget": None, "Text": OUT}, "service_object_required"),
            }
            actor_class = await exact_object("/Script/Engine.Actor", "Class")
            negative_creation["actor_lifecycle"] = await rejected_service("CreateHeldObject", {
                "ObjectClass": object_arg(actor_class), "Index": OUT}, "prototype_lifecycle_required")
            abstract_widget = await exact_object("/Script/UMG.Widget", "Class")
            # Native guard correlates the actual UClass flags with the exact game
            # construction-code checkpoints before reporting abstract_class.
            negative_creation["abstract_class"] = await rejected_service("CreateHeldObject", {
                "ObjectClass": object_arg(abstract_widget), "Index": OUT}, "abstract_class")
            summary["checks"]["invalid_creation_and_widget"] = negative_creation

            text_class = await exact_object("/Script/UMG.TextBlock", "Class")
            created = outputs(await service("CreateHeldObject", {"ObjectClass": object_arg(text_class), "Index": OUT}))
            objects = await held()
            if len(objects) != 1 or created.get("Index") != 0 or objects[0].get("class") != "TextBlock":
                raise AcceptanceError("The service did not create and strongly retain a TextBlock prototype")
            widget = objects[0]
            text = "SN2 MCP runtime mod prototype — text service"
            await service("WriteWidgetText", {"Widget": object_arg(widget), "Text": text})
            read = outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT}))
            if read.get("Text") != text:
                raise AcceptanceError("The in-game text wrapper did not preserve the synthetic widget text")
            changed = "SN2 MCP property edit through cooked service"
            await service("SetObjectTextProperty", {"Target": object_arg(widget),
                "PropertyName": {"name": "Text"}, "Text": changed})
            read_changed = outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT}))
            if read_changed.get("Text") != changed:
                raise AcceptanceError("The in-game FText property setter did not change the selected prototype")
            negative_properties = {
                "missing": await rejected_service("SetObjectTextProperty", {
                    "Target": object_arg(widget), "PropertyName": {"name": "SN2MCPMissingTextProperty"},
                    "Text": "This rejected edit must not appear"}, "property_not_found"),
                "non_text": await rejected_service("SetObjectTextProperty", {
                    "Target": object_arg(widget), "PropertyName": {"name": "bIsVariable"},
                    "Text": "This rejected edit must not appear"}, "service_text_property_required"),
            }
            unchanged = outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT}))
            if unchanged.get("Text") != changed:
                raise AcceptanceError("A rejected text-property edit changed the working prototype")
            summary["checks"]["invalid_text_property"] = {"cases": negative_properties,
                                                          "unchanged_text": unchanged}
            raw = await function(widget, "GetText", complete=False)
            if raw.get("signature_status") == "complete" or not any(
                    item.get("type") == "TextProperty" for item in raw.get("parameters", [])):
                raise AcceptanceError("The expected direct FText marshalling gap was not established")
            rejected = await call("sn2_invoke", {"handle": widget["handle"], "function": "GetText",
                                                "arguments": {}, "timeout_seconds": 30.0}, expected_error=True)
            effects = rejected.get("effect", {})
            if rejected.get("may_have_executed") is not False or effects.get("executed") is not False:
                raise AcceptanceError("The raw text rejection did not prove absence of ProcessEvent execution")
            properties = await call("sn2_object_properties", {"handle": widget["handle"],
                "property_names": ["Text"], "timeout_seconds": 30.0})
            summary["checks"]["text_prototype"] = {"created": widget, "write_read": read,
                "property_write_read": read_changed, "direct_signature": raw,
                "direct_rejection": rejected, "property_read": properties}

            kismet = await exact_object("/Script/Engine.Default__KismetSystemLibrary")
            raw_trace = await function(kismet, "LineTraceSingle", complete=False)
            if raw_trace.get("signature_status") == "complete":
                raise AcceptanceError("The expected direct managed HitResult gap was not established")
            trace_signature = next(item for item in catalog["items"] if item["name"] == "TraceImpactPoint")
            # Select every collision constant from the game's actual reflected enum table.
            channel = enum_value(trace_signature, "Channel", "TraceTypeQuery1")
            trace = outputs(await service("TraceImpactPoint", {
                "Start": {"X": 1000000.0, "Y": 1000000.0, "Z": 1000000.0},
                "End": {"X": 1000010.0, "Y": 1000000.0, "Z": 1000000.0},
                "Channel": channel, "TraceComplex": False,
                "ActorsToIgnore": [], "IgnoreSelf": True, "Hit": OUT, "ImpactPoint": OUT}))
            impact = trace.get("ImpactPoint")
            if (trace.get("Hit") is not False or not isinstance(impact, dict)
                    or set(impact) != {"X", "Y", "Z"}
                    or any(type(value) not in (int, float) or not math.isfinite(value) for value in impact.values())):
                raise AcceptanceError("The no-hit trace did not return a false hit and finite impact data")
            summary["checks"]["trace_no_hit"] = {"direct_signature": raw_trace, "wrapper": trace,
                "channel": channel, "hit_scenario_checked": False}

            box_class = await exact_object("/Script/Engine.BoxComponent", "Class")
            neutral = {"Rotation": {"X": 0, "Y": 0, "Z": 0, "W": 1},
                       "Translation": {"X": 0, "Y": 0, "Z": 0},
                       "Scale3D": {"X": 1, "Y": 1, "Z": 1}}
            collision = (await invoke(service_actor, "AddComponentByClass", {
                "Class": object_arg(box_class), "bManualAttachment": False,
                "RelativeTransform": neutral, "bDeferredFinish": False})).get("result")
            if not isinstance(collision, dict) or collision.get("class") != "BoxComponent":
                raise AcceptanceError("The synthetic collision component was not created")
            await invoke(collision, "SetBoxExtent", {"InBoxExtent": {"X": 100, "Y": 100, "Z": 100},
                                                    "bUpdateOverlaps": True})
            enabled_signature = await function(collision, "SetCollisionEnabled")
            response_signature = await function(collision, "SetCollisionResponseToAllChannels")
            await invoke(collision, "SetCollisionEnabled", {
                "NewType": enum_value(enabled_signature, "NewType", "QueryOnly")})
            await invoke(collision, "SetCollisionResponseToAllChannels", {
                "NewResponse": enum_value(response_signature, "NewResponse", "ECR_Block")})
            hit = outputs(await service("TraceImpactPoint", {
                "Start": {"X": -1000, "Y": 0, "Z": 0}, "End": {"X": 1000, "Y": 0, "Z": 0},
                "Channel": channel, "TraceComplex": False, "ActorsToIgnore": [],
                "IgnoreSelf": False, "Hit": OUT, "ImpactPoint": OUT}))
            point = hit.get("ImpactPoint")
            if (hit.get("Hit") is not True or not isinstance(point, dict)
                    or set(point) != {"X", "Y", "Z"}
                    or abs(point["X"] + 100) > 0.1 or abs(point["Y"]) > 0.1 or abs(point["Z"]) > 0.1):
                raise AcceptanceError("The trace did not independently hit the synthetic box at its known face")
            summary["checks"]["trace_hit"] = {"component": collision, "extent": 100,
                "wrapper": hit, "expected_impact": {"X": -100, "Y": 0, "Z": 0}}

            # One interaction prototype connects the traced geometry to its
            # synthetic widget, observed scalar state, and game-side text API.
            baseline_opacity = (await invoke(widget, "GetRenderOpacity")).get("result")
            if type(baseline_opacity) not in (int, float) or not math.isfinite(baseline_opacity):
                raise AcceptanceError("The prototype widget has no finite reflected opacity")
            before_snapshot = await call("sn2_snapshot", {"targets": [{"handle": widget["handle"],
                "properties": ["RenderOpacity"]}], "timeout_seconds": 30.0})
            if snapshot_property(before_snapshot, widget["handle"], "RenderOpacity") != baseline_opacity:
                raise AcceptanceError("The independent getter and baseline prototype snapshot differ")
            watch = await call("sn2_watch_start", {"handle": widget["handle"],
                "properties": ["RenderOpacity"], "duration_ms": 120000, "interval_ms": 50,
                "capacity": 16, "timeout_seconds": 30.0})
            watches.append(watch)
            prompt = f"Prototype hit at X={point['X']:.1f}; interaction available"
            await service("WriteWidgetText", {"Widget": object_arg(widget), "Text": prompt})
            selected_opacity = 0.5 if baseline_opacity != 0.5 else 0.75
            await invoke(widget, "SetRenderOpacity", {"InOpacity": selected_opacity})
            independent = await invoke(widget, "GetRenderOpacity")
            prompt_read = outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT}))
            after_snapshot = await call("sn2_snapshot", {"targets": [{"handle": widget["handle"],
                "properties": ["RenderOpacity"]}], "timeout_seconds": 30.0})
            compared = await call("sn2_snapshot_compare", {"before": before_snapshot, "after": after_snapshot})
            if (independent.get("result") != selected_opacity or prompt_read.get("Text") != prompt
                    or snapshot_property(after_snapshot, widget["handle"], "RenderOpacity") != selected_opacity
                    or compared.get("equal_selected_values") is not False or compared.get("value_changes") != 1):
                raise AcceptanceError("The connected interaction prototype was not independently observed")
            watch_deadline = time.monotonic() + 30.0
            while True:
                observed = await call("sn2_watch_read", {"watch_id": watch["watch_id"],
                    "expected_session_id": watch["session_id"], "timeout_seconds": 30.0})
                if observed.get("gap") or observed.get("dropped_events"):
                    raise AcceptanceError("The prototype observation lost events")
                changed_events = [event for event in observed.get("events", [])
                    if event.get("kind") == "changed" and snapshot_property(
                        event["snapshot"], widget["handle"], "RenderOpacity") == selected_opacity]
                if changed_events:
                    break
                if time.monotonic() >= watch_deadline:
                    raise AcceptanceError("The game Tick watch did not observe the prototype revision")
                await asyncio.sleep(0.1)
            summary["checks"]["connected_interaction_prototype"] = {
                "trace": hit, "widget": widget, "prompt": prompt, "prompt_read": prompt_read,
                "before": before_snapshot, "after": after_snapshot, "comparison": compared,
                "independent_getter": independent, "watch": watch, "observed": observed,
                "text_change_route": "cooked_service_WriteWidgetText",
                "opacity_change_route": "existing_reflected_SetRenderOpacity",
                "watch_limit": "FText is read through the service; snapshot/watch observes supported RenderOpacity"}
            summary["checks"]["connected_interaction_prototype"]["watch_cleanup"] = await stop_watch(watch)

            ignored = outputs(await service("TraceImpactPoint", {
                "Start": {"X": -1000, "Y": 0, "Z": 0}, "End": {"X": 1000, "Y": 0, "Z": 0},
                "Channel": channel, "TraceComplex": False, "ActorsToIgnore": [object_arg(service_actor)],
                "IgnoreSelf": False, "Hit": OUT, "ImpactPoint": OUT}))
            if ignored.get("Hit") is not False:
                raise AcceptanceError("The trace ignore list did not exclude the owned collision actor")
            summary["checks"]["trace_ignore"] = ignored
            collision_destroy_attempted = True
            await invoke(collision, "K2_DestroyComponent", {"Object": object_arg(service_actor)})
            collision = None

            # CollectGarbage schedules a full purge through the game engine.
            # An unheld control establishes subsequent collection evidence;
            # a returned ProcessEvent alone does not establish completed GC.
            await service("CreateHeldObject", {"ObjectClass": object_arg(text_class), "Index": OUT})
            candidates = [item for item in await held() if identity(item) != identity(widget)]
            if len(candidates) != 1:
                raise AcceptanceError("The garbage-collection control was not uniquely retained")
            collection_control = candidates[0]
            control_before = await invoke(collection_control, "GetRenderOpacity")
            control_release = outputs(await service("ReleaseHeldObject", {
                "Object": object_arg(collection_control), "Released": OUT}))
            if control_release.get("Released") is not True:
                raise AcceptanceError("The garbage-collection control reference was not released")
            if [identity(item) for item in await held()] != [identity(widget)]:
                raise AcceptanceError("Releasing the collection control changed the interaction prototype")
            garbage = await function(kismet, "CollectGarbage")
            garbage_request = await invoke(kismet, "CollectGarbage")
            collection_evidence = await await_absent(collection_control, ["RenderOpacity"])
            retained = await held()
            if len(retained) != 1 or identity(retained[0]) != identity(widget):
                raise AcceptanceError("The retained prototype did not survive observed control invalidation")
            widget = retained[0]
            if outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT})).get("Text") != prompt:
                raise AcceptanceError("The retained prototype lost its authored state after garbage collection")
            summary["checks"]["gc_retention"] = {"collect_signature": garbage,
                "collect_request": garbage_request, "control_before_release": control_before,
                "control_release": control_release, "control_invalidation": collection_evidence,
                "retained": retained, "request_semantics": "ForceGarbageCollection(true) schedules collection",
                "evidence_scope": "unheld control invalidated; strongly held interaction identity and state retained"}

            previous_native = await call("sn2_native_status", {"target_process": selected})
            previous_service = await call("sn2_service_status", {"target_process": selected, "timeout_seconds": 30.0})
            previous_actor = previous_service["actor"]
            previous_actor_identity = previous_service["blueprint"]["actor"]["identity"]
            previous_widget = widget
            reconnect_watch = await call("sn2_watch_start", {"handle": widget["handle"],
                "properties": ["RenderOpacity"], "duration_ms": 120000, "interval_ms": 50,
                "capacity": 16, "timeout_seconds": 30.0})
            watches.append(reconnect_watch)
            # This closes only our frontend/native connection. The cooked actor
            # remains retained in the existing game's native lifecycle record.
            await server_lifetimes.aclose()
            watches.clear()
            server = create_server(workspace, runtime_root=repository, game_root=game,
                                   modkit_root=modkit, engine_root=engine)
            await server_lifetimes.enter_async_context(server._lowlevel_server.lifespan(server._lowlevel_server))
            reconnected_native = await call("sn2_native_status", {"target_process": selected})
            if (reconnected_native.get("session_id") == previous_native.get("session_id")
                    or reconnected_native.get("ready") is not True
                    or reconnected_native.get("process_id") != previous_native.get("process_id")
                    or reconnected_native.get("process_creation_filetime") != previous_native.get("process_creation_filetime")):
                raise AcceptanceError("The frontend reconnect did not retain the same game with a fresh native session")
            reconnected_service = await call("sn2_service_status", {"target_process": selected, "timeout_seconds": 30.0})
            if (not reconnected_service.get("available")
                    or reconnected_service["blueprint"]["actor"]["identity"] != previous_actor_identity
                    or reconnected_service["blueprint"]["activation_attempts"] != previous_service["blueprint"]["activation_attempts"]):
                raise AcceptanceError("The frontend reconnect duplicated or replaced the cooked service actor")
            service_actor = reconnected_service["actor"]
            stale = await call("sn2_object_properties", {"handle": previous_widget["handle"],
                "property_names": ["RenderOpacity"], "timeout_seconds": 30.0}, expected_error=True)
            if stale.get("code") != "stale_handle":
                raise AcceptanceError("The old frontend object handle was not rejected after reconnect")
            stale_watch = await call("sn2_watch_resume", {"watch_id": reconnect_watch["watch_id"],
                "expected_session_id": reconnect_watch["session_id"], "timeout_seconds": 30.0}, expected_error=True)
            if stale_watch.get("code") != "native_session_mismatch":
                raise AcceptanceError("The old watch session was not rejected after reconnect")
            new_watches = await call("sn2_watch_list", {"timeout_seconds": 30.0})
            if new_watches.get("items"):
                raise AcceptanceError("The reconnected frontend inherited obsolete watch handles")
            current_objects = await held()
            if len(current_objects) != 1 or identity(current_objects[0]) != identity(previous_widget):
                raise AcceptanceError("The prototype registry changed across frontend reconnect")
            widget = current_objects[0]
            reconnect_text = outputs(await service("ReadWidgetText", {"Widget": object_arg(widget), "Text": OUT}))
            reconnect_opacity = await invoke(widget, "GetRenderOpacity")
            if reconnect_text.get("Text") != prompt or reconnect_opacity.get("result") != selected_opacity:
                raise AcceptanceError("The interaction prototype did not retain its revision across reconnect")
            summary["checks"]["frontend_reconnect"] = {"previous_native": previous_native,
                "reconnected_native": reconnected_native, "previous_actor": previous_actor,
                "previous_actor_identity": previous_actor_identity, "service": reconnected_service,
                "obsolete_object_rejection": stale, "obsolete_watch_rejection": stale_watch,
                "watch_inventory": new_watches, "prototype": widget,
                "text": reconnect_text, "independent_opacity": reconnect_opacity,
                "service_enable_repeated": False, "prototype_creation_repeated": False}
            released = outputs(await service("ReleaseHeldObject", {"Object": object_arg(widget), "Released": OUT}))
            if released.get("Released") is not True or await held() != []:
                raise AcceptanceError("The service did not remove the exact prototype reference")
            summary["checks"]["release"] = released
            text_class = await exact_object("/Script/UMG.TextBlock", "Class")
            populated_creation = outputs(await service("CreateHeldObject", {
                "ObjectClass": object_arg(text_class), "Index": OUT}))
            populated = await held()
            if len(populated) != 1 or populated_creation.get("Index") != 0:
                raise AcceptanceError("The populated clear control was not strongly retained")
            clear_control = populated[0]
            clear_text = "Populated ClearHeldObjects reference-release control"
            await service("WriteWidgetText", {"Widget": object_arg(clear_control), "Text": clear_text})
            clear_read = outputs(await service("ReadWidgetText", {
                "Widget": object_arg(clear_control), "Text": OUT}))
            if clear_read.get("Text") != clear_text:
                raise AcceptanceError("The populated clear control was not independently readable")
            clear_held_attempted = True
            cleared = await service("ClearHeldObjects")
            remaining_held = await held()
            if remaining_held != []:
                raise AcceptanceError("Service teardown did not empty its strong-reference inventory")
            kismet = await exact_object("/Script/Engine.Default__KismetSystemLibrary")
            clear_gc_request = await invoke(kismet, "CollectGarbage")
            clear_invalidation = await await_absent(clear_control, ["RenderOpacity"])
            summary["checks"]["populated_clear"] = {"created": populated_creation,
                "held_before": populated, "independent_text_before": clear_read,
                "clear_invocation": cleared, "held_after": remaining_held,
                "collect_request": clear_gc_request, "control_invalidation": clear_invalidation,
                "evidence_scope": "nonempty strong-reference inventory cleared; former held control independently invalidated"}
            # Leave one owned control for natural EndPlay to release, rather
            # than pre-clearing every object and calling that lifecycle proof.
            text_class = await exact_object("/Script/UMG.TextBlock", "Class")
            await service("CreateHeldObject", {"ObjectClass": object_arg(text_class), "Index": OUT})
            controls = await held()
            if len(controls) != 1:
                raise AcceptanceError("The natural EndPlay control object was not retained")
            endplay_control = controls[0]
            await service("WriteWidgetText", {"Widget": object_arg(endplay_control),
                                              "Text": "Natural EndPlay reference-release control"})
            summary["checks"]["endplay_control"] = endplay_control
            summary["state"] = "runtime_checks_passed"
        except BaseException as error:
            primary_error = error
            summary["state"] = "failed"
            summary["error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            cleanup_errors = []
            if launch_requested and selected is None:
                try:
                    discovered = await call("sn2_target_processes", cleanup=True)
                    summary["cleanup"]["launch_recovery_snapshot"] = discovered
                    expected_executable = str(game / "Subnautica2/Binaries/Win64/Subnautica2-Win64-Shipping.exe").casefold()
                    matches = [item for item in discovered.get("processes", [])
                               if str(item.get("executable", "")).casefold() == expected_executable]
                    if len(matches) == 1:
                        # Preflight proved this game was stopped before our launch.
                        selected = matches[0]
                        summary["cleanup"]["recovered_launch_identity"] = selected
                except BaseException as error:
                    cleanup_errors.append({"stage": "launch_recovery", "message": str(error)})
            if selected is not None:
                try:
                    for watch in list(watches):
                        summary["cleanup"].setdefault("watches", []).append(await stop_watch(watch, cleanup=True))
                    current = await call("sn2_service_status", {"target_process": selected,
                        "timeout_seconds": 30.0}, cleanup=True)
                    if current.get("available"):
                        ending_actor = current["actor"]
                        if collision is not None and not collision_destroy_attempted:
                            collision_destroy_attempted = True
                            await invoke(collision, "K2_DestroyComponent", {"Object": object_arg(current["actor"])})
                            collision = None
                        elif collision is not None:
                            component_state = await call("sn2_object_properties", {
                                "handle": collision["handle"], "limit": 1, "timeout_seconds": 30.0},
                                allow_error=True, cleanup=True)
                            summary["cleanup"]["component_destroy_not_replayed"] = {
                                "component": collision, "read_only_state": component_state,
                                "reason": "an earlier destroy request was already attempted; natural Actor teardown remains the cleanup route"}
                        if primary_error is not None and not clear_held_attempted:
                            clear_held_attempted = True
                            await call("sn2_service_call", {"function": "ClearHeldObjects", "arguments": {},
                                "target_process": selected, "timeout_seconds": 30.0}, cleanup=True)
                        elif primary_error is not None:
                            held_state = await call("sn2_service_call", {"function": "GetHeldObjects",
                                "arguments": {"Objects": OUT}, "target_process": selected,
                                "timeout_seconds": 30.0}, allow_error=True, cleanup=True)
                            summary["cleanup"]["held_clear_not_replayed"] = {
                                "read_only_state": held_state,
                                "reason": "an earlier ClearHeldObjects request was already attempted; natural Actor teardown remains the cleanup route"}
                        summary["cleanup"]["service_disable"] = await call("sn2_service_disable", {
                            "target_process": selected, "timeout_seconds": 30.0}, cleanup=True)
                        deadline = time.monotonic() + 30.0
                        while True:
                            ended = await call("sn2_service_status", {"target_process": selected,
                                "timeout_seconds": 30.0}, cleanup=True)
                            observation = ended.get("blueprint", {}).get("actor_observation", {})
                            if observation.get("state") in {"destroying_observed", "actor_invalidated"}:
                                summary["cleanup"]["actor_end"] = ended
                                break
                            if time.monotonic() >= deadline:
                                raise AcceptanceError("The companion actor end was not independently observed")
                            await asyncio.sleep(0.1)
                        summary["cleanup"]["ended_actor_invalidation"] = await await_absent(
                            ending_actor, ["ServicePurpose"], cleanup=True)
                        if endplay_control is not None and primary_error is None:
                            library = await exact_object("/Script/Engine.Default__KismetSystemLibrary")
                            collect_request = await invoke(library, "CollectGarbage")
                            invalidated = await await_absent(endplay_control, ["RenderOpacity"], cleanup=True)
                            summary["cleanup"]["endplay_control_collection"] = {
                                "control": endplay_control, "collect_request": collect_request,
                                "invalidation": invalidated, "manual_clear_before_disable": False,
                                "manual_endplay_event_invoked": False,
                                "native_end_play_observed": observation.get("end_play_observed"),
                                "evidence_scope": "natural Actor teardown and subsequent held-control invalidation; EndPlay event is not directly instrumented"}
                except BaseException as error:
                    cleanup_errors.append({"stage": "service", "message": str(error)})
                try:
                    summary["cleanup"]["game_stop"] = await call("sn2_development_stop", {
                        "target_process": selected}, cleanup=True)
                except BaseException as error:
                    cleanup_errors.append({"stage": "game_stop", "message": str(error)})
            try:
                processes = await call("sn2_target_processes", cleanup=True)
                summary["cleanup"]["processes"] = processes
                if launch_requested and selected is None and processes.get("processes"):
                    cleanup_errors.append({"stage": "launch_identity_unresolved",
                        "message": "Launch did not return a selected process identity; inspect captured processes"})
                if processes.get("running") is not False:
                    cleanup_errors.append({"stage": "exit", "message": "The selected game exit was not confirmed"})
                elif installation_requested and package is not None:
                    deployment = await call("sn2_mod_deployment_status", {"plugin_id": SERVICE}, cleanup=True)
                    summary["cleanup"]["installation_before_removal"] = deployment
                    if (deployment.get("identified_installation")
                            and deployment.get("receipt", {}).get("package_manifest_sha256") == package["manifest_sha256"]):
                        summary["cleanup"]["uninstall"] = await call("sn2_mod_uninstall", {
                            "plugin_id": SERVICE,
                            "expected_installed_inventory_sha256": deployment["installed_inventory_sha256"]}, cleanup=True)
                        after = await call("sn2_mod_deployment_status", {"plugin_id": SERVICE}, cleanup=True)
                        summary["cleanup"]["installation_after_removal"] = after
                        if after.get("installed_files"):
                            cleanup_errors.append({"stage": "removal", "message": "The test plugin remains installed"})
                    elif deployment.get("installed_files"):
                        cleanup_errors.append({"stage": "installation_unresolved",
                            "message": "The installation could not be correlated; it was preserved"})
            except BaseException as error:
                cleanup_errors.append({"stage": "final_state", "message": str(error)})
            summary["cleanup"]["errors"] = cleanup_errors
            environment_after = {"modkit_exists": modkit.exists(), "engine_exists": engine.exists()}
            summary["authoring_environment"]["after"] = environment_after
            if args.require_no_authoring_environment and any(environment_after.values()):
                cleanup_errors.append({"stage": "authoring_environment",
                    "message": "A configured authoring dependency path appeared during the consumer check"})
            summary["call_count"] = sequence
            summary["limits"] = ["These runtime wrappers do not compile or cook new persistent Blueprint packages",
                                 "Removing strong references does not itself prove an object was collected",
                                 "Lifecycle evidence covers native Actor teardown and control invalidation, not direct EndPlay event instrumentation",
                                 "Installed-package mode exercises the wheel and supplied runtime bundle; bundled source files may remain present"]
            if primary_error is None and not cleanup_errors:
                summary["state"] = "passed"
            elif primary_error is None:
                summary["state"] = "cleanup_unconfirmed"
            write_json(output / "summary.json", summary)
    print(json.dumps({"state": summary["state"], "summary": str(output / "summary.json"),
                      "call_count": sequence}, ensure_ascii=False))
    return 0 if summary["state"] == "passed" else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-path", required=True)
    parser.add_argument("--workspace-root", type=Path, default=Path("D:/SN2Modding"))
    parser.add_argument("--repository-root", type=Path, default=ROOT)
    parser.add_argument("--game-root", type=Path, default=Path("E:/Games/Subnautica 2"))
    parser.add_argument("--modkit-root", type=Path)
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--require-no-authoring-environment", action="store_true")
    parser.add_argument("--use-installed-package", action="store_true")
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--readiness-timeout-seconds", type=float, default=180.0)
    args = parser.parse_args()
    if not math.isfinite(args.readiness_timeout_seconds) or args.readiness_timeout_seconds <= 0:
        parser.error("Readiness timeout must be positive and finite")
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
