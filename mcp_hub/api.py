from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Mount, Route

from .config import ConfigStore, ServerConfig
from .formats import merge_target_text, serialize_for_view, target_filename_hint
from .manager import HubManager, RuntimeServer
from .predefined import load_predefined

logger = logging.getLogger(__name__)


def _server_to_dict(rt: RuntimeServer, base_url: str = "") -> dict:
    return {
        "name": rt.name,
        "type": rt.cfg.type,
        "command": rt.cfg.command,
        "args": list(rt.cfg.args),
        "env": dict(rt.cfg.env),
        "env_keys": sorted(rt.cfg.env.keys()),
        "cwd": rt.cfg.cwd,
        "url": rt.cfg.url,
        "headers": dict(rt.cfg.headers),
        "enabled": rt.cfg.enabled,
        "status": rt.status.status,
        "error": rt.status.error,
        "server_info": rt.status.server_info,
        "capabilities": rt.status.capabilities,
        "tool_count": rt.status.tool_count,
        "prompt_count": rt.status.prompt_count,
        "resource_count": rt.status.resource_count,
        "mcp_url": f"{base_url}/mcp/{rt.name}" if base_url else f"/mcp/{rt.name}",
    }


def _server_config_from_body(body: dict) -> ServerConfig:
    return ServerConfig.from_mcp_json(body)


