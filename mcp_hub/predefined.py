"""Loader for the user-editable predefined MCP server catalog (`predefined.json`)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def load_predefined(path: Path) -> list[dict]:
    """Read the catalog from `path`. Returns [] (and logs) if the file is
    missing or malformed; the UI just shows an empty Predefined tab."""
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        logger.warning("predefined.json is invalid JSON: %s", e)
        return []
    if not isinstance(data, list):
        logger.warning("predefined.json must be a JSON array, got %s", type(data).__name__)
        return []
    return data
