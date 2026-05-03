from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

import uvicorn

from .api import create_app
from .config import ConfigStore


def main() -> None:
    parser = argparse.ArgumentParser(prog="mcp-hub", description="Lightweight MCP proxy with web UI.")
    parser.add_argument(
        "--config",
        "-c",
        default=os.environ.get("MCP_HUB_CONFIG", "config.json"),
        help="Path to config file (default: ./config.json)",
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
    store = ConfigStore(config_path)  # ensures the file exists with defaults

    host = args.host or store.config.host
    port = args.port or store.config.port

    app = create_app(config_path)

    uvicorn.run(app, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
