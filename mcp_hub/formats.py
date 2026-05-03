"""Serialize the hub-URL mcpServers dict into the format the target client expects.

- "claude_code": standard mcp.json (`{"mcpServers": {...}}`)
- "codex":       Codex `config.toml` (`[mcp_servers.<name>]` tables)

Both formats round-trip the same field set (command/args/env/url/headers).
For target-file merging we preserve everything outside the mcp.json /
mcp_servers section so other settings (theme, model config, etc.) survive.
"""
from __future__ import annotations

import json
import re
from typing import Any


# --- TOML primitive serialization ----------------------------------------

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


def _toml_string(s: str) -> str:
    return (
        '"'
        + s.replace("\\", "\\\\")
           .replace('"', '\\"')
           .replace("\n", "\\n")
           .replace("\r", "\\r")
           .replace("\t", "\\t")
        + '"'
    )


def _toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return _toml_string(v)
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        parts = []
        for k, val in v.items():
            key = k if _BARE_KEY_RE.match(k) else _toml_string(k)
            parts.append(f"{key} = {_toml_value(val)}")
        return "{ " + ", ".join(parts) + " }"
    raise ValueError(f"unsupported TOML value: {type(v).__name__}")


def _toml_key(name: str) -> str:
    return name if _BARE_KEY_RE.match(name) else _toml_string(name)


def _format_codex_block(name: str, spec: dict) -> str:
    lines = [f"[mcp_servers.{_toml_key(name)}]"]
    # Stable, predictable field order
    order = ("type", "command", "args", "env", "cwd", "url", "headers")
    for key in order:
        if key not in spec:
            continue
        val = spec[key]
        if val is None:
            continue
        if isinstance(val, (list, dict)) and not val:
            continue
        lines.append(f"{key} = {_toml_value(val)}")
    return "\n".join(lines) + "\n"


def serialize_for_view(client_format: str, mcp_servers: dict) -> tuple[str, str]:
    """Return (text, media_type) for the standalone "show config" view."""
    if client_format == "codex":
        if not mcp_servers:
            return "# (no enabled servers)\n", "application/toml"
        body = "\n".join(_format_codex_block(name, spec) for name, spec in mcp_servers.items())
        return body, "application/toml"
    body = json.dumps({"mcpServers": mcp_servers}, indent=2, ensure_ascii=False) + "\n"
    return body, "application/json"


# --- Target file merge ---------------------------------------------------


_MCP_SERVERS_HEADER_RE = re.compile(r"^\s*\[\s*mcp_servers\s*\.")
_TABLE_HEADER_RE = re.compile(r"^\s*\[")


def _strip_codex_mcp_blocks(text: str) -> str:
    """Remove every existing `[mcp_servers.*]` table from the TOML text,
    preserving everything else (other tables, top-level keys, comments).
    """
    out_lines: list[str] = []
    skip = False
    for line in text.splitlines(keepends=True):
        if _MCP_SERVERS_HEADER_RE.match(line):
            skip = True
            continue
        if skip and _TABLE_HEADER_RE.match(line):
            # New non-mcp_servers section starts; stop skipping.
            skip = False
        if not skip:
            out_lines.append(line)
    return "".join(out_lines)


def merge_target_text(client_format: str, existing_text: str, mcp_servers: dict) -> str:
    """Produce the new text for the target file with `mcp_servers` replaced
    while keeping everything else intact."""
    if client_format == "codex":
        cleaned = _strip_codex_mcp_blocks(existing_text).rstrip()
        new_blocks = "\n".join(
            _format_codex_block(name, spec) for name, spec in mcp_servers.items()
        )
        if cleaned and new_blocks:
            return cleaned + "\n\n" + new_blocks
        return (cleaned + "\n") if cleaned and not new_blocks else new_blocks

    # claude_code (mcp.json — JSON merge)
    existing: dict = {}
    if existing_text.strip():
        try:
            parsed = json.loads(existing_text)
            if isinstance(parsed, dict):
                existing = parsed
        except json.JSONDecodeError:
            existing = {}
    existing["mcpServers"] = mcp_servers
    return json.dumps(existing, indent=2, ensure_ascii=False) + "\n"


def target_filename_hint(client_format: str) -> str:
    return "config.toml" if client_format == "codex" else "mcp.json"
