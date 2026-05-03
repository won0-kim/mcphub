from __future__ import annotations

import json
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path


FILE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
SERVER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*$")
META_SUFFIX = ".meta.json"


@dataclass
class ServerConfig:
    type: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = False  # not persisted into mcp.json

    def to_mcp_json(self) -> dict:
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
        spec = {k: v for k, v in spec.items() if k != "enabled"}
        out[name] = ServerConfig.from_mcp_json(spec)
    return out


CLIENT_FORMATS = ("claude_code", "codex")


@dataclass
class HubSettings:
    """Hub-only config. Per-profile state (enabled, target) lives in sidecar
    files next to each mcp.json — see `projects/<name>.meta.json`."""

    host: str = "127.0.0.1"
    port: int = 3737
    public_url: str = ""
    active: str = "default"
    # Output format used when generating "show mcp.json" view + writing
    # to a profile's target file. "claude_code" = mcp.json (JSON);
    # "codex" = config.toml (TOML, [mcp_servers.<name>] tables).
    client_format: str = "claude_code"

    def to_json(self) -> dict:
        return {
            "host": self.host,
            "port": self.port,
            "public_url": self.public_url,
            "active": self.active,
            "client_format": self.client_format,
        }

    @classmethod
    def from_json(cls, data: dict) -> "HubSettings":
        cf = (data.get("client_format") or "claude_code").lower()
        if cf not in CLIENT_FORMATS:
            cf = "claude_code"
        return cls(
            host=data.get("host", "127.0.0.1"),
            port=int(data.get("port", 3737)),
            public_url=str(data.get("public_url") or ""),
            active=data.get("active") or "default",
            client_format=cf,
        )


