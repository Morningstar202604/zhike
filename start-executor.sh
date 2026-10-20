#!/usr/bin/env bash
# 启动执行器（可选组件；不启动时电脑操作动作会返回 503，对话功能不受影响）
set -e
cd "$(dirname "$0")"
[ -f policy.json ] || cp executor/policy.example.json policy.json
export EXECUTOR_POLICY_PATH="${EXECUTOR_POLICY_PATH:-$PWD/policy.json}"
PY=.venv/Scripts/python; [ -x "$PY" ] || PY=.venv/bin/python; [ -x "$PY" ] || PY=python
exec "$PY" -m executor.main
