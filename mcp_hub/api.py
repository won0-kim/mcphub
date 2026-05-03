from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from .config import ConfigStore
from .manager import HubManager, RuntimeServer

logger = logging.getLogger(__name__)


def _server_to_dict(rt: RuntimeServer) -> dict:
    return {
        "name": rt.name,
        "command": rt.cfg.command,
        "args": list(rt.cfg.args),
        "env_keys": sorted(rt.cfg.env.keys()),
        "cwd": rt.cfg.cwd,
        "enabled": rt.cfg.enabled,
        "status": rt.status.status,
        "error": rt.status.error,
        "server_info": rt.status.server_info,
        "capabilities": rt.status.capabilities,
        "tool_count": rt.status.tool_count,
        "prompt_count": rt.status.prompt_count,
        "resource_count": rt.status.resource_count,
        "mcp_url": f"/mcp/{rt.name}",
    }


def create_app(config_path: Path) -> Starlette:
    store = ConfigStore(config_path)
    manager = HubManager()

    @asynccontextmanager
    async def lifespan(app: Starlette):
        store.reload()
        await manager.sync(store.config.mcpServers)
        try:
            yield
        finally:
            await manager.stop_all()

    async def index(request: Request) -> Response:
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def list_servers(request: Request) -> Response:
        return JSONResponse({"servers": [_server_to_dict(rt) for rt in manager.list()]})

    async def toggle_server(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        enabled = bool(body.get("enabled"))
        try:
            store.set_enabled(name, enabled)
            rt = await manager.set_enabled(name, enabled)
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        return JSONResponse(_server_to_dict(rt))

    async def restart_server(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            rt = await manager.restart(name)
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        return JSONResponse(_server_to_dict(rt))

    async def reload_config(request: Request) -> Response:
        cfg = store.reload()
        await manager.sync(cfg.mcpServers)
        return JSONResponse({"servers": [_server_to_dict(rt) for rt in manager.list()]})

    async def get_config(request: Request) -> Response:
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
        return JSONResponse({"path": str(config_path), "content": text})

    async def export_mcp_json(request: Request) -> Response:
        # Build a Claude Code-compatible .mcp.json that points each enabled server
        # at this hub's streamable HTTP endpoint. Base URL is taken from the request
        # so it works regardless of how the user reaches the hub.
        base = str(request.base_url).rstrip("/")
        servers: dict[str, dict] = {}
        for rt in manager.list():
            if not rt.cfg.enabled:
                continue
            servers[rt.name] = {
                "type": "http",
                "url": f"{base}/mcp/{rt.name}",
            }
        body = json.dumps({"mcpServers": servers}, indent=2, ensure_ascii=False) + "\n"
        return Response(
            body,
            media_type="application/json",
            headers={"content-disposition": 'attachment; filename="mcp.json"'},
        )

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    async def mcp_dispatch(scope, receive, send):
        # Mounted at /mcp. Starlette stores the matched prefix in scope["root_path"]
        # and leaves scope["path"] full. Subtract root_path to get our relative route.
        full_path: str = scope.get("path") or "/"
        root_path: str = scope.get("root_path") or ""
        path = full_path[len(root_path):] if root_path and full_path.startswith(root_path) else full_path
        rest = path.lstrip("/")
        if not rest:
            await _send_json(send, 404, {"error": "missing server name"})
            return
        if "/" in rest:
            name, sub_part = rest.split("/", 1)
            new_path = "/" + sub_part
        else:
            name = rest
            new_path = "/"
        rt = manager.get(name)
        if rt is None:
            await _send_json(send, 404, {"error": f"unknown server '{name}'"})
            return
        if not rt.cfg.enabled:
            await _send_json(send, 503, {"error": f"server '{name}' is disabled"})
            return
        if rt.session_manager is None or rt.status.status != "running":
            await _send_json(
                send,
                503,
                {"error": f"server '{name}' is not running", "status": rt.status.status, "detail": rt.status.error},
            )
            return
        # Rewrite scope so the streamable manager sees this request at root.
        # Move /mcp/{name} into root_path; present the remainder as the route path.
        new_scope = dict(scope)
        new_scope["root_path"] = (root_path or "") + "/" + name
        new_scope["path"] = new_path
        new_scope["raw_path"] = new_path.encode("utf-8")
        await rt.session_manager.handle_request(new_scope, receive, send)

    routes = [
        Route("/", index),
        Route("/healthz", healthz),
        Route("/api/servers", list_servers, methods=["GET"]),
        Route("/api/servers/{name}/toggle", toggle_server, methods=["POST"]),
        Route("/api/servers/{name}/restart", restart_server, methods=["POST"]),
        Route("/api/config", get_config, methods=["GET"]),
        Route("/api/reload", reload_config, methods=["POST"]),
        Route("/api/export-mcp-json", export_mcp_json, methods=["GET"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)

    # Mount MCP dispatcher as raw ASGI for /mcp/* (Streamable HTTP supports GET/POST/DELETE).
    async def mcp_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        await mcp_dispatch(scope, receive, send)

    app.router.routes.append(Mount("/mcp", app=mcp_asgi))

    return app


async def _send_json(send, status: int, body: dict) -> None:
    payload = json.dumps(body).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(payload)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
