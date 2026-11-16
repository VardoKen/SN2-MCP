# Subnautica 2 MCP: agent installation and source build

These instructions are for any AI agent that can run commands and configure an MCP-compatible client. Use the client's actual supported interface and configuration location. Do not assume a universal MCP JSON layout or a specific host's credential/settings path. [README.md](README.md) is the human overview; [INSTALL.md](INSTALL.md) is the prebuilt manual installation route.

## Choose the requested route

| Task | Required inputs |
| --- | --- |
| Install/use the prebuilt bridge | Runtime ZIP, Python 3.11+, `uv`, Visual C++ x64 Runtime, compatible Windows x64 game. |
| Build the frontend and native DLL/loader | Source checkout, Python/`uv`, Git, CMake 3.24+, Visual Studio 2022 C++ and Windows SDK. No Unreal Editor or game is needed for this compilation. |
| Build the cooked companion from its specification | Source checkout plus the compatible custom Unreal Engine, Modkit, helper-build prerequisites and installed game container references described below. |
| Author supported mods with the installed bridge | Ready native executor and companion in the game. Consumer Unreal Editor/Modkit is not required. |

The tested checkpoint is Windows x64, UE 5.6.1 / CL131439 / game build3388. Inspect an updated game before claiming compatibility. Source labels alone do not prove runtime layouts. Keep the supplied implementation stack: Python MCP frontend, native C++ bridge, Unreal Content/Blueprint packages and existing build tools.

Before changes, inspect the assignment, applicable rules, actual paths, Git state, running processes and versions. Preserve unrelated work. Installing files in a game, launching/restarting it, changing configuration/saves or publishing a repository must be covered by the operator's assignment. Ordinary project-local builds and the cleanup of your confirmed temporary files do not need repeated approval. Separate compilation, packaging, installation and runtime verification in reports.

## Install the runtime ZIP

