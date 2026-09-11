#!/usr/bin/env bash
# 端到端自检: 配置合法性 / 策略可加载 / 采集数据非空 / dry-run API / 看板
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"
FAILED=0
WARNED=0
PY="python3 $DSHC_ROOT/scripts/dshc_report.py"
FP="${DSHC_FT_API_PORT:-18081}"
DP="${DSHC_DASH_PORT:-18083}"
AUTH="${DSHC_FT_API_USER:-m3dsc}:${DSHC_FT_API_PASS}"

echo "=== M3-DSH 自检 ==="
echo "  北京时间: $(dshc_now_cst)"
echo "  UTC     : $(dshc_now)"

echo "--- 1. 配置 / 策略 ---"
if docker run --rm --user ftuser \
     -v "$DSHC_ROOT/config:/freqtrade/m3dsc_config:ro" \
     -v "$DSHC_ROOT/freqtrade/user_data:/freqtrade/user_data" \
     -v "$DSHC_DATA_DIR:/workspace/data:ro" -v "$DSHC_LOG_DIR:/workspace/logs" \
     --entrypoint freqtrade m3dsc-freqtrade:0.1.0 list-strategies \
     --config /freqtrade/m3dsc_config/config-dryrun.json \
     --strategy-path /freqtrade/user_data/strategies 2>&1 | grep -q "M3GainersTrend"; then
  echo "  [PASS] 策略 M3GainersTrend 可被 freqtrade 加载"
else
  echo "  [FAIL] 策略加载失败"; FAILED=$((FAILED+1))
fi

if docker run --rm --user ftuser \
     -v "$DSHC_ROOT/config:/freqtrade/m3dsc_config:ro" \
     -v "$DSHC_ROOT/freqtrade/user_data:/freqtrade/user_data" \
     -v "$DSHC_DATA_DIR:/workspace/data:ro" -v "$DSHC_LOG_DIR:/workspace/logs" \
     --entrypoint freqtrade m3dsc-freqtrade:0.1.0 show-config \
     --config /freqtrade/m3dsc_config/config-dryrun.json 2>&1 | grep -q "m3dsc-dashboard"; then
  echo "  [PASS] 动态 pairlist 指向看板 /api/pairlist"
else
  echo "  [WARN] pairlist 未体现, 需人工确认"; WARNED=$((WARNED+1))
fi

echo "--- 2. 容器 ---"
for c in market-collector freqtrade-dryrun dashboard; do
  st=$(docker inspect -f '{{.State.Status}}' "${DSHC_PREFIX}-$c" 2>/dev/null || echo missing)
  if [[ "$st" == "running" ]]; then echo "  [PASS] ${DSHC_PREFIX}-$c: running"
  else echo "  [FAIL] ${DSHC_PREFIX}-$c: $st"; FAILED=$((FAILED+1)); fi
done

echo "--- 3. 采集数据 ---"
$PY tables "$DSHC_DATA_DIR/m3dsc_market.db" | sed 's/^/  /'

echo "--- 4. dry-run 交易节点 ---"
if curl -s -m 8 -u "$AUTH" "http://127.0.0.1:$FP/api/v1/show_config" | grep -q '"dry_run": *true'; then
  echo "  [PASS] freqtrade API 可用且为 dry-run"
  curl -s -m 8 -u "$AUTH" "http://127.0.0.1:$FP/api/v1/status" > /tmp/m3dsc_status.json 2>/dev/null || echo '[]' > /tmp/m3dsc_status.json
  $PY status /tmp/m3dsc_status.json
  curl -s -m 8 -u "$AUTH" "http://127.0.0.1:$FP/api/v1/profit" > /tmp/m3dsc_profit.json 2>/dev/null || echo '{}' > /tmp/m3dsc_profit.json
  $PY profit /tmp/m3dsc_profit.json
else
  echo "  [FAIL] freqtrade API 不可用"; FAILED=$((FAILED+1))
fi

echo "--- 5. 看板 ---"
if curl -s -m 8 "http://127.0.0.1:$DP/health" | grep -q '"status":"ok"'; then
  echo "  [PASS] 看板可访问 http://127.0.0.1:$DP/"
  echo -n "  候选池(RemotePairList 数据源): "
  curl -s -m 8 "http://127.0.0.1:$DP/api/pairlist" | head -c 150; echo
else
  echo "  [FAIL] 看板不可访问"; FAILED=$((FAILED+1))
fi

echo
if [[ $FAILED -eq 0 ]]; then echo "=== 全部通过 (警告 $WARNED 项) ==="; else echo "=== 失败 $FAILED 项 / 警告 $WARNED 项 ==="; fi
exit $FAILED
