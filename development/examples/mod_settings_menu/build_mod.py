"""Build the example with the declared Editor helper; never install or launch the game."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

REPOSITORY = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY / "src"))
from sn2_mcp.build_workflow import BuildError, run_build_command
from sn2_mcp.content_cooker import ContentCooker
from sn2_mcp.content_projects import ContentProjects
from sn2_mcp.workspace import SourceWorkspace, _checked

PLUGIN = "SN2ModSettingsTest"
HELPER = "ModSettingsMenuBuilder"
ASSETS = {"WBP_ModMainScreen", "WBP_ModMenuExtension", "WBP_ModSettingsPage"}
EXTENSION_CLASS = "/" + PLUGIN + "/UI/WBP_ModMenuExtension.WBP_ModMenuExtension_C"
DEFERRED_PACKAGES = (
    "/" + PLUGIN + "/UI/WBP_ModMenuExtension",
    "/" + PLUGIN + "/UI/WBP_ModSettingsPage",
    "/Game/Blueprints/UI/Lobby/WBP_MainLobbyScreen",
    "/Game/Blueprints/UI/GenericUIElements/GenericUI_WBP/WBP_GenericButtonBig",
)


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verify_main_references(report, source):
    references = report.get("main_screen_direct_script_property_references")
    if (report.get("main_screen_extension_hard_reference") is not False
            or not isinstance(references, list) or not references
            or any(not isinstance(path, str) or not path for path in references)):
        raise RuntimeError("The startup wrapper has no complete direct-reference check: " + source)
    for path in references:
        if any(path == package or path.startswith(package + ".") for package in DEFERRED_PACKAGES):
            raise RuntimeError("The startup wrapper has a hard reference that must load later: " + path)


def capture(source, destination, inventory):
    source = _checked(source, source.parent)
    raw = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("xb") as stream:
        stream.write(raw)
    if source.read_bytes() != raw:
        raise RuntimeError("Source changed during capture: " + str(source))
    inventory.append({"source_path": str(source), "path": str(destination), "bytes": len(raw),
                      "sha256": hashlib.sha256(raw).hexdigest()})


def command(arguments, cwd, root, name, timeout):
    try:
        result = run_build_command(arguments, cwd, timeout, root / (name + ".stdout.log"), root / (name + ".stderr.log"))
    except BuildError as error:
        write_json(root / (name + "-process.json"), error.receipt)
        raise
    write_json(root / (name + "-process.json"), result)
    if result["state"] != "succeeded" or result["exit_code"] != 0 or not result["tree_exit_confirmed"] or not result["output_complete"]:
        raise RuntimeError(name + " failed; inspect its complete process receipt")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace-root", type=Path, default=Path("D:/SN2Modding"))
    parser.add_argument("--engine-root", type=Path)
    parser.add_argument("--modkit-root", type=Path)
    parser.add_argument("--game-root", type=Path, default=Path("E:/Games/Subnautica 2"))
    options = parser.parse_args()
    workspace_root = options.workspace_root.resolve(strict=True)
    engine = (options.engine_root or workspace_root / "Engine").resolve(strict=True)
    modkit = (options.modkit_root or workspace_root / "Modkit").resolve(strict=True)
    game = options.game_root.resolve(strict=True)
    if not (engine / "Build/Build.version").is_file():
        engine = (engine / "Engine").resolve(strict=True)
    game_reference = game if (game / "Content/Paks").is_dir() else game / "Subnautica2"
    if not (game_reference / "Content/Paks").is_dir():
        raise RuntimeError("The selected game has no project Content/Paks directory")
    example = Path(__file__).parent
    workspace = SourceWorkspace(workspace_root)
    relative = "projects/ModSettingsMenuBuild-" + uuid.uuid4().hex
    root = workspace_root / relative
    root.mkdir(parents=True)
    inputs = []
    helper, host, editor = root / HELPER, root / "BuilderHost", root / "EditorProject"
    for source in sorted((example / "authoring" / HELPER).rglob("*")):
        if source.is_file():
            capture(source, helper / source.relative_to(example / "authoring" / HELPER), inputs)
    host.mkdir()
    target = host / "HostProject.uproject"
    write_json(target, {"FileVersion": 3, "EngineAssociation": "", "DisableEnginePluginsByDefault": True})
    dotnet = engine / "Binaries/ThirdParty/DotNet/8.0.300/win-x64/dotnet.exe"
    ubt = engine / "Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll"
    command([str(dotnet), str(ubt), "UnrealEditor", "Win64", "Development", "-Project=" + str(target),
             "-Plugin=" + str(helper / (HELPER + ".uplugin")), "-Module=" + HELPER, "-NoHotReload", "-NoUBTMakefiles",
             "-Unattended", "-NoEngineChanges", "-UsePrecompiled", "-NoUBA", "-NoXGE", "-Compiler=VisualStudio2022",
             "-CompilerVersion=14.44.35207", "-MaxParallelActions=1", "-NoPCH"], host, root, "builder-build", 3600)
    metadata = host / "Intermediate/Build/Win64/x64/UnrealEditor/Development/TargetMetadata.dat"
    command([str(dotnet), str(ubt), "-Mode=WriteMetadata", "-Input=" + str(metadata), "-Version=2"], host, root, "builder-metadata", 120)
    for name in ("UnrealEditor-" + HELPER + ".dll", "UnrealEditor.modules"):
        if not (helper / "Binaries/Win64" / name).is_file():
            raise RuntimeError("The helper build and WriteMetadata did not produce " + name)
    tracked = subprocess.run(["git", "-C", str(modkit), "ls-files", "-z"], check=True, capture_output=True).stdout.decode("utf-8").split("\0")
    for name in tracked:
        parts = Path(name).parts
        if parts and (parts[0] in {"Binaries", "Config", "Content", "Plugins", "Releases", "Source"}
                      or name in {"Subnautica2.uproject", "AssetRegistry.bin"}):
            if any(part.casefold() in {"nppbackup", ".git", "saved", "intermediate", "deriveddatacache"} for part in parts):
                continue
            capture(modkit / name, editor / name, inputs)
    (editor / "GameInstallDirectory.txt").write_text(game_reference.as_posix() + "\n", encoding="utf-8")
    for name in ("fmod.dll", "fmodL.dll", "fmodstudio.dll", "fmodstudioL.dll", "resonanceaudio.dll"):
        relative_dll = Path("Plugins/FMODStudio/Binaries/Win64") / name
        if not (editor / relative_dll).exists():
            capture(modkit / relative_dll, editor / relative_dll, inputs)
    for source in sorted(helper.rglob("*")):
        relative_source = source.relative_to(helper)
        if source.is_file() and (relative_source.parts[0] == "Source" or relative_source.as_posix() in {
                HELPER + ".uplugin", "Binaries/Win64/UnrealEditor-" + HELPER + ".dll", "Binaries/Win64/UnrealEditor.modules"}):
            capture(source, editor / "Plugins" / HELPER / relative_source, inputs)
    project = editor / "Plugins" / PLUGIN
    for source in sorted((example / "project").rglob("*")):
        if source.is_file():
            capture(source, project / source.relative_to(example / "project"), inputs)
    (project / "Content").mkdir(exist_ok=True)
    write_json(root / "capture.json", {"inputs": inputs, "game_reference": str(game_reference)})
    report_path = root / "authoring-report.json"
    editor_arguments = [str(engine / "Binaries/Win64/UnrealEditor-Cmd.exe"), str(editor / "Subnautica2.uproject"),
                        "-run=" + HELPER, "-EnablePlugins=" + HELPER, "-PluginRoot=" + str(project),
                        "-unattended", "-nop4", "-nosound", "-NullRHI", "-stdout", "-FullStdOutLogOutput",
                        "-ddc=NoZenLocalFallback", "-LocalDataCachePath=" + str(workspace_root / "Cache/DDC")]
    command([*editor_arguments, "-BackgroundWidget=DarkFade_1", "-TitleWidget=SettingTitle", "-BackWidget=CancelGraphicsButton",
             "-Report=" + str(report_path), "-abslog=" + str(root / "authoring.log")], editor, root, "authoring", 900)
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    if (report.get("kind") != "sn2_mod_settings_menu_build_report" or report.get("plugin_id") != PLUGIN
            or report.get("state") != "succeeded" or report.get("saved") is not True or report.get("errors")
            or {Path(item["filename"]).stem for item in report.get("outputs", [])} != ASSETS or len(report["outputs"]) != len(ASSETS)):
        raise RuntimeError("The helper did not compile and save all three Widget Blueprints; inspect authoring-report.json")
    if (report.get("extension_soft_class_path") != EXTENSION_CLASS
            or report.get("main_screen_tick_frequency") != "Auto"
            or report.get("menu_extension_tick_frequency") != "Auto"
            or report.get("menu_extension_parent") != "/Script/UMG.UserWidget"
            or report.get("menu_extension_root_visibility") != "HitTestInvisible"):
        raise RuntimeError("The authoring report does not establish the deferred input-transparent UI extension")
    verify_main_references(report, "authoring-report.json")
    authored = {}
    for output in report["outputs"]:
        asset = Path(output["filename"])
        if asset.parent != project / "Content/UI" or asset.stem not in ASSETS or asset.suffix != ".uasset" or asset.stat().st_size != output["bytes"]:
            raise RuntimeError("The helper output does not identify the expected saved UI asset")
        if output["generated_class"] != "/" + PLUGIN + "/UI/" + asset.stem + "." + asset.stem + "_C":
            raise RuntimeError("The saved UI output identifies a different generated class")
        authored[asset.relative_to(project).as_posix()] = {"sha256": hashlib.sha256(asset.read_bytes()).hexdigest(), "bytes": asset.stat().st_size}
    write_json(root / "authored-assets.json", authored)
    reload_path = root / "reload-report.json"
    command([*editor_arguments, "-InspectSaved", "-Report=" + str(reload_path), "-abslog=" + str(root / "reload.log")],
            editor, root, "reload", 900)
    reloaded = json.loads(reload_path.read_text(encoding="utf-8-sig"))
    if (reloaded.get("kind") != "sn2_mod_settings_menu_build_report" or reloaded.get("schema") != 1 or reloaded.get("plugin_id") != PLUGIN
            or reloaded.get("state") != "reload_verified" or reloaded.get("saved") is not False
            or reloaded.get("inspection_only") is not False or reloaded.get("reload_only") is not True
            or reloaded.get("saved_assets_reload_verified") is not True or reloaded.get("errors")):
        raise RuntimeError("The fresh Editor did not verify the saved assets; inspect reload-report.json")
    expected_reload = {
        "WBP_ModMainScreen": ("/Game/Blueprints/UI/WBP_MainScreen.WBP_MainScreen_C", False, {"Construct", "Tick", "Destruct"}),
        "WBP_ModMenuExtension": ("/Script/UMG.UserWidget", True, {"OpenModSettings", "Tick", "Destruct"}),
        "WBP_ModSettingsPage": ("/Script/UWECommonUI.FocusActivatableWidget", True, {"CloseModSettings", "Construct"}),
    }
    checks = reloaded.get("reload_checks", [])
    expected_paths = {"/" + PLUGIN + "/UI/" + name + "." + name for name in ASSETS}
    if len(checks) != len(ASSETS) or {item.get("asset") for item in checks} != expected_paths:
        raise RuntimeError("The fresh reload did not inspect exactly the three saved UI assets")
    for check in checks:
        name = check["asset"].rsplit(".", 1)[1]
        if name == "WBP_ModMainScreen":
            verify_main_references(check, "reload-report.json")
        parent, own_tree, required_functions = expected_reload[name]
        if (check.get("generated_class") != check["asset"] + "_C" or check.get("parent") != parent
                or check.get("own_widget_tree") is not own_tree
                or ("Tick" in required_functions and check.get("tick_frequency") != "Auto")
                or any(type(check.get(key)) is not int or check[key] <= 0 for key in ("graph_count", "nodes", "pins_with_links"))):
            raise RuntimeError("The fresh reload changed the saved parent/tree/graph composition: " + name)
        functions = check.get("functions", [])
        if len(functions) != len(required_functions) or {item.get("name") for item in functions} != required_functions:
            raise RuntimeError("The fresh reload lacks the required owned function bodies: " + name)
        for function in functions:
            size, bytecode = function.get("script_bytes"), function.get("script_hex")
            if (function.get("owned") is not True or type(size) is not int or size <= 0 or not isinstance(bytecode, str)
                    or len(bytecode) != size * 2 or len(bytes.fromhex(bytecode)) != size):
                raise RuntimeError("The fresh reload lacks complete nonempty owned bytecode: " + name + "." + function["name"])
    for name, expected in authored.items():
        raw = (project / name).read_bytes()
        if {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)} != expected:
            raise RuntimeError("The fresh inspection modified the saved source asset: " + name)
    cook = ContentCooker(REPOSITORY, workspace_root=workspace_root, modkit_root=modkit, engine_root=engine, game_root=game, workspace=workspace)
    receipt = cook.build(project.relative_to(workspace_root).as_posix(), timeout_seconds=3600)
    write_json(root / "cook-result.json", receipt)
    if receipt["state"] != "succeeded":
        raise RuntimeError("Cooking failed; inspect cook-result.json and the complete cooker receipt")
    cooked_assets = {item["path"]: {key: item[key] for key in ("sha256", "bytes")} for item in receipt["inputs"] if item["path"] in authored}
    if cooked_assets != authored:
        raise RuntimeError("Cooked source differs from the selected helper's saved assets")
    package = ContentProjects(workspace, REPOSITORY).package(receipt["receipt_path"], relative + "/package")
    write_json(root / "package-result.json", package)
    payload = Path(package["package_root"]) / PLUGIN
    print(json.dumps({"workspace": str(root), "package": package["package_root"], "manifest_sha256": package["manifest_sha256"],
                      "plugin_folder": str(payload), "editor_reload_verified": True, "installed": False, "game_executed": False}, indent=2))


if __name__ == "__main__":
    main()
