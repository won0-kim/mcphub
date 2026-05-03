@echo off
setlocal
cd /d "%~dp0"
if not exist config.json (
  copy /y config.example.json config.json >nul
  echo [mcp-hub] Created config.json from example. Edit it and rerun.
)
python -m mcp_hub --config config.json %*
endlocal
