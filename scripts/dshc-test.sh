#!/usr/bin/env bash
# 运行 M3-DSH 单元测试(风控数学等). 纯标准库 + pytest, 本地即可执行。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"
PY="${DSHC_PYTHON:-$DSHC_ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  if command -v python3 >/dev/null; then PY=python3; else echo "找不到 python"; exit 1; fi
fi
if ! "$PY" -c 'import pytest' 2>/dev/null; then
  echo "[提示] 安装 pytest: $PY -m pip install pytest"
  exit 1
fi
dshc_log "运行单元测试: freqtrade/tests/unit"
"$PY" -m pytest -q "$DSHC_ROOT/freqtrade/tests/unit" "$@"
