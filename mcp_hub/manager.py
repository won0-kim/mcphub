"""Per-server lifecycle: spawn upstream stdio MCP, hold session, expose proxy via Streamable HTTP."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Callable, Literal

from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import ServerConfig
from .proxy import build_proxy_server

logger = logging.getLogger(__name__)

Status = Literal["stopped", "starting", "running", "error", "stopping"]


@dataclass
class ServerStatus:
    name: str
    status: Status = "stopped"
    error: str | None = None
    server_info: dict[str, Any] | None = None
    tool_count: int = 0
    prompt_count: int = 0
    resource_count: int = 0
    capabilities: dict[str, bool] = field(default_factory=dict)
    # Names of all tools the upstream advertised at session init.
    tools: list[str] = field(default_factory=list)


class RuntimeServer:
    def __init__(
        self,
        name: str,
        cfg: ServerConfig,
        get_disabled_tools: Callable[[str], set[str]] | None = None,
    ):
        self.name = name
        self.cfg = cfg
        self.status = ServerStatus(name=name)
        self.session_manager: StreamableHTTPSessionManager | None = None
        # Closure over the live store so disable changes take effect without
        # restarting the server.
        self._get_disabled_tools = (
            (lambda: get_disabled_tools(name)) if get_disabled_tools else (lambda: set())
        )

        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event = asyncio.Event()
        self._ready_event: asyncio.Event = asyncio.Event()

    def is_running(self) -> bool:
        return self.status.status == "running" and self.session_manager is not None

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._ready_event = asyncio.Event()
        self.status = ServerStatus(name=self.name, status="starting")
        self._task = asyncio.create_task(self._run(), name=f"mcp-hub:{self.name}")
        await self._ready_event.wait()

    async def stop(self) -> None:
        if not self._task:
            return
        self.status.status = "stopping"
        self._stop_event.set()
        try:
            await asyncio.wait_for(self._task, timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("[%s] stop timed out, cancelling task", self.name)
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._task = None
        self.session_manager = None
        if self.status.status != "error":
            self.status = ServerStatus(name=self.name, status="stopped")

    async def _run(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                if self.cfg.type == "stdio":
                    merged_env = {**os.environ, **(self.cfg.env or {})}
                    params = StdioServerParameters(
                        command=self.cfg.command,
                        args=list(self.cfg.args),
                        env=merged_env,
                        cwd=self.cfg.cwd,
                    )
                    logger.info("[%s] starting upstream stdio: %s %s", self.name, params.command, " ".join(params.args))
                    read, write = await stack.enter_async_context(stdio_client(params))
                elif self.cfg.type == "http":
                    logger.info("[%s] connecting upstream http: %s", self.name, self.cfg.url)
                    streams = await stack.enter_async_context(
                        streamablehttp_client(self.cfg.url, headers=self.cfg.headers or None)
                    )
                    read, write = streams[0], streams[1]
                elif self.cfg.type == "sse":
                    logger.info("[%s] connecting upstream sse: %s", self.name, self.cfg.url)
                    read, write = await stack.enter_async_context(
                        sse_client(self.cfg.url, headers=self.cfg.headers or None)
                    )
                else:
                    raise ValueError(f"unsupported server type: {self.cfg.type!r}")
                session = await stack.enter_async_context(ClientSession(read, write))
                init_result = await session.initialize()

                caps = init_result.capabilities
                self.status.capabilities = {
                    "tools": caps.tools is not None,
                    "resources": caps.resources is not None,
                    "prompts": caps.prompts is not None,
                    "logging": caps.logging is not None,
                    "completions": caps.completions is not None,
                }
                self.status.server_info = {
                    "name": init_result.serverInfo.name,
                    "version": init_result.serverInfo.version,
                }

                # cache counts (best-effort; some servers may not implement listing despite capability)
                if caps.tools is not None:
                    try:
                        tools = (await session.list_tools()).tools
                        self.status.tool_count = len(tools)
                        self.status.tools = [t.name for t in tools]
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[%s] list_tools failed: %s", self.name, e)
                if caps.prompts is not None:
                    try:
                        prompts = (await session.list_prompts()).prompts
                        self.status.prompt_count = len(prompts)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[%s] list_prompts failed: %s", self.name, e)
                if caps.resources is not None:
                    try:
                        resources = (await session.list_resources()).resources
                        self.status.resource_count = len(resources)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("[%s] list_resources failed: %s", self.name, e)

                proxy_server = build_proxy_server(
                    self.name, session, caps, get_disabled_tools=self._get_disabled_tools
                )
                manager = StreamableHTTPSessionManager(
                    app=proxy_server,
                    event_store=None,
                    json_response=False,
                    stateless=False,
                )
                await stack.enter_async_context(manager.run())
                self.session_manager = manager

                self.status.status = "running"
                self.status.error = None
                logger.info(
                    "[%s] running (tools=%d prompts=%d resources=%d)",
                    self.name,
                    self.status.tool_count,
                    self.status.prompt_count,
                    self.status.resource_count,
                )
                self._ready_event.set()

                await self._stop_event.wait()
                logger.info("[%s] stop signal received, tearing down", self.name)
        except asyncio.CancelledError:
            self._ready_event.set()
            raise
        except BaseException as e:  # includes ExceptionGroup
            err = _format_error(e)
            logger.error("[%s] failed: %s", self.name, err)
            self.status.status = "error"
            self.status.error = err
            self._ready_event.set()
        finally:
            self.session_manager = None
            if self.status.status not in ("error",):
                self.status.status = "stopped"


def _format_error(e: BaseException) -> str:
    if isinstance(e, BaseExceptionGroup):
        inner = "; ".join(_format_error(x) for x in e.exceptions)
        return f"{type(e).__name__}: {inner}"
    return f"{type(e).__name__}: {e}"


class HubManager:
    def __init__(self, get_disabled_tools: Callable[[str], set[str]] | None = None):
        self._servers: dict[str, RuntimeServer] = {}
        self._lock = asyncio.Lock()
        self._get_disabled_tools = get_disabled_tools

    def get(self, name: str) -> RuntimeServer | None:
        return self._servers.get(name)

    def list(self) -> list[RuntimeServer]:
        return list(self._servers.values())

    async def sync(self, configs: dict[str, ServerConfig]) -> None:
        """Add/remove/update servers to match the given config dict.

        Servers no longer in config are stopped and removed. Servers whose config
        changed are restarted. Newly added servers are created (and started if enabled).
        Start operations run concurrently.
        """
        async with self._lock:
            # remove servers not in config
            for name in list(self._servers.keys()):
                if name not in configs:
                    await self._servers[name].stop()
                    del self._servers[name]

            tasks: list = []
            for name, cfg in configs.items():
                existing = self._servers.get(name)
                if existing is None:
                    rt = RuntimeServer(name, cfg, get_disabled_tools=self._get_disabled_tools)
                    self._servers[name] = rt
                    if cfg.enabled:
                        tasks.append(rt.start())
                else:
                    if _config_changed(existing.cfg, cfg):
                        await existing.stop()
                        existing.cfg = cfg
                        if cfg.enabled:
                            tasks.append(existing.start())
                    else:
                        existing.cfg = cfg
                        if cfg.enabled and not existing.is_running() and existing.status.status not in ("starting", "error"):
                            tasks.append(existing.start())

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    async def set_enabled(self, name: str, enabled: bool) -> RuntimeServer:
        async with self._lock:
            rt = self._servers.get(name)
            if rt is None:
                raise KeyError(name)
            rt.cfg.enabled = enabled
            if enabled and not rt.is_running() and rt.status.status not in ("starting",):
                await rt.start()
            elif not enabled and rt.status.status in ("running", "starting", "error"):
                await rt.stop()
            return rt

    async def restart(self, name: str) -> RuntimeServer:
        async with self._lock:
            rt = self._servers.get(name)
            if rt is None:
                raise KeyError(name)
            await rt.stop()
            if rt.cfg.enabled:
                await rt.start()
            return rt

    async def stop_all(self) -> None:
        async with self._lock:
            for rt in list(self._servers.values()):
                await rt.stop()


def _config_changed(a: ServerConfig, b: ServerConfig) -> bool:
    return (
        a.type != b.type
        or a.command != b.command
        or list(a.args) != list(b.args)
        or dict(a.env) != dict(b.env)
        or a.cwd != b.cwd
        or a.url != b.url
        or dict(a.headers) != dict(b.headers)
    )
