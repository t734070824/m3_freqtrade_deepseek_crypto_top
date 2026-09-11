#!/usr/bin/env bash
# 首次初始化: 生成 .env / 准备目录与权限 / 重置数据(可选)
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

if [[ ! -f "$DSHC_ROOT/.env" ]]; then
  dshc_log "生成 .env (含随机 API 口令) ..."
  PW=$(openssl rand -hex 12); JWT=$(openssl rand -hex 24); WS=$(openssl rand -hex 16)
  sed -e "s|^DSHC_FT_API_PASS=.*|DSHC_FT_API_PASS=$PW|" "$DSHC_ROOT/.env.example" > "$DSHC_ROOT/.env"
  {
    echo "DSHC_FT_JWT_SECRET=$JWT"
    echo "DSHC_FT_WS_TOKEN=$WS"
  } >> "$DSHC_ROOT/.env"
  chmod 600 "$DSHC_ROOT/.env"
  dshc_log "已生成 .env"
else
  dshc_log ".env 已存在, 跳过生成"
fi

dshc_log "创建目录 ..."
mkdir -p "$DSHC_DATA_DIR/live" "$DSHC_LOG_DIR" \
         "$DSHC_ROOT/freqtrade/user_data/strategies" \
         "$DSHC_ROOT/freqtrade/user_data/logs" \
         "$DSHC_ROOT/config"

# freqtrade 容器以 uid 1000 (ftuser) 运行, 需要的目录必须可写/可读
chmod -R 777 "$DSHC_DATA_DIR" "$DSHC_LOG_DIR" "$DSHC_ROOT/freqtrade/user_data" 2>/dev/null || true
chmod 755 "$DSHC_ROOT/config" 2>/dev/null || true
chmod 644 "$DSHC_ROOT"/config/*.json 2>/dev/null || true
dshc_log "目录权限已就绪 (数据目录对 uid 1000 可写, 配置对 uid 1000 可读)"

if [[ "${1:-}" == "--reset" ]]; then
  dshc_log "重置所有运行时数据 ..."
  rm -f "$DSHC_DATA_DIR"/m3dsc_*.db* "$DSHC_DATA_DIR/live/"* "$DSHC_LOG_DIR"/*.log
  rm -f "$DSHC_ROOT/freqtrade/user_data/"*.sqlite* 
  dshc_log "已重置"
fi

dshc_log "初始化完成。下一步: bash scripts/dshc-up.sh --build"
