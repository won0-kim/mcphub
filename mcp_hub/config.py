from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path


FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")


@dataclass
class ServerConfig:
    """Mirrors the spec for one server inside mcp.json. `enabled` is hub-side
    overlay that is NOT serialized into mcp.json on disk."""

    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = False  # not persisted into mcp.json

    def to_mcp_json(self) -> dict:
        """Serialize back to the standard mcp.json shape (no `enabled`)."""
        out: dict = {}
        if self.type == "stdio":
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
        return out

    @classmethod
    def from_mcp_json(cls, data: dict) -> "ServerConfig":
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
        )


def parse_mcp_json(content: str | dict) -> dict[str, ServerConfig]:
    """Parse a Claude Desktop/Code mcp.json blob (wrapped or bare)."""
    raw = json.loads(content) if isinstance(content, str) else content
    if not isinstance(raw, dict):
        raise ValueError("mcp.json must be a JSON object")
    servers_raw = raw.get("mcpServers") if "mcpServers" in raw else raw
    if not isinstance(servers_raw, dict):
        raise ValueError("`mcpServers` must be an object")
    out: dict[str, ServerConfig] = {}
    for name, spec in servers_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"server '{name}' must be an object")
        if not SERVER_NAME_RE.match(name):
            raise ValueError(f"invalid server name: {name!r}")
        # Tolerate (and discard) a stray `enabled` field if present.
        spec = {k: v for k, v in spec.items() if k != "enabled"}
        out[name] = ServerConfig.from_mcp_json(spec)
    return out


@dataclass
class HubSettings:
    """Hub's own config (config.json). Distinct from the managed mcp.json files."""

    host: str = "127.0.0.1"
    port: int = 3737
    # public_url: prefix used when generating hub URLs that go into mcp.json
    # (e.g. "https://mcp.example.com" or "http://10.0.0.5:3737"). When empty
    # the hub falls back to whatever URL the request came in on.
    public_url: str = ""
    active: str = "default"
    # enabled[<mcp file name>] = [<server name>, ...]
    enabled: dict[str, list[str]] = field(default_factory=dict)
    # targets[<mcp file name>] = absolute filesystem path the hub writes to
    targets: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "public_url": self.public_url,
            "active": self.active,
            "enabled": {k: sorted(set(v)) for k, v in self.enabled.items()},
            "targets": {k: v for k, v in self.targets.items() if v},
        }

    @classmethod
    def from_json(cls, data: dict) -> "HubSettings":
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=int(data.get("port", 3737)),
            public_url=str(data.get("public_url") or ""),
            active=data.get("active") or "default",
            enabled={k: list(v or []) for k, v in (data.get("enabled") or {}).items()},
            targets={k: str(v) for k, v in (data.get("targets") or {}).items() if v},
        )


