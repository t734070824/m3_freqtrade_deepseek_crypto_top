#!/usr/bin/env bash
# M3-DSH 持续计量: 每 N 秒把两个 dry-run 实验的账户/持仓/采集健康写入 logs/monitor.log
#
#   实验 A (主策略)  : m3dsc-freqtrade-dryrun  M3GainersTrend  动量甜区追涨
#   实验 B (对照组)  : m3dsc-freqtrade-dip     M3DipRevert      急跌反弹
#
# 用法: bash scripts/dshc-monitor.sh [间隔秒, 默认 300]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

INTERVAL="${1:-300}"
LOG="$DSHC_LOG_DIR/monitor.log"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"

snapshot() {   # $1=名称 $2=端口
  local name="$1" port="$2"
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$port/api/v1/profit" > "/tmp/m3mon_$name.p" 2>/dev/null || echo '{}' > "/tmp/m3mon_$name.p"
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$port/api/v1/status" > "/tmp/m3mon_$name.s" 2>/dev/null || echo '[]' > "/tmp/m3mon_$name.s"
}

printf '\n===== 监控启动 %s =====\n' "$(dshc_now_cst)" >> "$LOG"
while true; do
  TS="$(dshc_now_cst)"
  snapshot A "${DSHC_FT_API_PORT:-18081}"
  snapshot B "${DSHC_DIP_API_PORT:-18084}"

  python3 - "$TS" /tmp/m3mon_A.p /tmp/m3mon_A.s /tmp/m3mon_B.p /tmp/m3mon_B.s \
           "$DSHC_DATA_DIR/live/collector_status.json" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys
ts = sys.argv[1]
def load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d
out = []
for label, pf, sf, pfile, sfile in (
        ("A 追涨 M3GainersTrend", None, None, sys.argv[2], sys.argv[3]),
        ("B 反弹 M3DipRevert  ", None, None, sys.argv[4], sys.argv[5])):
    p = load(pfile, {})
    st = load(sfile, [])
    out.append("[%s] %s | 总盈亏 %+8.2f | 已平仓 %+8.2f | 持仓 %d | 交易 %s(平 %s) | 胜率 %.1f%%" % (
        ts, label, p.get("profit_all_coin", 0) or 0, p.get("profit_closed_coin", 0) or 0,
        len(st), p.get("trade_count", 0), p.get("closed_trade_count", 0),
        (p.get("winrate", 0) or 0) * 100))
    for t in st:
        out.append("      %-22s %s %+9.6g -> %+9.6g %+7.2f%% (%+7.2f) x%s" % (
            t.get("pair", ""), "空" if t.get("is_short") else "多", t.get("open_rate", 0),
            t.get("current_rate", 0), t.get("profit_pct", 0) or 0,
            t.get("profit_abs", 0) or 0, t.get("leverage", 1)))
try:
    c = json.load(open(sys.argv[6]))
    w = c.get("workers", {})
    out.append("      采集: " + " ".join("%s=%d" % (k, v.get("runs", 0)) for k, v in sorted(w.items())))
except Exception:
    pass
print("\n".join(out), flush=True)
PYEOF

  for c in freqtrade-dryrun freqtrade-dip; do
    SIG=$(docker logs --since "${INTERVAL}s" "${DSHC_PREFIX}-$c" 2>&1 | grep -E '信号率|评估' | tail -1)
    [[ -n "$SIG" ]] && echo "      [$c] $SIG" | sed 's/^ *//' >> "$LOG"
  done
  sleep "$INTERVAL"
done
