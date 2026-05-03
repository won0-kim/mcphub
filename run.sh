#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f config.json ]; then
  cp config.example.json config.json
  echo "[mcp-hub] Created config.json from example. Edit it and rerun."
fi
exec python -m mcp_hub --config config.json "$@"
