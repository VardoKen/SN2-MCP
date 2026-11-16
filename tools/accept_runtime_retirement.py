"""Verify owned-root retirement, real GC effects and same-process reconnect.

Run with an installed consumer frontend, the workspace and A/B author summary
from accept_standalone_runtime_mod.py, and an already ready game with the service
actor enabled. This tool never launches the game, installs files, enables the
service or invokes GameInstance lifecycle events. It creates temporary held
instances, retires A, requests GC and observes effects. Unknown mutation outcomes
are recorded and never automatically retried. B remains the working revision.
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
import re
import sys
import sysconfig
import time
from typing import Any


OUT = {"out": True}
KISMET_CDO = "/Script/Engine.Default__KismetSystemLibrary"


class AcceptanceError(RuntimeError):
    pass


def write_json(path: Path, value: Any) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write("\n")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise AcceptanceError(f"Expected a JSON object: {path}")
    return value


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while fragment := stream.read(1024 * 1024):
            digest.update(fragment)
    return digest.hexdigest()


def inventory(root: Path) -> dict[str, str]:
    root = root.resolve(strict=True)
    result = {}
    for path in root.rglob("*"):
        if path.is_file():
            if not path.resolve(strict=True).is_relative_to(root):
                raise AcceptanceError(f"Inventory file escapes its root: {path}")
            result[path.relative_to(root).as_posix()] = file_hash(path)
    if not result:
        raise AcceptanceError(f"Expected nonempty file inventory: {root}")
    return result


def object_arg(object_: dict) -> dict:
    if not isinstance(object_.get("handle"), str):
        raise AcceptanceError("A current object descriptor is required")
    return {"handle": object_["handle"]}


def identity(object_: dict) -> tuple:
    return tuple(object_.get(key) for key in ("index", "path", "class"))


def array_objects(value: Any) -> list[dict]:
    if isinstance(value, list):
        items = value
    elif isinstance(value, dict) and value.get("partial") is False:
        items = value.get("items")
        if value.get("total_count") != len(items or []):
            raise AcceptanceError("The held object array is incomplete")
    else:
        raise AcceptanceError("Expected a complete held object array")
    if not isinstance(items, list):
        raise AcceptanceError("The held object array has no items")
    objects = []
    for item in items:
        if not isinstance(item, dict):
            raise AcceptanceError("The held object array has an invalid descriptor")
        object_ = item.get("value", item)
        if item.get("supported", True) is not True or not isinstance(object_, dict):
            raise AcceptanceError("The held object array contains unsupported values")
        object_arg(object_)
        objects.append(object_)
    return objects


def outputs(result: dict) -> dict:
    invocation = result.get("invocation", result)
    if (invocation.get("executed") is not True or invocation.get("process_event_returned") is not True
            or not isinstance(invocation.get("out_parameters"), dict)):
        raise AcceptanceError("No confirmed returned ProcessEvent with structured outputs")
    return invocation["out_parameters"]


def error_payload(texts: list[str]) -> dict:
    for text in texts:
        try:
            value = json.loads(text[text.index("{"):])
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            return value
    return {}


def latent_specification(plugin: str) -> dict:
    return {"schema": 1, "plugin_id": plugin, "assets": [{
        "kind": "blueprint", "path": f"/{plugin}/BP_Latent.BP_Latent",
        "parent_class": "/Script/CoreUObject.Object", "variables": [], "nodes": [], "connections": [],
        "functions": [{"name": "UnsupportedDelay", "inputs": [], "outputs": [], "nodes": [
            {"id": "entry", "kind": "function_entry"},
            {"id": "delay", "kind": "call_function", "class": "/Script/Engine.KismetSystemLibrary", "function": "Delay"},
            {"id": "result", "kind": "function_result"}], "connections": [
                {"from": {"node": "entry", "pin": "then"}, "to": {"node": "delay", "pin": "execute"}},
                {"from": {"node": "delay", "pin": "then"}, "to": {"node": "result", "pin": "execute"}}]}]}]}


def verify_retirement(result: dict, compiled: dict, *, reconcile: bool) -> None:
    from sn2_mcp.runtime_authoring import RuntimeAuthoring
    RuntimeAuthoring._validate_retirement(result, compiled, reconcile)
    if (result.get("state") != "released" or result.get("released_count", 0) <= 0
            or result.get("retained_count") != 0 or result.get("unknown_count") != 0):
        raise AcceptanceError("A retirement must have a complete released root inventory")
    if result.get("effects", {}).get("executed") is True and reconcile:
        raise AcceptanceError("Reconciliation must not execute root clearing")


async def run(args: argparse.Namespace) -> dict:
    if args.use_repository_frontend:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    import sn2_mcp
    from sn2_mcp import server as server_module
    from sn2_mcp.native_client import NativeClient, NativeError
    from mcp.server.mcpserver.exceptions import ToolError

    workspace = args.workspace_root.resolve(strict=True)
    runtime = args.runtime_root.resolve(strict=True)
    game = args.game_root.resolve(strict=True)
    selected = read_json(args.target_process)
    authored = read_json(args.author_summary)
    if authored.get("state") != "packages_prepared":
        raise AcceptanceError("author-summary must contain completed A/error/B package preparation")
    records = authored["checks"]["revisions"]
    compiled = {name: records[name]["compile"] for name in ("A", "B")}
    for name, receipt in compiled.items():
        identifier = receipt.get("authoring_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", identifier):
            raise AcceptanceError(f"Invalid authoring identity for {name}")
        actual = read_json(workspace / "artifacts/runtime-authoring" / identifier / "receipt.json")
        for key in ("authoring_id", "runtime_asset_path", "source_manifest_sha256", "specification_sha256"):
            if actual.get(key) != receipt.get(key):
                raise AcceptanceError(f"Author summary and workspace receipt differ: {name}.{key}")
        if (actual.get("compiled") is not True or actual.get("runtime_published") is not True
                or actual.get("retirement", {}).get("retirement_requested")):
            raise AcceptanceError(f"Revision {name} must be an active retained compilation")
        compiled[name] = actual
    if compiled["A"]["authoring_id"] == compiled["B"]["authoring_id"]:
        raise AcceptanceError("The two revisions must differ")
    imported = [Path(sn2_mcp.__file__).resolve(), Path(server_module.__file__).resolve()]
    installed_roots = {Path(sysconfig.get_path(key)).resolve() for key in ("purelib", "platlib")}
    if not args.use_repository_frontend and not all(any(path.is_relative_to(root) for root in installed_roots) for path in imported):
        raise AcceptanceError("Consumer checks require installed site-packages origins")
    version = importlib.metadata.version("sn2-mcp")
    if version != sn2_mcp.__version__:
        raise AcceptanceError("The imported frontend and distribution versions differ")
    modkit, engine = workspace / "UnavailableConsumerModkit", workspace / "UnavailableConsumerEngine"
    if modkit.exists() or engine.exists():
        raise AcceptanceError("The configured consumer Modkit/Engine roots must be absent")
    output = args.output.absolute()
    report = {"kind": "sn2_runtime_retirement_acceptance", "schema": 1, "state": "running",
              "frontend_version": version, "frontend_origins": [str(path) for path in imported],
              "installed_frontend_required": not args.use_repository_frontend, "selected_target": selected,
              "author_summary": str(args.author_summary.resolve()), "checks": {}, "cleanup": {},
              "game_launch_requested": False, "installation_requested": False, "service_enable_requested": False,
              "event_invocation_requested": False, "editor_invoked": False, "automatic_mutation_retries": False}
    protected = {}
    for name in ("A", "B"):
        for kind, root in (("package", Path(records[name]["package"]["package_root"])),
                           ("captured_source", Path(compiled[name]["staged_source_root"])),
                           ("save_outputs", Path(records[name]["save"]["receipt_path"]).parent)):
            resolved = root.resolve(strict=True)
            if not resolved.is_relative_to(workspace):
                raise AcceptanceError(f"Protected {name} {kind} is outside the selected workspace")
            protected[f"{name}_{kind}"] = {"root": str(resolved), "sha256": inventory(resolved)}
    project_root = Path(compiled["B"]["project_root"]).resolve(strict=True)
    if not project_root.is_relative_to(workspace):
        raise AcceptanceError("The original source project is outside the workspace")
    protected["original_project"] = {"root": str(project_root), "sha256": inventory(project_root)}
    if args.latent_plugin_id.casefold() in {compiled[name]["plugin_id"].casefold() for name in ("A", "B")}:
        raise AcceptanceError("The latent control must use a separate plugin identity")
    latent_project = f"projects/{args.latent_plugin_id}"
    if (workspace / latent_project).exists():
        raise AcceptanceError("The latent negative-control project must not already exist")
    output.mkdir(parents=True, exist_ok=False)
    sequence = 0
    owned: list[dict] = []
    creations: list[dict] = []
    release_attempted: set[tuple] = set()
    baseline_held: list[dict] | None = None
    server = None
    lifetimes = AsyncExitStack()
    native_identity: dict | None = None
    cleanup_failed = False
    connected = False

    def new_server():
        return server_module.create_server(workspace, args.native_port, game, runtime_root=runtime,
                                           modkit_root=modkit, engine_root=engine)

    async def connect() -> None:
        nonlocal server, connected
        server = new_server()
        await lifetimes.enter_async_context(server._lowlevel_server.lifespan(server._lowlevel_server))
        connected = True

    async def disconnect() -> None:
        nonlocal connected
        connected = False
        await lifetimes.aclose()

    async def call(tool: str, arguments: dict | None = None, *, expect_error: bool = False,
                   allow_error: bool = False, cleanup: bool = False) -> dict:
        nonlocal sequence
        sequence += 1
        started = time.monotonic()
        try:
            response = await server.call_tool(tool, arguments or {})
            texts = [item.text for item in response.content if item.type == "text"]
            value = response.structured_content if response.structured_content is not None else error_payload(texts)
            is_error = response.is_error
        except ToolError as error:
            texts, value, is_error = [str(error)], error_payload([str(error)]), True
        write_json(output / f"mcp-{sequence:04d}.json", {"tool": tool, "arguments": arguments or {},
                   "is_error": is_error, "result": value, "text": texts, "cleanup": cleanup,
                   "elapsed_seconds": time.monotonic() - started})
        if not isinstance(value, dict):
            raise AcceptanceError(f"No structured response from {tool}")
        if (expect_error and not is_error) or (is_error and not expect_error and not allow_error):
            raise AcceptanceError(f"Unexpected response from {tool}: {value.get('code', 'inspect_receipt')}")
        return value

    async def exact(path: str, *, cleanup: bool = False) -> dict:
        result = await call("sn2_find_objects", {"exact_path": path, "limit": 8,
                            "timeout_seconds": args.timeout_seconds}, cleanup=cleanup)
        items = [item for item in result.get("items", []) if item.get("path") == path]
        if result.get("scan_complete") is not True or len(items) != 1:
            raise AcceptanceError(f"No unique current exact object: {path}")
        return items[0]

    async def invoke(object_: dict, function: str, arguments: dict | None = None, *, cleanup: bool = False) -> dict:
        metadata = await call("sn2_object_functions", {"handle": object_["handle"], "function_query": function,
                              "limit": 64, "timeout_seconds": args.timeout_seconds}, cleanup=cleanup)
        functions = [item for item in metadata.get("items", []) if item.get("name") == function]
        if metadata.get("scan_complete") is not True or len(functions) != 1 or functions[0].get("signature_status") != "complete":
            raise AcceptanceError(f"No exact usable reflected function: {function}")
        result = await call("sn2_invoke", {"handle": object_["handle"], "function": function,
                            "arguments": arguments or {}, "expected_signature_token": functions[0]["signature_token"],
                            "timeout_seconds": args.timeout_seconds}, cleanup=cleanup)
        if result.get("executed") is not True or result.get("process_event_returned") is not True:
            raise AcceptanceError(f"No confirmed returned invocation: {function}")
        return result

    async def service(function: str, arguments: dict, *, cleanup: bool = False) -> dict:
        return await call("sn2_service_call", {"function": function, "arguments": arguments,
                          "target_process": selected, "timeout_seconds": args.timeout_seconds}, cleanup=cleanup)

    async def held(*, cleanup: bool = False) -> list[dict]:
        return array_objects(outputs(await service("GetHeldObjects", {"Objects": OUT}, cleanup=cleanup)).get("Objects"))

    async def create(class_: dict) -> dict:
        before = await held()
        attempt = {"before": before, "class": class_, "state": "outcome_unresolved", "request_sequence": sequence + 1}
        creations.append(attempt)
        created = await call("sn2_service_call", {"function": "CreateHeldObject",
                             "arguments": {"ObjectClass": object_arg(class_), "Index": OUT},
                             "target_process": selected, "timeout_seconds": args.timeout_seconds}, allow_error=True)
        attempt["response"] = created
        after = await held()
        previous = {identity(item) for item in before}
        additions = [item for item in after if identity(item) not in previous]
        attempt.update(after=after, additions=additions)
        if created.get("code"):
            effect = created.get("effect", created.get("details", {}))
            if (effect.get("executed") is False and effect.get("may_have_executed") is False
                    and [identity(item) for item in after] == [identity(item) for item in before]):
                attempt["state"] = "rejected_no_creation"
            raise AcceptanceError(f"Held-object creation failed: {created['code']}")
        values = outputs(created)
        if (len(additions) != 1 or len(after) != len(before) + 1
                or [identity(item) for item in after[:-1]] != [identity(item) for item in before]
                or additions[0].get("class") != class_.get("name") or values.get("Index") != len(before)):
            raise AcceptanceError("Creation did not append one isolated strong reference")
        # Only an exact class-matching append belongs to this test. An
        # ambiguous inventory must never authorize removal of other holders.
        owned.append(additions[0])
        attempt["state"] = "confirmed"
        return additions[0]

    async def release(object_: dict, *, cleanup: bool = False) -> dict:
        key = identity(object_)
        if key in release_attempted:
            raise AcceptanceError("The same release must never be retried automatically")
        release_attempted.add(key)
        result = await service("ReleaseHeldObject", {"Object": object_arg(object_), "Released": OUT}, cleanup=cleanup)
        if outputs(result).get("Released") is not True:
            raise AcceptanceError("The test instance strong reference was not removed")
        return result

    async def absent(object_: dict, *, cleanup: bool = False) -> dict:
        deadline = time.monotonic() + args.gc_wait_seconds
        while True:
            rejected = await call("sn2_object_properties", {"handle": object_["handle"],
                                  "limit": 1, "timeout_seconds": args.timeout_seconds},
                                  allow_error=True, cleanup=cleanup)
            code = rejected.get("code")
            if code is not None and code not in {"stale_handle", "object_destroying"}:
                raise AcceptanceError(f"Unexpected object invalidation result: {code}")
            scan = await call("sn2_find_objects", {"exact_path": object_["path"], "limit": 8,
                              "timeout_seconds": args.timeout_seconds}, cleanup=cleanup)
            if code in {"stale_handle", "object_destroying"} and scan.get("scan_complete") is True and not scan.get("items"):
                return {"object": object_, "handle_rejection": rejected, "registry_absence": scan,
                        "scope": "complete scan of inspectable exact-path objects"}
            if time.monotonic() >= deadline:
                raise AcceptanceError(f"GC did not invalidate the selected identity: {object_['path']}")
            await asyncio.sleep(0.1)

    async def diagnostic(object_: dict) -> dict:
        return outputs(await invoke(object_, "ReadStartupState", {name: OUT for name in
                       ("Initialized", "Count", "Shutdowns", "BaselineFrameRate", "BaselineEnabled", "AppliedFrameRate", "Revision")}))

    async def preserved() -> dict:
        for label, capture in protected.items():
            if inventory(Path(capture["root"])) != capture["sha256"]:
                raise AcceptanceError(f"The protected file inventory changed: {label}")
        working = await call("sn2_runtime_authoring_status", {"working": True})
        if working.get("authoring_id") != compiled["B"]["authoring_id"] or working.get("compiled") is not True:
            raise AcceptanceError("The working B revision was replaced or retired")
        return {"working": working, "protected_inventories_unchanged": True}

    async def native_request(operation: str, parameters: dict) -> dict:
        nonlocal sequence
        sequence += 1
        values = {**parameters, "expected_process_id": native_identity["process_id"],
                  "expected_creation_filetime": native_identity["process_creation_filetime"]}
        with NativeClient(port=args.native_port, timeout=args.timeout_seconds) as client:
            try:
                result = await asyncio.to_thread(client.request, operation, values)
            except NativeError as error:
                write_json(output / f"native-{sequence:04d}.json", {"operation": operation, "parameters": values,
                           "error": {"code": error.code, "message": str(error), "may_have_executed": error.may_have_executed,
                                     "request_id": error.request_id, "details": error.details}})
                raise
        write_json(output / f"native-{sequence:04d}.json", {"operation": operation, "parameters": values, "result": result})
        return result

    try:
        await connect()
        native_identity = await call("sn2_native_status", {"target_process": selected})
        if native_identity.get("ready") is not True:
            raise AcceptanceError("The selected already running native executor is not ready")
        for name in ("A", "B"):
            expected_token = f"revision-{native_identity['process_id']}-{native_identity['process_creation_filetime']}-"
            if not compiled[name].get("native_response", {}).get("revision_id", "").startswith(expected_token):
                raise AcceptanceError(f"Revision {name} belongs to a different game process")
        state = await call("sn2_service_status", {"target_process": selected, "timeout_seconds": args.timeout_seconds})
        if state.get("available") is not True:
            raise AcceptanceError("The development service must already be enabled")
        baseline_held = await held()
        report["checks"]["initial"] = {"native": native_identity, "service": state, "held": baseline_held,
                                     "protected": protected, **await preserved()}
        classes = {name: await exact(compiled[name]["runtime_asset_path"] + "_C") for name in ("A", "B")}
        # Normal weak serial allocation can invalidate ordinary class handles;
        # refresh them for each creation instead of weakening their guards.
        instance_a = await create(await exact(classes["A"]["path"]))
        instance_b = await create(await exact(classes["B"]["path"]))
        state_a, state_b = await diagnostic(instance_a), await diagnostic(instance_b)
        if (state_a.get("Revision") != "A" or state_b.get("Revision") != "B"
                or state_a.get("Initialized") is not False or state_b.get("Initialized") is not False
                or state_a.get("Count") != 0 or state_b.get("Count") != 0):
            raise AcceptanceError("The isolated holders must retain A/B defaults without lifecycle invocation")
        # Preserve latest.json independently: retiring historical A must not
        # replace either the most recent compile attempt or working B.
        latest = workspace / "artifacts/runtime-authoring/latest.json"
        latest_before = file_hash(latest)
        retired = await call("sn2_runtime_release_blueprint", {"authoring_id": compiled["A"]["authoring_id"],
                             "timeout_seconds": args.timeout_seconds})
        verify_retirement(retired, compiled["A"], reconcile=False)
        if file_hash(latest) != latest_before:
            raise AcceptanceError("Retiring A changed the latest compile pointer")
        report["checks"]["retirement"] = {"release": retired, **await preserved()}
        # Construct the unheld control after retirement and file checks, using
        # the class retained by live A. Confirm its presence immediately before
        # the scheduled GC request; do not count an already absent control.
        control_a = await create(await exact(classes["A"]["path"]))
        kismet = await exact(KISMET_CDO)
        released_control = await release(control_a)
        before_gc_control = await exact(control_a["path"])
        if identity(before_gc_control) != identity(control_a):
            raise AcceptanceError("The unheld control was replaced before the collection request")
        gc = await invoke(kismet, "CollectGarbage")
        control_absent = await absent(control_a)
        current_a = await exact(instance_a["path"])
        if identity(current_a) != identity(instance_a) or await diagnostic(current_a) != state_a:
            raise AcceptanceError("Held A identity or logic changed during completed GC")
        if identity(await exact(classes["A"]["path"])) != identity(classes["A"]):
            raise AcceptanceError("A held instance did not retain its generated class")
        current_b = await exact(instance_b["path"])
        if identity(current_b) != identity(instance_b) or await diagnostic(current_b) != state_b:
            raise AcceptanceError("B changed while retiring A")
        saved_b = await call("sn2_runtime_save_package", {"authoring_id": compiled["B"]["authoring_id"],
                            "timeout_seconds": args.timeout_seconds})
        if saved_b.get("persisted") is not True or saved_b.get("containerized") is not True:
            raise AcceptanceError("Active B could not be saved after holder/GC identity allocation")
        report["checks"]["held_gc"] = {"collection_request": gc, "control_reference_release": released_control,
                                      "control_live_before_request": before_gc_control, "control_absent": control_absent,
                                      "held_a": current_a, "state_a": state_a, "held_b": current_b, "state_b": state_b,
                                      "b_saved_after_gc": saved_b, **await preserved()}
        released_a = await release(current_a)
        released_b = await release(current_b)
        second_gc = await invoke(await exact(KISMET_CDO), "CollectGarbage")
        report["checks"]["unheld_gc"] = {"a_reference_release": released_a, "b_reference_release": released_b,
                                        "collection_request": second_gc, "a_absent": await absent(current_a),
                                        "a_class_absent": await absent(classes["A"]), "b_instance_absent": await absent(current_b),
                                        "b_class_retained": await exact(classes["B"]["path"]), **await preserved()}
        await call("sn2_create_mod_project", {"project_path": latent_project, "mod_id": args.latent_plugin_id,
                                           "title": "Synchronous runtime latent rejection control"})
        latent_spec = latent_specification(args.latent_plugin_id)
        await call("sn2_write_source", {"file_path": latent_project + "/authoring.json",
                                      "content": json.dumps(latent_spec, indent=2, ensure_ascii=False) + "\n"})
        rejected = await call("sn2_runtime_compile_blueprint", {"project_path": latent_project,
                              "spec_path": latent_project + "/authoring.json", "timeout_seconds": args.timeout_seconds}, expect_error=True)
        effect = rejected.get("effect", {})
        if (rejected.get("code") != "latent_function_in_function_graph" or effect.get("executed") is not False
                or effect.get("may_have_executed") is not False):
            raise AcceptanceError("Latent Delay was not precisely rejected before game mutation")
        report["checks"]["latent_rejection"] = {"rejection": rejected, **await preserved()}
        previous_native = native_identity
        old_b_handle = (await exact(classes["B"]["path"]))["handle"]
        await disconnect()
        # Each request opens a genuinely new native connection. Completed
        # tombstones must answer without resolving a collected class handle.
        token = compiled["A"]["native_response"]["revision_id"]
        repeated = await native_request("runtime_release_blueprint", {"revision_id": token, "reconcile": False})
        reconciled = await native_request("runtime_release_blueprint", {"revision_id": token, "reconcile": True})
        verify_retirement(repeated, compiled["A"], reconcile=False)
        verify_retirement(reconciled, compiled["A"], reconcile=True)
        if repeated["effects"]["executed"] is not False:
            raise AcceptanceError("A completed tombstone repeated root clearing")
        b_audit = await native_request("runtime_release_blueprint", {
            "revision_id": compiled["B"]["native_response"]["revision_id"], "reconcile": True})
        from sn2_mcp.runtime_authoring import RuntimeAuthoring
        RuntimeAuthoring._validate_retirement(b_audit, compiled["B"], True)
        if b_audit.get("state") != "retained" or b_audit.get("retirement_requested") is not False:
            raise AcceptanceError("Read-only B audit changed active ownership")
        await connect()
        native_identity = await call("sn2_native_status", {"target_process": selected})
        if (native_identity.get("ready") is not True or native_identity.get("session_id") == previous_native.get("session_id")
                or native_identity.get("process_id") != previous_native.get("process_id")
                or native_identity.get("process_creation_filetime") != previous_native.get("process_creation_filetime")):
            raise AcceptanceError("Reconnect did not establish a new session on the same ready process")
        stale = await call("sn2_object_properties", {"handle": old_b_handle, "limit": 1,
                           "timeout_seconds": args.timeout_seconds}, expect_error=True)
        if stale.get("code") != "stale_handle":
            raise AcceptanceError("An obsolete frontend handle was not rejected")
        frontend_repeated = await call("sn2_runtime_release_blueprint", {"authoring_id": compiled["A"]["authoring_id"],
                                      "timeout_seconds": args.timeout_seconds})
        verify_retirement(frontend_repeated, compiled["A"], reconcile=False)
        if frontend_repeated != retired:
            raise AcceptanceError("Frontend repeated retirement did not return its completed durable receipt")
        saved_b_reconnect = await call("sn2_runtime_save_package", {"authoring_id": compiled["B"]["authoring_id"],
                                      "timeout_seconds": args.timeout_seconds})
        if saved_b_reconnect.get("persisted") is not True or saved_b_reconnect.get("containerized") is not True:
            raise AcceptanceError("Stable B revision token did not save after frontend reconnect")
        if [identity(item) for item in await held()] != [identity(item) for item in baseline_held]:
            raise AcceptanceError("The test changed the preexisting held object inventory")
        report["checks"]["reconnect"] = {"previous_native": previous_native, "native": native_identity,
                                         "native_tombstone_repeat": repeated, "native_tombstone_reconcile": reconciled,
                                         "active_b_audit": b_audit, "obsolete_b_handle_rejection": stale,
                                         "frontend_completed_receipt": frontend_repeated,
                                         "b_saved_after_reconnect": saved_b_reconnect, **await preserved()}
        refreshes = b_audit.get("identity_refreshes", [])
        report["checks"]["normal_serial_allocation"] = {
            "observed": any(item.get("before_serial") == 0 and item.get("after_serial", 0) > 0
                            or item.get("before_class_serial") == 0 and item.get("after_class_serial", 0) > 0
                            for item in refreshes),
            "identity_refreshes": refreshes,
            "b_saved_after_gc_and_reconnect": True,
            "scope": "Continuous RootSet identities only; unchanged positive serial identities require no refresh"}
        report["state"] = "accepted"
        report["evidence_limits"] = ["GC completion inferred from controlled invalidation and exact-path absence",
                                     "Retirement reports root ownership only; it never claims object destruction",
                                     "Isolated GameInstance diagnostic calls do not exercise natural lifecycle events"]
    except BaseException as error:
        report["state"] = "failed"
        report["error"] = {"type": type(error).__name__, "message": str(error)}
        raise
    finally:
        try:
            if server is not None and baseline_held is not None:
                if not connected:
                    await connect()
                    await call("sn2_native_status", {"target_process": selected}, cleanup=True)
                current = await held(cleanup=True)
                reconciliations = []
                for attempt in creations:
                    if attempt["state"] != "outcome_unresolved":
                        continue
                    before = attempt["before"]
                    additions = [item for item in current if identity(item) not in {identity(item) for item in before}]
                    response = attempt.get("response", {})
                    effect = response.get("effect", response.get("details", {}))
                    if (effect.get("executed") is False and effect.get("may_have_executed") is False
                            and [identity(item) for item in current] == [identity(item) for item in before]):
                        attempt["state"] = "rejected_no_creation"
                        reconciliations.append({"request_sequence": attempt["request_sequence"],
                                                "confirmed_preflight_rejection": True, "creation_repeated": False})
                        continue
                    confirmed = (len(additions) == 1 and len(current) == len(before) + 1
                                 and [identity(item) for item in current[:-1]] == [identity(item) for item in before]
                                 and additions[0].get("class") == attempt["class"].get("name"))
                    if confirmed:
                        owned.extend(additions)
                        attempt.update(state="identified_by_read_only_inventory", additions=additions)
                    reconciliations.append({"request_sequence": attempt["request_sequence"], "before": before,
                                            "current": current, "identified_unique_class_matching_append": confirmed,
                                            "creation_repeated": False})
                report["cleanup"]["creation_reconciliation"] = reconciliations
                report["cleanup"]["unresolved_creation_attempts"] = [attempt for attempt in creations
                                                                     if attempt["state"] == "outcome_unresolved"]
                ours = {identity(item) for item in owned}
                removable = [item for item in current if identity(item) in ours and identity(item) not in release_attempted]
                cleanup_releases = []
                for item in removable:
                    cleanup_releases.append(await release(item, cleanup=True))
                if removable:
                    cleanup_gc = await invoke(await exact(KISMET_CDO, cleanup=True), "CollectGarbage", cleanup=True)
                    report["cleanup"]["collection_request"] = cleanup_gc
                    report["cleanup"]["invalidation"] = [await absent(item, cleanup=True) for item in removable]
                remaining = await held(cleanup=True)
                report["cleanup"].update(releases=cleanup_releases, held=remaining,
                                          preexisting_inventory_preserved=[identity(item) for item in remaining] ==
                                                                         [identity(item) for item in baseline_held],
                                          unconfirmed_release_not_retried=[item for item in remaining if identity(item) in ours])
                if report["cleanup"]["unresolved_creation_attempts"]:
                    raise AcceptanceError("A creation outcome remains unresolved after read-only inventory reconciliation")
                if not report["cleanup"]["preexisting_inventory_preserved"]:
                    raise AcceptanceError("The final service holder inventory differs from its baseline")
        except BaseException as error:
            cleanup_failed = True
            report["cleanup"]["error"] = {"type": type(error).__name__, "message": str(error)}
            if report["state"] == "accepted":
                report["state"] = "failed"
        finally:
            try:
                await disconnect()
            finally:
                write_json(output / "summary.json", report)
        if cleanup_failed and report.get("error") is None:
            raise AcceptanceError("Retirement acceptance cleanup is incomplete; inspect summary.json")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("workspace-root", "runtime-root", "game-root", "target-process", "author-summary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--native-port", type=int, default=27223)
    parser.add_argument("--timeout-seconds", type=float, default=60.0)
    parser.add_argument("--gc-wait-seconds", type=float, default=30.0)
    parser.add_argument("--latent-plugin-id", default="SN2RetirementLatentControl")
    parser.add_argument("--use-repository-frontend", action="store_true", help="Developer check only; consumer requires installed origins")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", args.latent_plugin_id):
        parser.error("latent-plugin-id must be an ASCII plugin identifier")
    if any(not math.isfinite(value) or value <= 0 for value in (args.timeout_seconds, args.gc_wait_seconds)):
        parser.error("Timeouts must be finite and positive")
    try:
        asyncio.run(run(args))
    except Exception as error:
        print(f"Runtime retirement acceptance failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