def create_app(store: ConfigStore, predefined_path: Path | None = None) -> Starlette:
    manager = HubManager()
    bound_host = store.settings.host
    bound_port = store.settings.port
    predefined_path = predefined_path or (store.root_path.parent / "predefined.json")

    def _build_hub_mcp_servers(base_url: str) -> dict:
        servers: dict[str, dict] = {}
        for rt in manager.list():
            if not rt.cfg.enabled:
                continue
            servers[rt.name] = {"type": "http", "url": f"{base_url}/mcp/{rt.name}"}
        return servers

    def _sync_target(base_url: str, file_name: str | None = None) -> tuple[bool, str | None]:
        """Write the hub-URL config into the file's target path, if set.
        Format is per-project (sidecar) — falls back to hub default if unset.
        Returns (synced, error)."""
        name = file_name or store.settings.active
        path_str = store.get_target(name)
        if not path_str:
            return False, None
        target_path = Path(path_str)
        fmt = store.get_client_format(name)
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            existing_text = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
            new_text = merge_target_text(fmt, existing_text, _build_hub_mcp_servers(base_url))
            target_path.write_text(new_text, encoding="utf-8")
            logger.info("[target] synced %s (%s) -> %s", name, fmt, target_path)
            return True, None
        except Exception as e:  # noqa: BLE001
            logger.warning("[target] sync failed for %s -> %s: %s", name, target_path, e)
            return False, str(e)

    def _base_url(request: Request) -> str:
        if store.settings.public_url:
            return store.settings.public_url.rstrip("/")
        return str(request.base_url).rstrip("/")

    @asynccontextmanager
    async def lifespan(app: Starlette):
        await manager.sync(store.active_servers())
        try:
            yield
        finally:
            await manager.stop_all()

    # ---- static UI ---------------------------------------------------------

    async def index(request: Request) -> Response:
        html = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"ok": True})

    # ---- runtime / servers (act on the ACTIVE mcp.json) -------------------

    async def list_servers(request: Request) -> Response:
        return JSONResponse(
            {
                "servers": [_server_to_dict(rt, _base_url(request)) for rt in manager.list()],
                "active": store.settings.active,
                "active_path": str(store.active_path()),
            }
        )

    async def toggle_server(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        enabled = bool(body.get("enabled"))
        try:
            store.set_enabled(name, enabled)
            rt = await manager.set_enabled(name, enabled)
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        _sync_target(_base_url(request))
        return JSONResponse(_server_to_dict(rt, _base_url(request)))

    async def restart_server(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            rt = await manager.restart(name)
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        return JSONResponse(_server_to_dict(rt, _base_url(request)))

    async def add_server(request: Request) -> Response:
        body = await request.json()
        name = body.get("name") or ""
        spec = body.get("spec") or body
        try:
            cfg = _server_config_from_body(spec)
            store.add_server(name, cfg)
            await manager.sync(store.active_servers())
        except (ValueError, KeyError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _sync_target(_base_url(request))
        return JSONResponse({"ok": True, "name": name})

    async def update_server(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        new_name = body.get("name") or None
        spec = body.get("spec") or {k: v for k, v in body.items() if k != "name"}
        try:
            cfg = _server_config_from_body(spec)
            final = store.update_server(name, cfg, new_name=new_name)
            await manager.sync(store.active_servers())
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _sync_target(_base_url(request))
        return JSONResponse({"ok": True, "name": final})

    async def delete_server(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            store.delete_server(name)
            await manager.sync(store.active_servers())
        except KeyError:
            return JSONResponse({"error": f"unknown server '{name}'"}, status_code=404)
        _sync_target(_base_url(request))
        return JSONResponse({"ok": True})

    # ---- mcp.json files ---------------------------------------------------

    async def list_mcp_files(request: Request) -> Response:
        items = []
        for name in store.list_files():
            try:
                servers = store.get_servers(name)
                items.append(
                    {
                        "name": name,
                        "server_count": len(servers),
                        "enabled_count": sum(1 for s in servers.values() if s.enabled),
                        "active": name == store.settings.active,
                        "path": str(store.projects_dir / f"{name}.json"),
                        "target": store.get_target(name),
                        "client_format": store.get_client_format(name),
                    }
                )
            except Exception as e:  # noqa: BLE001
                items.append(
                    {
                        "name": name,
                        "error": str(e),
                        "active": name == store.settings.active,
                        "target": store.get_target(name),
                        "client_format": store.get_client_format(name),
                    }
                )
        active = store.settings.active
        return JSONResponse(
            {
                "files": items,
                "active": active,
                "active_target": store.get_target(active),
                "active_client_format": store.get_client_format(active),
                "hub": {"host": store.settings.host, "port": store.settings.port},
                "base_url": _base_url(request),
                "public_url": store.settings.public_url,
                "client_format_default": store.settings.client_format,
                "projects_dir": str(store.projects_dir),
            }
        )

    async def create_mcp_file(request: Request) -> Response:
        body = await request.json()
        name = (body.get("name") or "").strip()
        content = body.get("mcpJson")
        try:
            store.create_file(name, content)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "name": name})

    async def replace_mcp_file(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        try:
            store.replace_file(name, body.get("mcpJson"))
            if name == store.settings.active:
                await manager.sync(store.active_servers())
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if name == store.settings.active:
            _sync_target(_base_url(request))
        return JSONResponse({"ok": True, "name": name})

    async def delete_mcp_file(request: Request) -> Response:
        name = request.path_params["name"]
        was_active = name == store.settings.active
        try:
            store.delete_file(name)
        except KeyError:
            return JSONResponse({"error": f"unknown mcp.json '{name}'"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        if was_active:
            # Active rolled over to another profile; sync the manager to it.
            await manager.sync(store.active_servers())
            _sync_target(_base_url(request))
        return JSONResponse({"ok": True, "active": store.settings.active})

    async def copy_mcp_file(request: Request) -> Response:
        src = request.path_params["name"]
        body = await request.json()
        dst = (body.get("name") or "").strip()
        try:
            store.copy_file(src, dst)
        except KeyError:
            return JSONResponse({"error": f"unknown mcp.json '{src}'"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "name": dst})

    async def rename_mcp_file(request: Request) -> Response:
        old = request.path_params["name"]
        body = await request.json()
        new = (body.get("name") or "").strip()
        try:
            store.rename_file(old, new)
        except KeyError:
            return JSONResponse({"error": f"unknown mcp.json '{old}'"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # If we just renamed the active file, the manager's runtime entries are
        # tied to server names not file names, so no manager.sync() needed.
        return JSONResponse({"ok": True, "name": new})

    async def set_active_mcp_file(request: Request) -> Response:
        body = await request.json()
        name = body.get("name") or ""
        try:
            servers = store.set_active(name)
            await manager.sync(servers)
        except KeyError:
            return JSONResponse({"error": f"unknown mcp.json '{name}'"}, status_code=404)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        _sync_target(_base_url(request))
        return JSONResponse({"ok": True, "active": name})

    async def get_mcp_file_raw(request: Request) -> Response:
        name = request.path_params["name"]
        try:
            text = store.get_raw(name)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"name": name, "content": text})

    # ---- mcp.json view ---------------------------------------------------

    async def view_mcp_json(request: Request) -> Response:
        """The hub-URL config for enabled servers in the active profile,
        in that profile's client_format (mcp.json or config.toml)."""
        fmt = store.get_client_format(store.settings.active)
        body, media_type = serialize_for_view(fmt, _build_hub_mcp_servers(_base_url(request)))
        headers = {}
        if request.query_params.get("download") in ("1", "true"):
            headers["content-disposition"] = f'attachment; filename="{target_filename_hint(fmt)}"'
        return Response(body, media_type=media_type, headers=headers)

    async def get_target(request: Request) -> Response:
        name = request.path_params["name"]
        return JSONResponse({"name": name, "target": store.get_target(name)})

    async def set_target(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        path = (body.get("path") or "").strip() or None
        store.set_target(name, path)
        synced, err = (False, None)
        if path and name == store.settings.active:
            synced, err = _sync_target(_base_url(request))
        return JSONResponse(
            {"ok": True, "name": name, "target": store.get_target(name), "synced": synced, "error": err}
        )

    async def load_mcp_from_path(request: Request) -> Response:
        """Import an existing mcp.json from disk: read it, create a new profile
        with all servers enabled, set the target to that path, and switch active."""
        body = await request.json()
        path_str = (body.get("path") or "").strip()
        if not path_str:
            return JSONResponse({"error": "path is required"}, status_code=400)
        src = Path(path_str)
        if not src.exists():
            return JSONResponse({"error": f"file not found: {src}"}, status_code=400)
        try:
            content = src.read_text(encoding="utf-8")
        except OSError as e:
            return JSONResponse({"error": f"failed to read: {e}"}, status_code=400)

        base = (body.get("name") or _derive_profile_name(path_str)).strip() or "imported"
        name = base
        n = 2
        while (store.projects_dir / f"{name}.json").exists():
            name = f"{base}-{n}"
            n += 1

        try:
            store.create_file(name, content)
        except (ValueError, json.JSONDecodeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        # Mark every imported server enabled, set target, switch active.
        store.enable_all(name)
        store.set_target(name, path_str)
        store.set_active(name)
        await manager.sync(store.active_servers())
        synced, err = _sync_target(_base_url(request), name)
        return JSONResponse(
            {"ok": True, "name": name, "target": path_str, "synced": synced, "error": err}
        )

    async def set_project_format(request: Request) -> Response:
        name = request.path_params["name"]
        body = await request.json()
        fmt = body.get("client_format")
        try:
            store.set_client_format(name, fmt)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # If this is the active project, re-sync target with the new format.
        if name == store.settings.active:
            _sync_target(_base_url(request))
        return JSONResponse(
            {"ok": True, "name": name, "client_format": store.get_client_format(name)}
        )

    async def sync_target_now(request: Request) -> Response:
        name = request.path_params.get("name") or store.settings.active
        if not store.get_target(name):
            return JSONResponse({"ok": False, "error": "no target set for this mcp.json"}, status_code=400)
        if name != store.settings.active:
            return JSONResponse({"ok": False, "error": "target sync only writes the active mcp.json"}, status_code=400)
        synced, err = _sync_target(_base_url(request), name)
        if not synced:
            return JSONResponse({"ok": False, "error": err}, status_code=500)
        return JSONResponse({"ok": True, "target": store.get_target(name)})

    async def get_predefined(request: Request) -> Response:
        # Re-read on each request so users can edit predefined.json without
        # restarting the hub.
        return JSONResponse(
            {"predefined": load_predefined(predefined_path), "path": str(predefined_path)}
        )

    async def get_settings(request: Request) -> Response:
        return JSONResponse(
            {
                "host": store.settings.host,
                "port": store.settings.port,
                "public_url": store.settings.public_url,
                "client_format": store.settings.client_format,
                "bound": {"host": bound_host, "port": bound_port},
                "effective_base_url": _base_url(request),
            }
        )

    async def update_settings(request: Request) -> Response:
        body = await request.json()
        try:
            store.update_hub_settings(
                public_url=body.get("public_url"),
                client_format=body.get("client_format"),
            )
        except (ValueError, TypeError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        # Either change can affect the target file content; re-sync.
        _sync_target(_base_url(request))
        return JSONResponse(
            {
                "ok": True,
                "public_url": store.settings.public_url,
                "client_format": store.settings.client_format,
                "effective_base_url": _base_url(request),
            }
        )

    # ---- MCP proxy (Streamable HTTP per server) ---------------------------

    async def mcp_dispatch(scope, receive, send):
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
        new_scope = dict(scope)
        new_scope["root_path"] = (root_path or "") + "/" + name
        new_scope["path"] = new_path
        new_scope["raw_path"] = new_path.encode("utf-8")
        await rt.session_manager.handle_request(new_scope, receive, send)

    async def mcp_asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        await mcp_dispatch(scope, receive, send)

    routes = [
        Route("/", index),
        Route("/healthz", healthz),

        # active-mcp.json server CRUD + runtime
        Route("/api/servers", list_servers, methods=["GET"]),
        Route("/api/servers", add_server, methods=["POST"]),
        Route("/api/servers/{name}", update_server, methods=["PUT"]),
        Route("/api/servers/{name}", delete_server, methods=["DELETE"]),
        Route("/api/servers/{name}/toggle", toggle_server, methods=["POST"]),
        Route("/api/servers/{name}/restart", restart_server, methods=["POST"]),

        # mcp.json file collection
        Route("/api/mcp-files", list_mcp_files, methods=["GET"]),
        Route("/api/mcp-files", create_mcp_file, methods=["POST"]),
        Route("/api/mcp-files/active", set_active_mcp_file, methods=["POST"]),
        Route("/api/mcp-files/{name}", replace_mcp_file, methods=["PUT"]),
        Route("/api/mcp-files/{name}", delete_mcp_file, methods=["DELETE"]),
        Route("/api/mcp-files/{name}/copy", copy_mcp_file, methods=["POST"]),
        Route("/api/mcp-files/{name}/rename", rename_mcp_file, methods=["POST"]),
        Route("/api/mcp-files/{name}/raw", get_mcp_file_raw, methods=["GET"]),
        Route("/api/mcp-files/{name}/target", get_target, methods=["GET"]),
        Route("/api/mcp-files/{name}/target", set_target, methods=["PUT"]),
        Route("/api/mcp-files/{name}/format", set_project_format, methods=["PUT"]),
        Route("/api/mcp-files/{name}/sync", sync_target_now, methods=["POST"]),
        Route("/api/mcp-files/load", load_mcp_from_path, methods=["POST"]),

        # misc
        Route("/api/predefined", get_predefined, methods=["GET"]),
        Route("/api/view-mcp-json", view_mcp_json, methods=["GET"]),
        Route("/api/settings", get_settings, methods=["GET"]),
        Route("/api/settings", update_settings, methods=["PUT"]),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.router.routes.append(Mount("/mcp", app=mcp_asgi))
    return app


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_\-]")


def _derive_profile_name(path: str) -> str:
    stem = Path(path).stem.lstrip(".") or "imported"
    cleaned = _SAFE_NAME_RE.sub("-", stem).strip("-")
    if not cleaned or not cleaned[0].isalnum():
        cleaned = "imported"
    return cleaned


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