class ConfigStore:
    """Manages the hub's `config.json` and a directory of clean `mcp.json` files."""

    def __init__(self, root_path: Path, mcp_dir: Path):
        self.root_path = root_path
        self.mcp_dir = mcp_dir
        self.mcp_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate_legacy()
        self._settings = self._read_settings()
        self._ensure_active_exists()

    # ---- settings (config.json) -------------------------------------------

    @property
    def settings(self) -> HubSettings:
        return self._settings

    def _read_settings(self) -> HubSettings:
        if not self.root_path.exists():
            s = HubSettings()
            self._write_settings(s)
            return s
        return HubSettings.from_json(json.loads(self.root_path.read_text(encoding="utf-8")))

    def _write_settings(self, s: HubSettings) -> None:
        self.root_path.write_text(
            json.dumps(s.to_json(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    # ---- migrations -------------------------------------------------------

    def _migrate_legacy(self) -> None:
        # 1) Old `configs/` directory with `enabled` flags in each server entry.
        legacy_dir = self.root_path.parent / "configs"
        if legacy_dir.exists() and legacy_dir.is_dir():
            self._migrate_dir_with_enabled(legacy_dir)

        # 2) Even older: root config.json had `mcpServers` directly inline.
        if self.root_path.exists():
            raw = json.loads(self.root_path.read_text(encoding="utf-8"))
            if "mcpServers" in raw:
                servers = raw.pop("mcpServers") or {}
                clean, enabled_list = _strip_enabled(servers)
                target = self.mcp_dir / "default.json"
                if not target.exists():
                    _write_mcp_json(target, clean)
                raw.setdefault("active", "default")
                raw.setdefault("enabled", {})
                raw["enabled"]["default"] = enabled_list
                self.root_path.write_text(
                    json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

    def _migrate_dir_with_enabled(self, legacy_dir: Path) -> None:
        if self.root_path.exists():
            raw = json.loads(self.root_path.read_text(encoding="utf-8"))
        else:
            raw = {}
        overlay: dict[str, list[str]] = dict(raw.get("enabled") or {})
        for src in sorted(legacy_dir.glob("*.json")):
            name = src.stem
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except Exception:
                continue
            servers = data.get("mcpServers") or {}
            clean, enabled_list = _strip_enabled(servers)
            target = self.mcp_dir / f"{name}.json"
            if not target.exists():
                _write_mcp_json(target, clean)
            overlay[name] = enabled_list
            src.unlink()
        try:
            legacy_dir.rmdir()
        except OSError:
            pass
        raw["enabled"] = overlay
        self.root_path.write_text(
            json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

    def _ensure_active_exists(self) -> None:
        if not self._mcp_path(self._settings.active).exists():
            existing = self.list_files()
            if not existing:
                self.save_mcp_file("default", {})
                self._settings.active = "default"
            elif self._settings.active not in existing:
                self._settings.active = existing[0]
            self._write_settings(self._settings)

    # ---- mcp file paths --------------------------------------------------

    def _mcp_path(self, name: str) -> Path:
        if not FILE_NAME_RE.match(name):
            raise ValueError(f"invalid mcp.json name: {name!r}")
        return self.mcp_dir / f"{name}.json"

    def list_files(self) -> list[str]:
        return sorted(p.stem for p in self.mcp_dir.glob("*.json"))

    # ---- read/write a single mcp.json file -------------------------------

    def get_servers(self, name: str) -> dict[str, ServerConfig]:
        """Returns servers from `mcp/<name>.json`, with `enabled` populated
        from the hub overlay."""
        path = self._mcp_path(name)
        if not path.exists():
            raise KeyError(name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers_raw = raw.get("mcpServers") or {}
        enabled_set = set(self._settings.enabled.get(name, []))
        out: dict[str, ServerConfig] = {}
        for sname, spec in servers_raw.items():
            if not isinstance(spec, dict):
                continue
            cfg = ServerConfig.from_mcp_json(spec)
            cfg.enabled = sname in enabled_set
            out[sname] = cfg
        return out

    def get_raw(self, name: str) -> str:
        path = self._mcp_path(name)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def save_mcp_file(self, name: str, servers: dict[str, ServerConfig]) -> None:
        clean = {sname: cfg.to_mcp_json() for sname, cfg in servers.items()}
        _write_mcp_json(self._mcp_path(name), clean)
        # Prune enabled list to remaining server names
        remaining = set(servers.keys())
        with self._lock:
            cur = set(self._settings.enabled.get(name, []))
            self._settings.enabled[name] = sorted(cur & remaining)
            self._write_settings(self._settings)

    # ---- file-level CRUD --------------------------------------------------

    def create_file(self, name: str, content: str | dict | None = None) -> None:
        with self._lock:
            path = self._mcp_path(name)
            if path.exists():
                raise ValueError(f"mcp.json '{name}' already exists")
            if content is None:
                _write_mcp_json(path, {})
                self._settings.enabled.setdefault(name, [])
            else:
                servers = parse_mcp_json(content)
                _write_mcp_json(path, {n: s.to_mcp_json() for n, s in servers.items()})
                # Default everything to disabled when importing
                self._settings.enabled[name] = []
            self._write_settings(self._settings)

    def replace_file(self, name: str, content: str | dict) -> None:
        with self._lock:
            self._mcp_path(name)  # validate name
            servers = parse_mcp_json(content)
            _write_mcp_json(self._mcp_path(name), {n: s.to_mcp_json() for n, s in servers.items()})
            kept = set(self._settings.enabled.get(name, [])) & set(servers.keys())
            self._settings.enabled[name] = sorted(kept)
            self._write_settings(self._settings)

    def delete_file(self, name: str) -> None:
        with self._lock:
            path = self._mcp_path(name)
            if not path.exists():
                raise KeyError(name)
            was_active = name == self._settings.active
            others = [n for n in self.list_files() if n != name]
            path.unlink()
            self._settings.enabled.pop(name, None)
            self._settings.targets.pop(name, None)
            if was_active:
                if others:
                    self._settings.active = others[0]
                else:
                    # Removed the last profile — recreate an empty `default`.
                    fresh = "default" if name != "default" else "default"
                    fresh_path = self._mcp_path(fresh)
                    _write_mcp_json(fresh_path, {})
                    self._settings.active = fresh
                    self._settings.enabled.setdefault(fresh, [])
            self._write_settings(self._settings)

    def copy_file(self, src: str, dst: str) -> None:
        with self._lock:
            src_path = self._mcp_path(src)
            dst_path = self._mcp_path(dst)
            if not src_path.exists():
                raise KeyError(src)
            if dst_path.exists():
                raise ValueError(f"mcp.json '{dst}' already exists")
            dst_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
            # Inherit enabled set; do NOT inherit target (avoid two files racing on same target)
            self._settings.enabled[dst] = list(self._settings.enabled.get(src, []))
            self._write_settings(self._settings)

    def rename_file(self, old: str, new: str) -> None:
        with self._lock:
            if old == new:
                return
            src_path = self._mcp_path(old)
            dst_path = self._mcp_path(new)
            if not src_path.exists():
                raise KeyError(old)
            if dst_path.exists():
                raise ValueError(f"mcp.json '{new}' already exists")
            src_path.rename(dst_path)
            if old in self._settings.enabled:
                self._settings.enabled[new] = self._settings.enabled.pop(old)
            if old in self._settings.targets:
                self._settings.targets[new] = self._settings.targets.pop(old)
            if self._settings.active == old:
                self._settings.active = new
            self._write_settings(self._settings)

    # ---- hub settings (host/port/public_url) ------------------------------

    def update_hub_settings(
        self,
        host: str | None = None,
        port: int | None = None,
        public_url: str | None = None,
    ) -> HubSettings:
        with self._lock:
            if host is not None:
                if not host.strip():
                    raise ValueError("host cannot be empty")
                self._settings.host = host.strip()
            if port is not None:
                p = int(port)
                if not (1 <= p <= 65535):
                    raise ValueError(f"port out of range: {p}")
                self._settings.port = p
            if public_url is not None:
                pu = public_url.strip().rstrip("/")
                if pu and not (pu.startswith("http://") or pu.startswith("https://")):
                    raise ValueError("public_url must start with http:// or https://")
                self._settings.public_url = pu
            self._write_settings(self._settings)
            return self._settings

    # ---- bulk enable (used after Load import) -----------------------------

    def enable_all(self, file_name: str) -> None:
        with self._lock:
            path = self._mcp_path(file_name)
            if not path.exists():
                raise KeyError(file_name)
            raw = json.loads(path.read_text(encoding="utf-8"))
            names = list((raw.get("mcpServers") or {}).keys())
            self._settings.enabled[file_name] = sorted(names)
            self._write_settings(self._settings)

    # ---- target path (auto-sync destination) ------------------------------

    def get_target(self, name: str) -> str:
        return self._settings.targets.get(name, "")

    def set_target(self, name: str, path: str | None) -> None:
        with self._lock:
            if path:
                self._settings.targets[name] = path
            else:
                self._settings.targets.pop(name, None)
            self._write_settings(self._settings)

    def set_active(self, name: str) -> dict[str, ServerConfig]:
        with self._lock:
            if not self._mcp_path(name).exists():
                raise KeyError(name)
            self._settings.active = name
            self._settings.enabled.setdefault(name, [])
            self._write_settings(self._settings)
            return self.get_servers(name)

    # ---- active mcp helpers ----------------------------------------------

    def active_path(self) -> Path:
        return self._mcp_path(self._settings.active)

    def active_servers(self) -> dict[str, ServerConfig]:
        return self.get_servers(self._settings.active)

    # ---- enabled overlay --------------------------------------------------

    def set_enabled(self, server_name: str, enabled: bool) -> None:
        with self._lock:
            servers = self.active_servers()
            if server_name not in servers:
                raise KeyError(server_name)
            cur = set(self._settings.enabled.get(self._settings.active, []))
            if enabled:
                cur.add(server_name)
            else:
                cur.discard(server_name)
            self._settings.enabled[self._settings.active] = sorted(cur)
            self._write_settings(self._settings)

    # ---- server CRUD on the active mcp.json -------------------------------

    def add_server(self, server_name: str, cfg: ServerConfig) -> None:
        if not SERVER_NAME_RE.match(server_name):
            raise ValueError(f"invalid server name: {server_name!r}")
        servers = self.active_servers()
        if server_name in servers:
            raise ValueError(f"server '{server_name}' already exists")
        servers[server_name] = cfg
        self.save_mcp_file(self._settings.active, servers)
        if cfg.enabled:
            self.set_enabled(server_name, True)

    def update_server(
        self,
        server_name: str,
        cfg: ServerConfig,
        new_name: str | None = None,
    ) -> str:
        servers = self.active_servers()
        if server_name not in servers:
            raise KeyError(server_name)
        target = new_name or server_name
        if not SERVER_NAME_RE.match(target):
            raise ValueError(f"invalid server name: {target!r}")
        was_enabled = servers[server_name].enabled
        if target != server_name:
            if target in servers:
                raise ValueError(f"server '{target}' already exists")
            del servers[server_name]
            # rename in enabled overlay
            with self._lock:
                cur = set(self._settings.enabled.get(self._settings.active, []))
                cur.discard(server_name)
                if was_enabled:
                    cur.add(target)
                self._settings.enabled[self._settings.active] = sorted(cur)
                self._write_settings(self._settings)
        servers[target] = cfg
        self.save_mcp_file(self._settings.active, servers)
        return target

    def delete_server(self, server_name: str) -> None:
        servers = self.active_servers()
        if server_name not in servers:
            raise KeyError(server_name)
        del servers[server_name]
        self.save_mcp_file(self._settings.active, servers)


# --- helpers --------------------------------------------------------------


def _strip_enabled(servers_raw: dict) -> tuple[dict, list[str]]:
    """Split a legacy mcpServers dict (with `enabled` per entry) into a clean
    mcp.json mapping plus the list of enabled server names."""
    clean: dict = {}
    enabled_list: list[str] = []
    for sname, spec in (servers_raw or {}).items():
        if not isinstance(spec, dict):
            continue
        spec = dict(spec)
        if spec.pop("enabled", False):
            enabled_list.append(sname)
        clean[sname] = spec
    return clean, sorted(set(enabled_list))


def _write_mcp_json(path: Path, servers: dict) -> None:
    body = {"mcpServers": servers}
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
