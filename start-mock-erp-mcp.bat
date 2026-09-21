@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" mcp_servers\mock_erp_server.py
) else (
  python mcp_servers\mock_erp_server.py
)
endlocal
