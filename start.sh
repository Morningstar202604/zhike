#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python -m venv .venv
PY=.venv/Scripts/python; [ -x "$PY" ] || PY=.venv/bin/python
"$PY" -m pip install -q -r requirements.txt
"$PY" -m uvicorn core.main:app --host 0.0.0.0 --port 8000
