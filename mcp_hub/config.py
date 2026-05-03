from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    enabled: bool = False

    def to_json(self) -> dict:
        out: dict = {"command": self.command}
        if self.args:
            out["args"] = list(self.args)
        if self.env:
            out["env"] = dict(self.env)
        if self.cwd:
            out["cwd"] = self.cwd
        out["enabled"] = self.enabled
        return out

    @classmethod
    def from_json(cls, data: dict) -> "ServerConfig":
        return cls(
            command=data["command"],
            args=list(data.get("args", []) or []),
            env=dict(data.get("env", {}) or {}),
            cwd=data.get("cwd"),
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
