from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    # Either a stdio process (command/args/env/cwd) or a remote endpoint (url/headers).
    type: str = "stdio"  # "stdio" | "http" | "sse"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = False

    def to_json(self) -> dict:
        out: dict = {}
        if self.type == "stdio":
            # Omit "type": stdio is implied by presence of "command".
            out["command"] = self.command
            if self.args:
                out["args"] = list(self.args)
            if self.env:
                out["env"] = dict(self.env)
            if self.cwd:
                out["cwd"] = self.cwd
        else:
            out["type"] = self.type
            out["url"] = self.url
            if self.headers:
                out["headers"] = dict(self.headers)
        out["enabled"] = self.enabled
        return out

    @classmethod
    def from_json(cls, data: dict) -> "ServerConfig":
        # Type inference: explicit "type" wins; otherwise "url" → http, "command" → stdio.
        explicit = (data.get("type") or "").lower()
        if explicit in ("http", "streamable-http", "streamable_http"):
            kind = "http"
        elif explicit == "sse":
            kind = "sse"
        elif explicit == "stdio":
            kind = "stdio"
        elif data.get("url"):
            kind = "http"
        else:
            kind = "stdio"
        return cls(
            type=kind,
            command=data.get("command", "") or "",
            args=list(data.get("args", []) or []),
            env=dict(data.get("env", {}) or {}),
            cwd=data.get("cwd"),
            url=data.get("url", "") or "",
            headers=dict(data.get("headers", {}) or {}),
            enabled=bool(data.get("enabled", False)),
        )


@dataclass
class HubConfig:
    host: str = "127.0.0.1"
    port: int = 3737
    mcpServers: dict[str, ServerConfig] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "mcpServers": {n: s.to_json() for n, s in self.mcpServers.items()},
        }

    @classmethod
    def from_json(cls, data: dict) -> "HubConfig":
        servers = {
            name: ServerConfig.from_json(s)
            for name, s in (data.get("mcpServers") or {}).items()
        }
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=int(data.get("port", 3737)),
            mcpServers=servers,
        )


class ConfigStore:
    """Thread-safe wrapper around the on-disk config file."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = self._read()

    def _read(self) -> HubConfig:
        if not self.path.exists():
            cfg = HubConfig()
            self._write(cfg)
            return cfg
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return HubConfig.from_json(raw)

    def _write(self, cfg: HubConfig) -> None:
        self.path.write_text(
            json.dumps(cfg.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    def reload(self) -> HubConfig:
        with self._lock:
            self._cfg = self._read()
            return self._cfg

    @property
    def config(self) -> HubConfig:
        return self._cfg

    def set_enabled(self, name: str, enabled: bool) -> None:
        with self._lock:
            if name not in self._cfg.mcpServers:
                raise KeyError(name)
            self._cfg.mcpServers[name].enabled = enabled
            self._write(self._cfg)
