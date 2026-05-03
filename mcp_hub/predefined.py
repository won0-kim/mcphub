"""Loader for the user-editable predefined catalog (`predefined.json`).

The file mirrors the standard mcp.json shape::

    {
      "mcpServers": {
        "<name>": {
          "description": "<optional one-line note>",
          ... standard mcp.json server fields (command/args/env/type/url/...)
        }
      }
    }

Each entry is flattened to ``{"name": <key>, ...spec}`` for the UI/API."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_predefined(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("predefined.json is invalid JSON: %s", e)
        return []
    if not isinstance(data, dict):
        logger.warning(
            "predefined.json must be a JSON object with `mcpServers`, got %s",
            type(data).__name__,
        )
        return []
    servers = data.get("mcpServers")
    if not isinstance(servers, dict):
        logger.warning("predefined.json: `mcpServers` must be an object")
        return []
    out: list[dict] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            continue
        out.append({"name": name, **spec})
    return out
