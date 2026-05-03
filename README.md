# mcp-hub

Lightweight MCP proxy with a web UI for turning servers on/off. No database — just `config.json`.

Inspired by [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) and the server-management UI of MetaMCP, minus the weight.

## What it does

- Reads `config.json` (Claude Desktop format with an `enabled` flag per server).
- For each enabled MCP server, spawns it as a stdio subprocess and holds the session open.
- Exposes each server over **Streamable HTTP** at `http://<host>:<port>/mcp/<name>`.
- Serves a single-page web UI at `http://<host>:<port>/` to flip each server on/off, restart, and copy its URL.

You set the MCPs up yourself in `config.json`. The hub only manages lifecycle.

## Install

```powershell
# from C:\mcp\hub
uv pip install --system -e .
# or, without installing:
uv pip install --system mcp starlette uvicorn
```

## Run

```powershell
# Windows convenience launcher (creates config.json from example on first run):
.\run.bat

# Or directly:
python -m mcp_hub --config .\config.json
```

Open <http://127.0.0.1:3737> in a browser.

## Config

`config.json` follows Claude Desktop's `mcpServers` format with an extra `enabled` field. **Servers default to disabled** — turn them on from the UI (or set `"enabled": true`) when you actually want them running.

```json
{
  "host": "127.0.0.1",
  "port": 3737,
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:\\Users\\won0\\Documents"],
      "env": {}
    }
  }
}
```

The UI's on/off toggle writes the `enabled` field back to this file. Editing the file directly works too — click **Reload config** in the UI.

## Connecting a client

Each enabled server is reachable at `/mcp/<name>` over Streamable HTTP. Example for Claude Code:

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

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | Web UI |
| GET | `/api/servers` | List servers + status |
| POST | `/api/servers/{name}/toggle` | Body `{"enabled": bool}` |
| POST | `/api/servers/{name}/restart` | Restart upstream |
| GET | `/api/config` | Current config file contents |
| POST | `/api/reload` | Re-read config.json |
| GET/POST/DELETE | `/mcp/{name}` | Streamable HTTP MCP endpoint |

## Limits

- No SSE transport (Streamable HTTP only). Add if a client you use still requires SSE.
- No notification forwarding (`tools/list_changed` etc.) — restart the server if its surface changes.
- No auth. Bind to `127.0.0.1` (the default) and don't expose it.
