#!/usr/bin/env bash
# M3-DSH 持续计量: 每 5 分钟把账户/持仓/候选池/信号率快照追加到 logs/monitor.log
# 目的: 在 dry-run 迭代中形成可回看的连续时间序列, 而不是靠零散的手工查看。
# 用法: bash scripts/dshc-monitor.sh [间隔秒, 默认 300]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

INTERVAL="${1:-300}"
LOG="$DSHC_LOG_DIR/monitor.log"
FP="${DSHC_FT_API_PORT:-18081}"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"

printf '\n===== 监控启动 %s =====\n' "$(dshc_now_cst)" >> "$LOG"
while true; do
  TS="$(dshc_now_cst)"
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$FP/api/v1/profit" > /tmp/m3mon_p.json 2>/dev/null || echo '{}' > /tmp/m3mon_p.json
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$FP/api/v1/status" > /tmp/m3mon_s.json 2>/dev/null || echo '[]' > /tmp/m3mon_s.json
  python3 - "$TS" /tmp/m3mon_p.json /tmp/m3mon_s.json "$DSHC_DATA_DIR/live/collector_status.json" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys, time
ts, pfile, sfile, cfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
try:
    p = json.load(open(pfile))
except Exception:
    p = {}
try:
    st = json.load(open(sfile))
except Exception:
    st = []
lines = ["[%s] 总盈亏 %+.2f | 已平仓 %+.2f | 持仓 %d 笔 | 交易 %s (平仓 %s) | 胜率 %.1f%%" % (
    ts, p.get("profit_all_coin", 0) or 0, p.get("profit_closed_coin", 0) or 0, len(st),
    p.get("trade_count", 0), p.get("closed_trade_count", 0), (p.get("winrate", 0) or 0) * 100)]
for t in st:
    lines.append("    持仓 %-22s %s %+.6g -> %+.6g  %+.2f%% (%+.2f) x%s" % (
        t.get("pair", ""), "空" if t.get("is_short") else "多", t.get("open_rate", 0),
        t.get("current_rate", 0), t.get("profit_pct", 0) or 0, t.get("profit_abs", 0) or 0,
        t.get("leverage", 1)))
try:
    c = json.load(open(cfile))
    w = c.get("workers", {})
    lines.append("    采集: " + " ".join("%s=%d" % (k, v.get("runs", 0)) for k, v in sorted(w.items())))
except Exception:
    pass
print("\n".join(lines), flush=True)
PYEOF
  # 信号率(策略日志)
  SIG=$(docker logs --since "${INTERVAL}s" "${DSHC_PREFIX}-freqtrade-dryrun" 2>&1 | grep '信号率' | tail -1)
  [[ -n "$SIG" ]] && echo "    $SIG" >> "$LOG"
  sleep "$INTERVAL"
done
