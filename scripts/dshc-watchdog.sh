#!/usr/bin/env bash
# M3-DSH 看门狗: 周期检查容器/数据/API 健康, 异常写入 logs/watchdog.log
# 用法: bash scripts/dshc-watchdog.sh [间隔秒数, 默认 300]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

INTERVAL="${1:-300}"
LOG="$DSHC_LOG_DIR/watchdog.log"
STATUS="${DSHC_FT_API_PORT:-18081}"
DASH="${DSHC_DASH_PORT:-18083}"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"

w() { printf '%s | %s | %s\n' "$(dshc_now_cst)" "$(dshc_now)" "$*" >> "$LOG"; }

w "看门狗启动 (间隔 ${INTERVAL}s, PID $$)"
while true; do
  ISSUES=0

  # 1) 容器是否在跑
  for c in market-collector freqtrade-dryrun dashboard; do
    st=$(docker inspect -f '{{.State.Status}}' "${DSHC_PREFIX}-$c" 2>/dev/null || echo missing)
    if [[ "$st" != "running" ]]; then
      w "[异常] 容器 ${DSHC_PREFIX}-$c 状态=$st"
      ISSUES=$((ISSUES+1))
    fi
  done

  # 2) 采集器心跳是否新鲜(15 分钟内)
  if ! python3 - "$DSHC_DATA_DIR/live/collector_status.json" <<'PYEOF'
import json, sys, time
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    raise SystemExit(1)
bad = [k for k, v in d.get("workers", {}).items()
       if (time.time()*1000 - (v.get("last_ok_ms") or 0)) / 1000 > max(900, v["interval"]*5)]
for k in bad:
    print(k)
raise SystemExit(1 if bad else 0)
PYEOF
  then
    w "[异常] 采集器心跳过期(详见上方 stderr 输出的任务名)"
    ISSUES=$((ISSUES+1))
  fi

  # 3) freqtrade API 与看板是否可用
  if ! curl -s -m 8 -u "$AUTH" "http://127.0.0.1:$STATUS/api/v1/show_config" | grep -q '"dry_run": *true'; then
    w "[异常] freqtrade REST API 无响应 (127.0.0.1:$STATUS)"
    ISSUES=$((ISSUES+1))
  fi
  if ! curl -s -m 8 "http://127.0.0.1:$DASH/health" | grep -q '"status":"ok"'; then
    w "[异常] 看板无响应 (127.0.0.1:$DASH)"
    ISSUES=$((ISSUES+1))
  fi

  # 4) 候选池是否新鲜(20 分钟内)
  AGE=$(python3 - "$DSHC_DATA_DIR/live/watchlist.json" <<'PYEOF'
import json, sys, time
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
    print(int((time.time()*1000 - d.get("generated_ms", 0)) / 1000))
except Exception:
    print(999999)
PYEOF
)
  if [[ "$AGE" -gt 1200 ]]; then
    w "[异常] 候选池 watchlist.json 陈旧 (${AGE}s)"
    ISSUES=$((ISSUES+1))
  fi

  # 5) 交易所限流检查(最近一轮日志里是否出现 429)
  N429=$(docker logs --since "${INTERVAL}s" "${DSHC_PREFIX}-freqtrade-dryrun" 2>&1 | grep -c '429 Too Many Requests' || true)
  if [[ "${N429:-0}" -gt 0 ]]; then
    w "[告警] 最近 ${INTERVAL}s 内出现 ${N429} 次币安 429 限流"
    ISSUES=$((ISSUES+1))
  fi

  if [[ $ISSUES -eq 0 ]]; then
    w "健康: 3 容器在线, 采集心跳正常, API/看板可用, 候选池距今 ${AGE}s"
  fi
  sleep "$INTERVAL"
done
