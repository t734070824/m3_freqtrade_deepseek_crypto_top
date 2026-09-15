#!/usr/bin/env bash
# M3-DSH 多实验持续计量: 每 N 秒把各 dry-run 的账户/持仓/采集健康写入 logs/monitor.log
#
#   I 正费率15m m3dsc-freqtrade-htrend15 M3DipTrend15  正费率+15m急跌     :18081
#   B 反弹   m3dsc-freqtrade-dip     M3DipRevert      急跌反弹(无费率)     :18084
#           (F 负费率+急跌已判失败; C 负费率长持已停用 —— 两者的档位均已移交)
#   G 融合   m3dsc-freqtrade-turbo  M3CarryDipTurbo  负费率+急跌 短持快收 :18087
#   H 正费率 m3dsc-freqtrade-htrend M3DipTrend       正费率+1h急跌 买回调 :18086
#           (E 费率空已停用: 34 小时数据显示做空方向不成立; 档位移交 G)
#           (D 突破已停用: 4 笔全亏 -12.32, 突破代理去极值 -0.266%; 档位移交 H)
#
# 用法: bash scripts/dshc-monitor.sh [间隔秒, 默认 300]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

INTERVAL="${1:-300}"
LOG="$DSHC_LOG_DIR/monitor.log"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"
P_B="${DSHC_DIP_API_PORT:-18084}"
P_H="${DSHC_HTREND_API_PORT:-18086}"
P_I="${DSHC_HTREND15_API_PORT:-18081}"
P_G="${DSHC_TURBO_API_PORT:-18087}"

snap() {   # $1=key $2=port
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$2/api/v1/profit" > "/tmp/m3mon_$1.p" 2>/dev/null || echo '{}' > "/tmp/m3mon_$1.p"
  curl -s -m 10 -u "$AUTH" "http://127.0.0.1:$2/api/v1/status" > "/tmp/m3mon_$1.s" 2>/dev/null || echo '[]' > "/tmp/m3mon_$1.s"
}

printf '\n===== 监控启动 %s =====\n' "$(dshc_now_cst)" >> "$LOG"
while true; do
  TS="$(dshc_now_cst)"
  snap B "$P_B"; snap G "$P_G"; snap H "$P_H"; snap I "$P_I"

  python3 - "$TS" /tmp/m3mon_B.p /tmp/m3mon_B.s /tmp/m3mon_G.p /tmp/m3mon_G.s \
           /tmp/m3mon_H.p /tmp/m3mon_H.s /tmp/m3mon_I.p /tmp/m3mon_I.s \
           "$DSHC_DATA_DIR/live/collector_status.json" >> "$LOG" 2>&1 <<'PYEOF'
import json, sys
ts = sys.argv[1]
def load(p, d):
    try:
        return json.load(open(p))
    except Exception:
        return d
labels = [("B 无费率 ", sys.argv[2], sys.argv[3]), ("G 负费率 ", sys.argv[4], sys.argv[5]),
          ("H 正费1h ", sys.argv[6], sys.argv[7]), ("I 正费15m", sys.argv[8], sys.argv[9])]
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
    c = json.load(open(sys.argv[12]))
    w = c.get("workers", {})
    out.append("      采集: " + " ".join("%s=%d" % (k, v.get("runs", 0)) for k, v in sorted(w.items())))
except Exception:
    pass
print("\n".join(out), flush=True)
PYEOF

  for c in freqtrade-dip freqtrade-turbo freqtrade-htrend freqtrade-htrend15; do
    N429=$(docker logs --since "${INTERVAL}s" "${DSHC_PREFIX}-$c" 2>&1 | grep -c '429' || true)
    [[ "${N429:-0}" -gt 0 ]] && echo "      [告警] $c 429 x$N429" >> "$LOG"
  done
  sleep "$INTERVAL"
done
