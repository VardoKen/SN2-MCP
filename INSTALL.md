# Manual installation

This guide installs the prebuilt **0.9.0 Windows x64** release. It does not compile source or require Unreal Editor/Modkit. For source builds or agent-managed installation, use [AGENTS.md](AGENTS.md).

## 1. Get the runtime release and prerequisites

Download `SN2-MCP-0.9.0-windows-x64.zip` from [GitHub Releases](https://github.com/VardoKen/SN2-MCP/releases). Use this named asset, not GitHub's automatic Source code ZIP. Extract it into `C:\SN2MCP\release`; that folder must contain `manifest.json`, `wheels`, `tools`, `build`, and `services`.

Install or provide:

- [Python 3.11+ for Windows](https://www.python.org/downloads/windows/).
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/), available as `uv` in PowerShell.
- [Microsoft Visual C++ x64 Runtime](https://aka.ms/vs/17/release/vc_redist.x64.exe).
- Subnautica 2 for Windows x64. The verified checkpoint is UE 5.6.1 / CL131439 / build3388.

Close the game before installing or replacing the companion plugin. Keep unrelated mods and saves in place.

## 2. Install the Python frontend

Open PowerShell. Edit the paths below to match your machine. `$sn2Game` is the outer game directory containing `Subnautica2`; `$sn2BasePython` must point to a real installed Python executable.

Use separate release, runtime, workspace, and environment directories. The runtime and environment destinations below must not already exist. Keep the extracted release unchanged; working logs and receipts belong in the runtime/workspace copies.

```powershell
$sn2Release = 'C:\SN2MCP\release'
$sn2Runtime = 'C:\SN2MCP\runtime'
$sn2Workspace = 'C:\SN2MCP\workspace'
$sn2Environment = 'C:\SN2MCP\environment'
$sn2Game = 'C:\Games\Subnautica 2'
$sn2BasePython = 'C:\Python311\python.exe'

New-Item -ItemType Directory -Path $sn2Workspace -Force | Out-Null
uv venv --no-python-downloads --python $sn2BasePython $sn2Environment
uv export --frozen --no-dev --no-default-groups --no-editable --no-emit-project --format requirements.txt --project $sn2Release --output-file (Join-Path $sn2Workspace 'requirements.lock')
$sn2Python = Join-Path $sn2Environment 'Scripts\python.exe'
uv pip install --python $sn2Python --require-hashes -r (Join-Path $sn2Workspace 'requirements.lock')
uv pip install --python $sn2Python --no-deps (Join-Path $sn2Release 'wheels\sn2_mcp-0.9.0-py3-none-any.whl')
& $sn2Python -I -B (Join-Path $sn2Release 'tools\prepare_bridge_bundle.py') --verify-root $sn2Release
Copy-Item -LiteralPath $sn2Release -Destination $sn2Runtime -Recurse
```

Run each command only after the previous command succeeds. The verifier must exit with code 0 and report `verified_files: true` and `payload_complete: true`.

## 3. Install the companion plugin

With the game closed, copy this **inner** folder:

```text
C:\SN2MCP\release\services\SN2MCPDevelopmentServices\SN2MCPDevelopmentServices
```

into `<game>\Subnautica2\Mods`. The final descriptor path must be:

```text
<game>\Subnautica2\Mods\SN2MCPDevelopmentServices\SN2MCPDevelopmentServices.uplugin
```

The outer `services\SN2MCPDevelopmentServices` folder also contains verification manifests and receipts. Do not copy that outer folder into `Mods`, and do not add a second nested plugin directory. If this plugin is already installed, close the game and inspect the existing installation before replacing it.

This manual copy is a manually managed installation. To update or remove it, close the game and replace or remove only this plugin directory yourself. Bridge-managed deployment will not adopt an unrecorded manual copy; choose the agent-managed route in [AGENTS.md](AGENTS.md) if you want MCP installation receipts and managed replacement.

## 4. Connect your MCP client

Use your client's MCP server settings to supply the following **command and arguments**. The surrounding configuration file and its location depend on the client; these are not a universal JSON configuration.

**Command:** `C:\SN2MCP\environment\Scripts\python.exe`

**Arguments, as separate entries:**

```text
-I
-B
-m
sn2_mcp.server
--transport
stdio
--workspace-root
C:\SN2MCP\workspace
--runtime-root
C:\SN2MCP\runtime
--game-root
C:\Games\Subnautica 2
```

Replace the paths with those chosen in step 2. In stdio mode, the MCP client launches the frontend; do not start a second copy of that same server manually. The client should show the `sn2_` tools. Game operations become available after step 5.

If your client uses HTTP instead, start this in PowerShell and keep that terminal running:

```powershell
& $sn2Python -I -B -m sn2_mcp.server --transport streamable-http --host 127.0.0.1 --port 27224 --native-port 27223 --workspace-root $sn2Workspace --runtime-root $sn2Runtime --game-root $sn2Game
```

Connect the client to `http://127.0.0.1:27224/mcp`. Use one frontend/native session at a time; the native port is 27223, not the MCP HTTP port.

## 5. Load and enable the bridge for a game session

Start Subnautica 2 normally and wait for the main menu. In your MCP client, request this startup sequence using the installed tools:

1. `sn2_target_processes`: select the running `Subnautica2-Win64-Shipping.exe` belonging to your chosen game directory. Use its returned process descriptor; do not guess a PID or reuse one from an earlier launch.
2. `sn2_native_load`: use that descriptor, `<runtime>\build\native\Release\SN2_MCP.dll`, and the SHA-256 recorded for that DLL in the release `manifest.json`.
3. `sn2_native_status`, then `sn2_native_enable` using that descriptor and the returned `session_id`. Check status until `ready` is `true`.
4. `sn2_service_enable` using that descriptor and a package correlation label such as `SN2MCPDevelopmentServices-0.9.0-release`. Then `sn2_service_status` must report `available: true`.

You can give these steps to the AI assistant in your MCP client. They load the selected bridge and activate the already installed companion; they do not need to download files or configure the client again. Loading the DLL alone does not enable the executor. A package label is a correlation label, not a compatibility or hash check.

The bridge DLL is loaded into the selected process for that game session. After restarting the game, repeat step 5 with a new descriptor. Reconnecting a client also requires fresh object handles and session metadata. If an operation reports an unknown outcome, inspect the actual state before retrying it.

## Troubleshooting and removal

| Symptom | Check |
| --- | --- |
| `sn2_` tools are missing | The configured Python executable and command arguments; frontend startup output. |
| `native_unavailable` | The selected game is running and its DLL load completed; native TCP uses 27223. |
| Native status is not ready | Enable the current native session and let the game reach its menu/Tick. |
| Companion is unavailable | The `.uplugin` path from step 3, compatible package, a game restart after copying it, and service activation. |
| `unknown_installation` | A manual plugin copy has no managed receipt. Use the manual update/removal path rather than retrying managed installation. |
| A different game build fails | Record the game build and diagnostic; this release has not established compatibility with every update. |

To remove the bridge, close the game, remove its MCP entry from your client, and remove the manually installed `SN2MCPDevelopmentServices` plugin directory. You may then remove your chosen runtime/environment directories. Keep your mod workspace if you want to retain authored mods and receipts. Existing standalone user mods are separate installations.

Managed replacement/removal preserves selected plugin files in `.trash` under the runtime's parent directory. Receipts identify the moved files; only the user empties that trash.
