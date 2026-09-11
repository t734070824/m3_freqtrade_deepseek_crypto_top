#!/usr/bin/env bash
# M3-DSH 公共环境: 所有运维脚本统一 source 本文件
# 容器名前缀固定为 m3dsc (M3-DSH DeepSeek Crypto Top 的唯一标记)
set -euo pipefail

DSHC_ROOT="${DSHC_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$DSHC_ROOT"

if [[ -f "$DSHC_ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$DSHC_ROOT/.env"
  set +a
fi

export DSHC_PREFIX="${DSHC_PREFIX:-m3dsc}"
export DSHC_DATA_DIR="${DSHC_DATA_DIR:-$DSHC_ROOT/data}"
export DSHC_LOG_DIR="${DSHC_LOG_DIR:-$DSHC_ROOT/logs}"
export DSHC_ROOT

DC="docker compose -f $DSHC_ROOT/docker-compose.yml --env-file $DSHC_ROOT/.env"

dshc_now() { date -u '+%Y-%m-%d %H:%M:%S UTC'; }
dshc_now_cst() { TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S 北京时间(UTC+8)'; }
dshc_log() { printf '\033[36m[%s | %s]\033[0m %s\n' "$(dshc_now_cst)" "$(dshc_now)" "$*"; }
dshc_err() { printf '\033[31m[%s] %s\033[0m\n' "$(dshc_now_cst)" "$*" >&2; }
