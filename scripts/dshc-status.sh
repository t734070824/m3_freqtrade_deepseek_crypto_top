#!/usr/bin/env bash
# 一次性总览: 容器 / dry-run 账户 / 候选池 / 采集健康
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

echo "=================================================================="
echo " M3-DSH 状态总览   北京时间: $(dshc_now_cst)"
echo "                    UTC   : $(dshc_now)"
echo "=================================================================="

echo
echo "--- 容器 ---"
docker ps -a --filter "name=${DSHC_PREFIX}" \
  --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'

echo
echo "--- freqtrade dry-run 账户 ---"
curl -s -u "${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}" \
     "http://127.0.0.1:${DSHC_FT_API_PORT:-18081}/api/v1/profit" \
  | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    print(f"  总盈亏: {d.get(\"profit_all_coin\",0):+.2f} USDT  ({d.get(\"profit_all_percent\",0)*100:+.2f}%年化基准=账面)")
    print(f"  已平仓: {d.get(\"profit_closed_coin\",0):+.2f} USDT  胜率: {d.get(\"winrate\",0)*100:.1f}%  交易数: {d.get(\"closed_trade_count\",0)}")
    print(f"  当前浮盈: {d.get(\"profit_all_coin\",0)-d.get(\"profit_closed_coin\",0):+.2f} USDT")
except Exception as e:
    print("  (无法读取 freqtrade API:", e, ")")' 2>/dev/null || echo "  (freqtrade API 未就绪)"

echo
echo "--- 当前持仓 ---"
curl -s -u "${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}" \
     "http://127.0.0.1:${DSHC_FT_API_PORT:-18081}/api/v1/status" \
  | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
    if not d: print("  (无持仓)")
    for t in d:
        print(f"  {t[\"pair\"]:<22} {\"空\" if t.get(\"is_short\") else \"多\"}  {t[\"open_rate\"]:.6g} -> {t[\"current_rate\"]:.6g}  {t[\"profit_pct\"]*100:+.2f}% ({t[\"profit_abs\"]:+.2f} USDT)  x{t.get(\"leverage\",1)}")
except Exception as e:
    print("  (无法读取:", e, ")")' 2>/dev/null || echo "  (freqtrade API 未就绪)"

echo
echo "--- 候选池 TOP15 (score>0 利多 / score<0 利空) ---"
python3 - "$DSHC_DATA_DIR/live/watchlist.json" <<'PYEOF'
import json, sys, time
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("  (watchlist 尚未生成:", e, ")"); raise SystemExit
age = (time.time()*1000 - d.get("generated_ms",0))/1000
print(f"  生成于 {d.get('generated_cst','?')} (距今 {age:.0f}s), 可交易合约 {d.get('n_tradable','?')} 个, 恐贪 {d.get('macro',{}).get('fear_greed','n/a')}")
print(f"  {'合约':<14}{'得分':>8}{'24h涨幅':>10}{'年化费率':>11}{'成交额M':>10}  标签")
for c in d.get("candidates", [])[:15]:
    print(f"  {c['symbol']:<14}{c['score']:>8.1f}{c['change_24h']:>9.2f}%{c['funding_ann']*100:>10.1f}%{c['quote_vol']/1e6:>10.0f}  {' '.join(c.get('tags',[]))}")
PYEOF

echo
echo "--- 采集器健康 ---"
python3 - "$DSHC_DATA_DIR/live/collector_status.json" <<'PYEOF'
import json, sys, time
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("  (状态文件缺失:", e, ")"); raise SystemExit
print(f"  运行 {d.get('uptime_min',0):.1f} 分钟, HTTP {d.get('http',{})}")
now = time.time()*1000
for k, v in sorted(d.get("workers", {}).items()):
    age = (now - (v.get("last_ok_ms") or 0))/1000
    flag = "OK " if age < max(600, v["interval"]*4) else "!! "
    print(f"  {flag}{k:<12} 周期{v['interval']:>5}s 运行{v['runs']:>5} 错误{v['errors']:>3} 行{v['rows']:>7} 距今{age:>6.0f}s {v.get('last_error','')[:50]}")
PYEOF
echo
