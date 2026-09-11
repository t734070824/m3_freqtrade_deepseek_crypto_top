#!/usr/bin/env bash
# 启动全部 M3-DSH 服务 (构建镜像 -> 起容器 -> 健康检查)
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

BUILD="${1:-}"

mkdir -p "$DSHC_DATA_DIR/live" "$DSHC_LOG_DIR"          "$DSHC_ROOT/freqtrade/user_data/strategies" "$DSHC_ROOT/freqtrade/user_data/logs"

if [[ "$BUILD" == "--build" ]]; then
  dshc_log "构建镜像 ..."
  $DC build --parallel
fi

dshc_log "启动容器 (前缀 $DSHC_PREFIX) ..."
$DC up -d

dshc_log "等待服务就绪 ..."
for i in $(seq 1 60); do
  ok_collector=$(docker inspect -f '{{.State.Running}}' "${DSHC_PREFIX}-market-collector" 2>/dev/null || echo false)
  [[ "$ok_collector" == "true" ]] && break
  sleep 3
done

dshc_log "容器状态:"
docker ps --filter "name=${DSHC_PREFIX}" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

dshc_log "看板:     http://127.0.0.1:${DSHC_DASH_PORT:-18082}/"
dshc_log "freqtrade 内部 API 端口: 127.0.0.1:${DSHC_FT_API_PORT:-18081}"
dshc_log "查看日志: bash scripts/dshc-logs.sh [collector|freqtrade|dashboard]"
