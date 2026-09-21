@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    ".venv\Scripts\python.exe" server.py --open %*
    goto end
  )
)
where py >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    py -3 server.py --open %*
    goto end
  )
)
where python >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>nul
  if not errorlevel 1 (
    python server.py --open %*
    goto end
  )
)
echo 需要 Python 3.10 或更高版本。此 ZIP 不含 Python 解释器。
:end
pause
endlocal
