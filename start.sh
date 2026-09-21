#!/usr/bin/env bash
set -eu
cd -- "$(dirname -- "$0")"
if [ -x .venv/bin/python ]; then PY=.venv/bin/python; elif command -v python3 >/dev/null 2>&1; then PY=python3; elif command -v python >/dev/null 2>&1; then PY=python; else
  printf '%s\n' '需要先安装 Python 3.10+。此包不含 Python 解释器。'; exit 1
fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else "需要 Python 3.10 或以上")'
exec "$PY" server.py --open "$@"