1. Obtain `SN2-MCP-0.9.0-windows-x64.zip` from [Releases](https://github.com/VardoKen/SN2-MCP/releases). The automatic Source code archives contain no ready DLL/loader/cooked companion. Extract to a selected immutable release directory.
2. Choose separate absolute release, writable runtime, workspace and Python-environment paths, plus the actual game root. Use absent runtime/environment destinations; create the workspace. Inspect required prerequisites instead of changing global settings for convenience.
3. Follow the dependency export, hash-locked install, wheel install, verification and runtime-copy commands in [INSTALL.md, step 2](INSTALL.md#2-install-the-python-frontend). Use the supplied `uv.lock` and wheel; do not build the frontend from source during this route. Confirm the import originates in the selected environment and reports 0.9.0:

   ```powershell
   & $sn2Python -I -B -c "import importlib.metadata, pathlib, sn2_mcp; print(importlib.metadata.version('sn2-mcp')); print(pathlib.Path(sn2_mcp.__file__).resolve())"
   ```

4. Configure the client's stdio command/arguments or local streamable HTTP from [INSTALL.md, step 4](INSTALL.md#4-connect-your-mcp-client). Specify `--workspace-root`, `--runtime-root` and `--game-root` explicitly. Do not use the source-oriented `start_shared_mcp.ps1` as a generic runtime ZIP launcher.
5. For managed companion installation, copy the entire **outer verified package**, including manifest/receipts, into the workspace:

   ```powershell
   New-Item -ItemType Directory -Path (Join-Path $sn2Workspace 'packages') -Force | Out-Null
   Copy-Item -LiteralPath (Join-Path $sn2Release 'services\SN2MCPDevelopmentServices') -Destination (Join-Path $sn2Workspace 'packages\SN2MCPDevelopmentServices') -Recurse
   ```

   The package destination must be absent. This differs from manual game installation, which copies only the inner plugin.
6. Call `sn2_status` and `sn2_capabilities` for local contracts. Call `sn2_verify_artifact_package` with `package_path: "packages/SN2MCPDevelopmentServices"`. Use the returned manifest hash for `sn2_mod_install` while the selected game is stopped. Keep the returned installation receipt/inventory. Do not overwrite an unrelated or manually installed copy; inspect `sn2_mod_deployment_status` and use the corresponding ownership/update path.
7. Start the game under applicable operator authority, or attach to the selected already running instance. Perform the native and companion readiness sequence below.

The runtime parent must be writable: managed content replacement/removal moves selected files under its `.trash/Subnautica 2 MCP/content-deployment/`. Preserve receipts and never empty the operator's trash automatically. Reverify the immutable release folder after a session, rather than expecting the writable runtime copy to keep an unchanged inventory.

## Establish actual game readiness

Use the registered tool schemas from the connected server; the following is the operation order, not a guessed client configuration format.

1. `sn2_target_processes`: choose the exact Shipping instance for the configured game. Preserve its returned descriptor. The `pid`, executable and `creation_time` identify the instance; creation time may be returned as decimal Windows FILETIME. The selector also accepts ISO UTC descriptors from the process scripts. A PID alone is insufficient.
2. Read the selected DLL's hash from the verified release manifest, or the successful source-build receipt. Call `sn2_native_load(target_process, dll_path, expected_dll_sha256)`. Preserve its result; an unknown outcome must be reconciled before a retry.
3. Call `sn2_native_status(target_process)` and obtain the actual `session_id`. Call `sn2_native_enable(target_process, expected_native_session_id)` with that identity. Poll read-only status until `ready: true` or a concrete diagnostic/timeout. Transport connection, discovery and Tick executor readiness are separate states.
4. Call `sn2_service_enable` with the selected descriptor and a nonempty package correlation label derived from the chosen package/manifest. `source_revision` may use the verified package's source revision. Labels are not byte verification. Then inspect `sn2_service_status`: `available: true` requires the expected live actor. A compiled/mounted package alone does not establish service behavior.
5. Inspect exact reflected metadata before invoking functions. Acquire fresh handles after reconnect, reflection/world change or process restart. Do not fabricate object paths, signatures or property layouts from earlier reports.

The unchanged companion built for the earlier 0.7.0 service specification was verified with frontend/native 0.9.0. A differing version label is not an automatic rejection. Actual file identity and ABI/function checks determine compatibility.

## Build the frontend and native bridge from source

### Prerequisites and layout

- Windows x64, installed Python 3.11+ and `uv`.
- Git and CMake **3.24+**, with `cmake` and `ctest` discoverable.
- Visual Studio 2022 or Build Tools 2022 with **Desktop development with C++**, MSVC x64/x86 tools and a Windows SDK. The native build uses the `Visual Studio 17 2022` generator and x64 architecture.
- Network access to declared dependencies on the first build, or the workflow's verified dependency cache.

Clone [VardoKen/SN2-MCP](https://github.com/VardoKen/SN2-MCP.git) into the chosen source directory. Inspect the selected ref and working diff. Do not rely on a sibling coordination repository; the bridge builds its own implementation. The root directories are `src/`, `native/`, `development/`, `tools/`, and `tests/`. Keep build outputs in ignored `build/`/`artifacts/` and mod work in a separately chosen workspace.

```powershell
git clone https://github.com/VardoKen/SN2-MCP.git 'C:\SN2MCP\source'
```

The Python dependency source is `pyproject.toml`/`uv.lock`: MCP **2.3.0**, build backend hatchling **1.27.0**. Native dependencies are nlohmann JSON **v3.12.0** and MinHook commit **`8af6b4acae5a9388fd742b56fa79ece89d96f823`**. BLAKE3 source/version and licenses are recorded under `native/vendor/blake3`; CityHash attribution remains in its source. Preserve these notices. A normal native build does not require Unreal headers, Modkit, FMOD or game files.

### Compile and verify

Run from the repository root. Choose a new wheel-output directory for each candidate:

```powershell
$sn2Repository = 'C:\SN2MCP\source'
Set-Location $sn2Repository
uv sync --frozen
$sn2RepoPython = Join-Path $sn2Repository '.venv\Scripts\python.exe'
& (Join-Path $sn2Repository 'tools\build_native.ps1') -Configuration Release -Target all -TimeoutSeconds 1200 -Json
uv build --wheel --out-dir (Join-Path $sn2Repository 'artifacts\wheels-local')
```

Check exit codes before continuing. `build_native.ps1` runs the existing managed workflow, captures source/dependency inputs, configures/builds and runs CTest by default. Keep production `SN2_MCP_DEVELOPMENT_PROBES=OFF`; development fixtures are not a release DLL. Do not pass `-SkipTests` for a release candidate. An unfinished build may continue as a worker; inspect its exact receipt/process and wait for the final state rather than calling the same build again blindly.

Successful outputs are:

- `artifacts/builds/<build-id>/receipt.json`, with final state, actual compiler/dependency identities, build/test output and publication result.
- Captured source and binary directories under `build/mcp/<input-digest>/`.
- Published `build/native/Release/SN2_MCP.dll` and `sn2_mcp_loader.exe` for normal runtime loading.
- `artifacts/wheels-local/sn2_mcp-0.9.0-py3-none-any.whl` for an unchanged 0.9.0 source version.

Inspect `state: succeeded`, the required test result, matching source capture and published hashes. Compilation proves the local components, not compatibility with the game. For frontend changes, run the affected neutral suites, for example:

```powershell
& $sn2RepoPython -m unittest discover -s tests -p test_subnautica_catalog.py -v
& $sn2RepoPython -m unittest discover -s tests -p test_runtime_authoring.py -v
& $sn2RepoPython -m unittest discover -s tests -p test_content_projects.py -v
& $sn2RepoPython -m unittest discover -s tests -p test_bridge_bundle.py -v
```

These commands do not start the game. Historical donor-only tests are source provenance; they are not automatically Subnautica acceptance. Run checks appropriate to the change instead of repeating every live probe. The prior 0.9.0 native candidate passed **48/48 CTest**; a new build must report its own result.

For a source-based runtime, set `--runtime-root` to the repository containing the published native outputs and fixed tools, and use a separate `--workspace-root`. Use the repository `.venv` Python for the MCP command. The freshly compiled bridge may reuse the verified cooked companion from a release, or build it using the next section.

## Build the companion from its source specification

This is a separate **Editor author/cook operation**, not runtime compilation inside the game. `development/services/SN2MCPDevelopmentServices/Authoring/service.json` is the companion specification; `development/authoring/SN2MCPAuthoring` contains the helper used to author editable assets. The build script copies/captures inputs, builds the helper, authors Blueprint assets, cooks the plugin and produces a verified package. It does not install the plugin or start the game.

### Obtain the compatible external environment

| Input | Verified source/checkpoint and required layout |
| --- | --- |
| Modkit | [Subnautica2Modding/Subnautica2-Project](https://github.com/Subnautica2Modding/Subnautica2-Project), revision `827162cc3985814f8bfa74ce6e6098628df35978`, CL131439. Select the root containing `Subnautica2.uproject`, `Plugins`, `Releases/SN2Base/Windows/AssetRegistry.bin`, and `Binaries/Win64/Subnautica2Editor.target`. |
| Custom Engine | [Subnautica 2 Custom Engine V4](https://github.com/Buckminsterfullerene02/UnrealEngine/releases/tag/sn2-v0.1.2.0), tag `sn2-v0.1.2.0`, source commit `6a081fc6282cb3bbfb806e3a8bb84fc4eb58acb9`, UE5.6.1. Select the inner `Engine` root containing `Build/Build.version` and `Binaries/Win64/UnrealEditor-Cmd.exe`. Stock UE5.6 has not been established as a substitute. |
| Game reference | Installed compatible game root; it must contain `Subnautica2/Content/Paks` with the game's container references. Do not copy game content into the source repository or publish it. |
| Helper tools | The engine's bundled .NET **8.0.300**, UnrealBuildTool, AutomationTool and batch build entry points; VS2022 C++/Windows SDK and discoverable .NET Framework SDK. The checked helper used MSVC **14.44.35207** toolchain / compiler **14.44.35228**, Windows SDK **10.0.22621.0** and NETFXSDK **4.6.2**. |
| FMOD | [FMOD 2.03 Unreal integration](https://fmod.com/docs/2.03/unreal), version **2.03.09 for UE5.6 Windows x64**. Preserve the Modkit/custom Engine's existing Editor DLLs and `.modules` metadata. |

The custom Engine download may require the operator's Epic/GitHub access. Obtain the upstream release parts through authorized access, keep all `.zip.001` through `.zip.004` together, verify published checksums, and extract the multipart archive with a compatible tool such as 7-Zip. The archive has an inner `Engine` directory. Acquire missing tools/vendor files from their actual upstream sources; ask for an unavailable required account or package rather than inventing downloads or changing global settings without authority.

For the recorded Modkit checkpoint, create a separate checkout and select the exact known revision; do not silently replace a newer operator checkout:

```powershell
git clone https://github.com/Subnautica2Modding/Subnautica2-Project.git 'C:\SN2Modding\Modkit'
git -C 'C:\SN2Modding\Modkit' checkout 827162cc3985814f8bfa74ce6e6098628df35978
```

Then confirm the required prebuilt files from the table. If the upstream checkout needs additional authorized release inputs, obtain those inputs before authoring; the source revision alone does not create missing Editor binaries. If prerequisite setup is within the assignment, install the declared C++ workload/Windows SDK and the [.NET Framework 4.6.2 Developer Pack](https://dotnet.microsoft.com/en-us/download/dotnet-framework/net462), or use an already compatible discoverable SDK. Do not invoke a global installer as a local extraction shortcut.

For FMOD, the verified external package supplied these native files under `Plugins/FMODStudio/Binaries/Win64`: `fmod.dll`, `fmodL.dll`, `fmodstudio.dll`, `fmodstudioL.dll`, `resonanceaudio.dll`, `fmod_vc.lib`, `fmodL_vc.lib`, `fmodstudio_vc.lib`, `fmodstudioL_vc.lib`. The logging DLLs resolved the checked Editor startup failure. Import libraries and the DSP DLL have conditional roles; a warning about an absent optional file is not proof that every cook needs it. Copy the corresponding native dependency files from the authorized FMOD package; do not replace the Modkit's custom `UnrealEditor-FMODStudio*.dll`, `UnrealEditor.modules`, descriptor or source with stock-package versions. The checked custom module BuildId was `a0f35de5-25c7-44b8-87b3-4fe5671dcf5c`; the stock FMOD package modules used `43139311` and are not interchangeable by renaming that value.

Keep the acquired Engine/Modkit outside the bridge checkout. The build tooling uses captured working trees and process-local `NoZenLocalFallback` DDC with workspace `Cache/DDC`; no engine registration, global DDC rewrite or original Modkit/game configuration edit is needed. Runtime asset creation without these external tools remains available through the installed bridge; it is a different authoring path.

### Inspect, author, cook and package

Use the previously installed source `.venv`, an existing separate writable workspace, and the actual roots:

```powershell
$sn2Workspace = 'C:\SN2MCP\workspace-build'
$sn2Modkit = 'C:\SN2Modding\Modkit'
$sn2Engine = 'C:\SN2Modding\Engine'
$sn2Game = 'C:\Games\Subnautica 2'
New-Item -ItemType Directory -Path $sn2Workspace -Force | Out-Null
& $sn2RepoPython (Join-Path $sn2Repository 'tools\build_development_services.py') --workspace-root $sn2Workspace --modkit-root $sn2Modkit --engine-root $sn2Engine --game-root $sn2Game --project-path projects/SN2MCPDevelopmentServices --package-path packages/SN2MCPDevelopmentServices --timeout-seconds 3600
```

Use an absent project/package destination for a new source revision, or reuse only the identical source admitted by the tool. The final companion receipt must report `state: succeeded`, `compiled: true`, `package_verified: true`, actual `package_root`, `package_manifest_sha256` and `source_revision`. Inspect any failure receipt/log instead of treating an existing output directory as success. `sn2_content_environment` and `sn2_authoring_recipe` on a source-configured frontend can inspect dependencies and the exact project recipe. Environment readiness is permission to attempt author/cook, not acceptance of its output.

For those MCP environment/recipe calls, run the source frontend with explicit `--modkit-root $sn2Modkit --engine-root $sn2Engine` in addition to the workspace, runtime and game roots. Otherwise its default external roots may point to a different installation.

Install the resulting **outer verified package** with `sn2_verify_artifact_package` and `sn2_mod_install` at the stopped-game boundary; then perform actual native/service readiness checks. A successful cook does not prove installation, plugin discovery, function behavior or standalone user-mod logic. If only the native/frontend changed, an unchanged compatible released companion can be reused; do not recook solely to make a version label match.

## Prepare a runtime distribution from your build

From the source repository, select the exact successful wheel and companion package. Preserve third-party notices for the actual selected dependency sources. Locate notice files in the successful build receipt: fetched sources usually live in the staged build's `_deps`, and reused sources live in the verified `build/dc` cache. The legacy `build/native/_deps` directory may also be present, but a fresh build does not promise that path.

```powershell
$sn2Wheel = Join-Path $sn2Repository 'artifacts\wheels-local\sn2_mcp-0.9.0-py3-none-any.whl'
$sn2Service = Join-Path $sn2Workspace 'packages\SN2MCPDevelopmentServices'
$sn2JsonNotice = 'C:\path-from-build-receipt\json-src\LICENSE.MIT'
$sn2MinHookNotice = 'C:\path-from-build-receipt\minhook-src\LICENSE.txt'
& $sn2RepoPython (Join-Path $sn2Repository 'tools\prepare_bridge_bundle.py') --destination (Join-Path $sn2Repository 'artifacts\runtime-local') --consumer-wheel $sn2Wheel --service-package $sn2Service --json-license $sn2JsonNotice --minhook-license $sn2MinHookNotice
& $sn2RepoPython (Join-Path $sn2Repository 'tools\prepare_bridge_bundle.py') --verify-root (Join-Path $sn2Repository 'artifacts\runtime-local')
```

Replace the notice placeholders with actual files for the selected dependency revision. Destination must be new and inside the repository's ignored `artifacts/`. The bundle verifier checks file/service/source correlation, not live-game acceptance. Test a writable copy with the actual use cases relevant to the change. Keep acceptance harnesses as explicit development checks, not automatic installation steps.

## Source changes, updates and cleanup

Keep code in this repository and use English comments, diagnostics and documentation. Follow the selected source/version contracts; do not renumber an unchanged bridge for a packaging/documentation-only edit. Preserve original source/dependency provenance and actual build receipts. A minor development release adds substantive capabilities; a patch fixes the tested version. Major changes require an operator decision.

Before committing, inspect the complete diff, run relevant checks and exclude local game data, credentials, build output and logs. Commit one complete task per affected repository. Push to the configured remote when the operator's publication/update authorization covers the task; otherwise obtain that authorization. Do not rewrite history or discard unrelated work. Source archives, wheel builds, runtime ZIPs, installation and external publication are separate results.

Complete cleanup after checks: keep the selected deliverable and required evidence, and move your confirmed unnecessary temporary files to the project's trash location without repeated confirmation. Preserve original relative paths, do not overwrite prior trash, and never empty it automatically. Stop temporary development processes that are no longer needed; do not stop unrelated processes.