class ConfigStore:
    """Hub config + per-profile mcp.json files (with sidecar metadata).

    Layout::

        config.json                   # { host, port, public_url, active }
        projects/<name>.json          # standard mcp.json (mcpServers only)
        projects/<name>.meta.json     # { enabled: [...], target: "..." }
    """

    def __init__(self, root_path: Path, projects_dir: Path):
        self.root_path = root_path
        self.projects_dir = projects_dir
        self._migrate_dir_rename(projects_dir)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._migrate_legacy()
        self._settings = self._read_settings()
        self._ensure_active_exists()

    def _migrate_dir_rename(self, projects_dir: Path) -> None:
        """If a legacy `mcp/` directory exists alongside config.json, rename it
        to `projects/` so we can keep operating from the new location."""
        legacy = projects_dir.parent / "mcp"
        if legacy.exists() and legacy.is_dir() and not projects_dir.exists():
            try:
                legacy.rename(projects_dir)
            except OSError:
                # Fall back to a copy + best-effort delete.
                projects_dir.mkdir(parents=True, exist_ok=True)
                for item in legacy.iterdir():
                    shutil.move(str(item), str(projects_dir / item.name))
                try:
                    legacy.rmdir()
                except OSError:
                    pass

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
        # 1) Old `configs/` dir with `enabled` flags inside server entries.
        legacy_dir = self.root_path.parent / "configs"
        if legacy_dir.exists() and legacy_dir.is_dir():
            self._migrate_dir_with_enabled(legacy_dir)

        # 2) Even older: root config.json had `mcpServers` directly inline.
        if self.root_path.exists():
            raw = json.loads(self.root_path.read_text(encoding="utf-8"))
            if "mcpServers" in raw:
                servers = raw.pop("mcpServers") or {}
                clean, enabled_list = _strip_enabled(servers)
                target = self.projects_dir / "default.json"
                if not target.exists():
                    _write_mcp_json(target, clean)
                raw.setdefault("active", "default")
                raw.setdefault("enabled", {})
                raw["enabled"]["default"] = enabled_list
                self.root_path.write_text(
                    json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8",
                )

        # 3) Hoist `enabled` / `targets` overlays from config.json into per-
        #    profile sidecar files (mcp/<name>.meta.json).
        if self.root_path.exists():
            raw = json.loads(self.root_path.read_text(encoding="utf-8"))
            enabled_overlay = raw.get("enabled")
            targets_overlay = raw.get("targets")
            if enabled_overlay is not None or targets_overlay is not None:
                names = set((enabled_overlay or {}).keys()) | set((targets_overlay or {}).keys())
                for name in names:
                    if not FILE_NAME_RE.match(name):
                        continue
                    meta_path = self.projects_dir / f"{name}{META_SUFFIX}"
                    existing: dict = {"enabled": [], "target": ""}
                    if meta_path.exists():
                        try:
                            existing = json.loads(meta_path.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            existing = {"enabled": [], "target": ""}
                    if enabled_overlay and name in enabled_overlay:
                        existing["enabled"] = list(enabled_overlay[name] or [])
                    if targets_overlay and name in targets_overlay:
                        existing["target"] = str(targets_overlay[name] or "")
                    _write_meta_file(meta_path, existing)
                raw.pop("enabled", None)
                raw.pop("targets", None)
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
            target = self.projects_dir / f"{name}.json"
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
        # Freeze any sidecar that doesn't yet have an explicit client_format
        # to the current hub default. Avoids surprise rewrites if the global
        # default changes later.
        for name in self.list_files():
            meta = self._read_meta(name)
            if not (meta.get("client_format") or "") in CLIENT_FORMATS:
                meta["client_format"] = self._settings.client_format
                self._write_meta(name, meta)

    # ---- paths ------------------------------------------------------------

    def _mcp_path(self, name: str) -> Path:
        if not FILE_NAME_RE.match(name):
            raise ValueError(f"invalid mcp.json name: {name!r}")
        return self.projects_dir / f"{name}.json"

    def _meta_path(self, name: str) -> Path:
        if not FILE_NAME_RE.match(name):
            raise ValueError(f"invalid mcp.json name: {name!r}")
        return self.projects_dir / f"{name}{META_SUFFIX}"

    def list_files(self) -> list[str]:
        out: list[str] = []
        for p in self.projects_dir.glob("*.json"):
            if p.name.endswith(META_SUFFIX):
                continue
            out.append(p.stem)
        return sorted(out)

    # ---- sidecar (per-profile meta) --------------------------------------

    def _read_meta(self, name: str) -> dict:
        p = self._meta_path(name)
        if not p.exists():
            return {"enabled": [], "target": "", "client_format": ""}
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"enabled": [], "target": "", "client_format": ""}
        return {
            "enabled": list(raw.get("enabled") or []),
            "target": str(raw.get("target") or ""),
            "client_format": str(raw.get("client_format") or ""),
        }

    def _write_meta(self, name: str, meta: dict) -> None:
        _write_meta_file(self._meta_path(name), meta)

    # ---- mcp.json read/write ---------------------------------------------

    def get_servers(self, name: str) -> dict[str, ServerConfig]:
        path = self._mcp_path(name)
        if not path.exists():
            raise KeyError(name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        servers_raw = raw.get("mcpServers") or {}
        enabled_set = set(self._read_meta(name)["enabled"])
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
        with self._lock:
            meta = self._read_meta(name)
            meta["enabled"] = sorted(set(meta["enabled"]) & set(servers.keys()))
            self._write_meta(name, meta)

    # ---- file-level CRUD --------------------------------------------------

    def create_file(self, name: str, content: str | dict | None = None) -> None:
        with self._lock:
            path = self._mcp_path(name)
            if path.exists():
                raise ValueError(f"mcp.json '{name}' already exists")
            if content is None:
                _write_mcp_json(path, {})
            else:
                servers = parse_mcp_json(content)
                _write_mcp_json(path, {n: s.to_mcp_json() for n, s in servers.items()})
            # Freeze the project's format to the current global default at creation
            # so later global changes don't silently rewrite this project's target.
            self._write_meta(
                name,
                {"enabled": [], "target": "", "client_format": self._settings.client_format},
            )

    def replace_file(self, name: str, content: str | dict) -> None:
        with self._lock:
            self._mcp_path(name)
            servers = parse_mcp_json(content)
            _write_mcp_json(self._mcp_path(name), {n: s.to_mcp_json() for n, s in servers.items()})
            meta = self._read_meta(name)
            meta["enabled"] = sorted(set(meta["enabled"]) & set(servers.keys()))
            self._write_meta(name, meta)

    def delete_file(self, name: str) -> None:
        with self._lock:
            path = self._mcp_path(name)
            if not path.exists():
                raise KeyError(name)
            was_active = name == self._settings.active
            others = [n for n in self.list_files() if n != name]
            path.unlink()
            meta_path = self._meta_path(name)
            if meta_path.exists():
                meta_path.unlink()
            if was_active:
                if others:
                    self._settings.active = others[0]
                else:
                    fresh = "default"
                    _write_mcp_json(self._mcp_path(fresh), {})
                    self._write_meta(fresh, {"enabled": [], "target": ""})
                    self._settings.active = fresh
                self._write_settings(self._settings)

    def copy_file(self, src: str, dst: str) -> None:
        with self._lock:
            src_path = self._mcp_path(src)
            dst_path = self._mcp_path(dst)
            if not src_path.exists():
                raise KeyError(src)
            if dst_path.exists():
                raise ValueError(f"mcp.json '{dst}' already exists")
            shutil.copyfile(src_path, dst_path)
            # Inherit enabled set + format; do NOT inherit target (avoid two
            # profiles writing into the same target file).
            src_meta = self._read_meta(src)
            self._write_meta(
                dst,
                {
                    "enabled": list(src_meta["enabled"]),
                    "target": "",
                    "client_format": src_meta.get("client_format") or self._settings.client_format,
                },
            )

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
            old_meta = self._meta_path(old)
            if old_meta.exists():
                old_meta.rename(self._meta_path(new))
            if self._settings.active == old:
                self._settings.active = new
                self._write_settings(self._settings)

    def set_active(self, name: str) -> dict[str, ServerConfig]:
        with self._lock:
            if not self._mcp_path(name).exists():
                raise KeyError(name)
            self._settings.active = name
            self._write_settings(self._settings)
            return self.get_servers(name)

    def active_path(self) -> Path:
        return self._mcp_path(self._settings.active)

    def active_servers(self) -> dict[str, ServerConfig]:
        return self.get_servers(self._settings.active)

    # ---- bulk enable (used after Load import) ----------------------------

    def enable_all(self, file_name: str) -> None:
        with self._lock:
            path = self._mcp_path(file_name)
            if not path.exists():
                raise KeyError(file_name)
            raw = json.loads(path.read_text(encoding="utf-8"))
            names = list((raw.get("mcpServers") or {}).keys())
            meta = self._read_meta(file_name)
            meta["enabled"] = sorted(names)
            self._write_meta(file_name, meta)

    # ---- hub settings (host/port/public_url) -----------------------------

    def update_hub_settings(
        self,
        host: str | None = None,
        port: int | None = None,
        public_url: str | None = None,
        client_format: str | None = None,
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
            if client_format is not None:
                cf = client_format.strip().lower()
                if cf not in CLIENT_FORMATS:
                    raise ValueError(f"client_format must be one of {CLIENT_FORMATS}")
                self._settings.client_format = cf
            self._write_settings(self._settings)
            return self._settings

    # ---- target path (auto-sync destination) ------------------------------

    def get_target(self, name: str) -> str:
        return self._read_meta(name)["target"]

    def set_target(self, name: str, path: str | None) -> None:
        with self._lock:
            meta = self._read_meta(name)
            meta["target"] = path or ""
            self._write_meta(name, meta)

    # ---- per-project client_format ---------------------------------------

    def get_client_format(self, name: str) -> str:
        """Project's format if explicitly set, else hub default."""
        fmt = self._read_meta(name).get("client_format") or ""
        if fmt in CLIENT_FORMATS:
            return fmt
        return self._settings.client_format

    def get_explicit_client_format(self, name: str) -> str:
        """Returns the per-project value (may be empty if falling back)."""
        fmt = self._read_meta(name).get("client_format") or ""
        return fmt if fmt in CLIENT_FORMATS else ""

    def set_client_format(self, name: str, fmt: str | None) -> None:
        cf = (fmt or "").strip().lower()
        if cf and cf not in CLIENT_FORMATS:
            raise ValueError(f"client_format must be one of {CLIENT_FORMATS}")
        with self._lock:
            meta = self._read_meta(name)
            meta["client_format"] = cf
            self._write_meta(name, meta)

    # ---- enabled overlay --------------------------------------------------

    def set_enabled(self, server_name: str, enabled: bool) -> None:
        with self._lock:
            servers = self.active_servers()
            if server_name not in servers:
                raise KeyError(server_name)
            meta = self._read_meta(self._settings.active)
            cur = set(meta["enabled"])
            if enabled:
                cur.add(server_name)
            else:
                cur.discard(server_name)
            meta["enabled"] = sorted(cur)
            self._write_meta(self._settings.active, meta)

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
            with self._lock:
                meta = self._read_meta(self._settings.active)
                cur = set(meta["enabled"])
                cur.discard(server_name)
                if was_enabled:
                    cur.add(target)
                meta["enabled"] = sorted(cur)
                self._write_meta(self._settings.active, meta)
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


def _write_meta_file(path: Path, meta: dict) -> None:
    body: dict = {
        "enabled": sorted(set(meta.get("enabled") or [])),
        "target": meta.get("target") or "",
    }
    cf = (meta.get("client_format") or "").strip()
    if cf:
        body["client_format"] = cf
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
