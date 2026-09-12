#!/usr/bin/env bash
# M3-DSH 多实验持续计量: 每 N 秒把各 dry-run 的账户/持仓/采集健康写入 logs/monitor.log
#
#   A 追涨   m3dsc-freqtrade-dryrun  M3GainersTrend   动量延续(甜区)      :18081
#   B 反弹   m3dsc-freqtrade-dip     M3DipRevert      急跌反弹            :18084
#   C Carry  m3dsc-freqtrade-carry   M3CarryLong      负费率长持          :18085
#   D 突破   m3dsc-freqtrade-vol     M3VolBreakout    波动压缩放量突破     :18086
#
# 用法: bash scripts/dshc-monitor.sh [间隔秒, 默认 300]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

INTERVAL="${1:-300}"
LOG="$DSHC_LOG_DIR/monitor.log"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"
P_A="${DSHC_FT_API_PORT:-18081}"
P_B="${DSHC_DIP_API_PORT:-18084}"
P_C="${DSHC_CARRY_API_PORT:-18085}"
P_D="${DSHC_VOL_API_PORT:-18086}"

snap() {   # $1=key $2=port
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$2/api/v1/profit" > "/tmp/m3mon_$1.p" 2>/dev/null || echo '{}' > "/tmp/m3mon_$1.p"
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$2/api/v1/status" > "/tmp/m3mon_$1.s" 2>/dev/null || echo '[]' > "/tmp/m3mon_$1.s"
}

printf '\n===== 监控启动 %s =====\n' "$(dshc_now_cst)" >> "$LOG"
while true; do
  TS="$(dshc_now_cst)"
  snap A "$P_A"; snap B "$P_B"; snap C "$P_C"; snap D "$P_D"

  python3 - "$TS" /tmp/m3mon_A.p /tmp/m3mon_A.s /tmp/m3mon_B.p /tmp/m3mon_B.s \
           /tmp/m3mon_C.p /tmp/m3mon_C.s /tmp/m3mon_D.p /tmp/m3mon_D.s \
           "$DSHC_DATA_DIR/live/collector_status.json" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys
ts = sys.argv[1]
def load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d
labels = [("A 追涨 ", sys.argv[2], sys.argv[3]), ("B 反弹 ", sys.argv[4], sys.argv[5]),
          ("C Carry", sys.argv[6], sys.argv[7]), ("D 突破 ", sys.argv[8], sys.argv[9])]
out = []
for label, pfile, sfile in labels:
    p = load(pfile, {}); st = load(sfile, [])
    out.append("[%s] %s | 权益 %+8.2f | 平仓 %+8.2f | 持仓 %d | 交易 %s(平 %s) | 胜率 %.1f%%" % (
        ts, label, p.get("profit_all_coin", 0) or 0, p.get("profit_closed_coin", 0) or 0,
        len(st), p.get("trade_count", 0), p.get("closed_trade_count", 0),
        (p.get("winrate", 0) or 0) * 100))
    for t in st:
        out.append("      %-22s %s %+9.6g -> %+9.6g %+7.2f%% (%+7.2f) x%s" % (
            t.get("pair", ""), "空" if t.get("is_short") else "多", t.get("open_rate", 0),
            t.get("current_rate", 0), t.get("profit_pct", 0) or 0,
            t.get("profit_abs", 0) or 0, t.get("leverage", 1)))
try:
    c = json.load(open(sys.argv[10]))
    w = c.get("workers", {})
    out.append("      采集: " + " ".join("%s=%d" % (k, v.get("runs", 0)) for k, v in sorted(w.items())))
except Exception:
    pass
print("\n".join(out), flush=True)
PYEOF

  for c in freqtrade-dryrun freqtrade-dip freqtrade-carry freqtrade-vol; do
    N429=$(docker logs --since "${INTERVAL}s" "${DSHC_PREFIX}-$c" 2>&1 | grep -c '429' || true)
    [[ "${N429:-0}" -gt 0 ]] && echo "      [告警] $c 429 x$N429" >> "$LOG"
  done
  sleep "$INTERVAL"
done
