from __future__ import annotations

import argparse
import atexit
import logging
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .config import ConfigStore


def _write_pid_file(path: Path) -> None:
    """Write our PID so a separate stop helper can find this exact process.
    Removed on normal shutdown (atexit). A stale file from a hard kill is
    harmless — the stop helper checks the PID still exists before signalling.
    """
    try:
        path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        return

    def _cleanup() -> None:
        try:
            if path.exists() and path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except OSError:
            pass

    atexit.register(_cleanup)


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-hub", description="Lightweight MCP proxy with web UI.")
    parser.add_argument(
        "--config",
        "-c",
        default=os.environ.get("MCP_HUB_CONFIG", "config.json"),
        help="Path to root config file (default: ./config.json)",
    )
    parser.add_argument(
        "--projects-dir",
        default=os.environ.get("MCP_HUB_PROJECTS_DIR"),
        help="Directory holding the managed mcp.json files (default: ./projects next to config.json)",
    )
    parser.add_argument(
        "--predefined",
        default=os.environ.get("MCP_HUB_PREDEFINED"),
        help="Path to the predefined.json catalog (default: ./predefined.json next to config.json)",
    )
    parser.add_argument("--host", default=None, help="Override host from config")
    parser.add_argument("--port", type=int, default=None, help="Override port from config")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    config_path = Path(args.config).resolve()
    projects_dir = (
        Path(args.projects_dir).resolve() if args.projects_dir else config_path.parent / "projects"
    )
    predefined_path = (
        Path(args.predefined).resolve() if args.predefined else config_path.parent / "predefined.json"
    )
    store = ConfigStore(config_path, projects_dir)

    host = args.host or store.settings.host
    port = args.port or store.settings.port

    app = create_app(store, predefined_path=predefined_path)

    _write_pid_file(config_path.parent / "hub.pid")

    uvicorn.run(app, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
