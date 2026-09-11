#!/usr/bin/env bash
# 查看日志: bash scripts/dshc-logs.sh [collector|freqtrade|dashboard] [行数]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"
WHAT="${1:-collector}"
N="${2:-60}"
case "$WHAT" in
  collector) tail -n "$N" "$DSHC_LOG_DIR/collector.log" ;;
  freqtrade) tail -n "$N" "$DSHC_LOG_DIR/freqtrade-dryrun.log" ;;
  dashboard) docker logs --tail "$N" "${DSHC_PREFIX}-dashboard" 2>&1 ;;
  docker)    docker compose -f "$DSHC_ROOT/docker-compose.yml" logs --tail "$N" ;;
  *) echo "用法: $0 [collector|freqtrade|dashboard|docker] [行数]"; exit 1 ;;
esac
