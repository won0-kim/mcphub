# mcp-hub

Lightweight MCP proxy with a web UI for managing multiple `mcp.json` profiles. No database — config + per-project sidecars on disk.

You keep your MCP server definitions in plain `mcp.json` files (one per project/profile). The hub spawns the enabled ones, exposes them over HTTP at `/mcp/<server>`, and auto-syncs a hub-URL version of the file into a target path of your choice (so an agent reads from a single file you don't have to hand-edit).

Inspired by [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) and the server-management UI of MetaMCP, minus the weight.

## Storage layout

```
config.json                       # hub-only: host, port, public_url, active, client_format default
projects/<name>.json              # standard mcp.json — `{"mcpServers": {...}}`. Copy directly into an agent if you want.
projects/<name>.meta.json         # hub-only sidecar: { enabled: [...], target: "...", client_format: "..." }
predefined.json                   # editable catalog shown in "+ Add server → From template"
```

The split is intentional: `projects/<name>.json` stays a pure mcp.json artifact (no `enabled` / `target` pollution) so it's portable. All hub-side state for that project lives next to it in `<name>.meta.json`.

## Install

```bash
uv pip install --system -e .
# or, without installing:
uv pip install --system mcp starlette uvicorn
```

## Run

```powershell
# Windows
.\run.bat
```

```bash
# Linux / macOS
chmod +x run.sh && ./run.sh

# Or directly (any platform)
python -m mcp_hub --config ./config.json
```

Open <http://127.0.0.1:3737>.

CLI flags:

- `--config <path>` — root config (default `./config.json`)
- `--projects-dir <path>` — where projects + sidecars live (default `<config>/../projects`)
- `--predefined <path>` — catalog file (default `<config>/../predefined.json`)
- `--host` / `--port` — override bind once (otherwise read from `config.json`)

## Concepts

| Term | What it is |
| --- | --- |
| **Project** | A self-contained set of MCP server definitions = one `projects/<name>.json` file, plus its sidecar. The UI's header dropdown switches between projects. |
| **Active project** | The one the hub is currently running (enabled servers spawned, target file kept in sync). Switch via the picker. |
| **Target** | A path the hub auto-rewrites with the hub-URL version of the active project whenever something changes. Typical targets: `~/.claude.json`, `~/.codex/config.toml`, `<repo>/.mcp.json`. Hub merges into existing files — only `mcpServers` (or `[mcp_servers.*]` tables) are replaced; everything else is preserved. |
| **Client format** | Per project — `claude_code` (`mcp.json`) or `codex` (`config.toml`). Determines both the in-UI "Show config" view and how target files are written. The Settings dialog only sets the **default** for new projects; each project keeps its own value. |
| **Public URL** | The base URL written into generated configs. Defaults to whatever URL the agent connected on; set it explicitly when the hub is reachable from clients via a different host (LAN address, reverse proxy, etc.). |

## UI cheatsheet

- **Header** — project picker + `New` / `Copy` / `Rename` / `Delete`. Right side: effective base URL + ⚙ Settings.
- **Toolbar** — `+ Add server`, `Show mcp.json` (label flips to `Show config.toml` for codex projects), and the inline format radio.
- **Target bar** — the project's target path + `Set` to set/clear it.
- **Server cards** — each enabled server has a card with status, capability counts, copy-URL, edit, delete, restart.

## "+ Add server" sources

- **Manual** — fill the form yourself (default tab).
- **From template** — pick from `predefined.json`, fields are pre-filled into the Manual form for editing.

## Connecting an agent

Each enabled server in the active project is reachable at `/mcp/<server>` over Streamable HTTP. Two ways to wire it up:

1. **Set a target** — give the hub a path to your agent's mcp.json/config.toml; the hub keeps it in sync. You never edit it by hand.
2. **Manual paste** — open `Show mcp.json` (or `Show config.toml`), copy, paste into your agent's config. You'll need to repeat after every toggle change.

Example (manual paste, Claude Code):

```json
{
  "mcpServers": {
    "filesystem-via-hub": {
      "type": "http",
      "url": "http://127.0.0.1:3737/mcp/filesystem"
    }
  }
}
```

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web UI |
| GET | `/api/servers` | List servers in the active project |
| POST | `/api/servers` | Add server to the active project (body: `{name, spec}`) |
| PUT | `/api/servers/{name}` | Edit / rename a server |
| DELETE | `/api/servers/{name}` | Remove a server |
| POST | `/api/servers/{name}/toggle` | `{enabled: bool}` |
| POST | `/api/servers/{name}/restart` | Restart upstream |
| GET | `/api/mcp-files` | List projects + per-project status (target, format, ...) |
| POST | `/api/mcp-files` | Create a project (`{name, mcpJson?}`) |
| POST | `/api/mcp-files/active` | Switch active (`{name}`) |
| PUT | `/api/mcp-files/{name}` | Replace a project's mcp.json (`{mcpJson}`) |
| DELETE | `/api/mcp-files/{name}` | Delete a project (active falls back to next; recreates empty `default` if last) |
| POST | `/api/mcp-files/{name}/copy` | Duplicate (`{name: <new>}`) |
| POST | `/api/mcp-files/{name}/rename` | Rename (`{name: <new>}`) |
| GET | `/api/mcp-files/{name}/raw` | Raw on-disk file content |
| PUT | `/api/mcp-files/{name}/target` | Set/clear target path (`{path}`) |
| PUT | `/api/mcp-files/{name}/format` | Set per-project client format (`{client_format}`) |
| POST | `/api/mcp-files/{name}/sync` | Force write target now |
| POST | `/api/mcp-files/load` | Import an existing mcp.json from a server-side path: creates a project, enables every server, sets that path as the target (`{path, name?}`) |
| GET | `/api/predefined` | Read `predefined.json` (re-read on every request) |
| GET / PUT | `/api/settings` | Hub settings: `public_url`, `client_format` default. (`host`/`port` are read-only; edit `config.json` and restart.) |
| GET | `/api/view-mcp-json` | Hub-URL config in the active project's format (mcp.json or config.toml) |
| GET / POST / DELETE | `/mcp/{name}` | Streamable HTTP MCP endpoint that proxies to the upstream server |

## Editing `predefined.json`

The "From template" tab is loaded fresh from `predefined.json` on every request — edit the file and refresh the modal.

The schema mirrors a standard mcp.json, with an optional per-server `description`:

```json
{
  "mcpServers": {
    "github": {
      "description": "GitHub API access",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": { "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..." }
    },
    "remote-http": {
      "description": "Connect to an existing Streamable HTTP MCP server",
      "type": "http",
      "url": "http://127.0.0.1:13337/mcp"
    }
  }
}
```

Picking a template fills the Manual form with these values; you edit and submit.

## Limits

- Only Streamable HTTP and SSE upstream transports plus stdio. Hub serves Streamable HTTP only.
- No notification forwarding (`tools/list_changed` etc.) — restart a server if its surface changes.
- No auth. Bind to `127.0.0.1` (the default) and don't expose it. If you need a public hub, put it behind a reverse proxy and set `public_url` accordingly.
