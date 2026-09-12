#!/usr/bin/env bash
# 启动/停止/查看 M3-DSH 的常驻观测进程:
#   monitor     每 5 分钟落盘账户/持仓/采集快照        -> logs/monitor.log
#   watchdog    每 5 分钟检查容器/心跳/API/候选池/429  -> logs/watchdog.log
#   attribution 每 30 分钟做统计判定, 达标/异常才报警  -> logs/attribution.log,
#                                                       logs/ALERTS.md, logs/attribution.jsonl
# 用法: bash scripts/dshc-daemons.sh {start|stop|status}
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

PID_DIR="$DSHC_ROOT/.run"
mkdir -p "$PID_DIR"

# 启动一个常驻进程: start_one <名字> <命令...>
start_one() {
  local name="$1"; shift
  local pidfile="$PID_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    dshc_log "$name 已在运行 (PID $(cat "$pidfile"))"
    return 0
  fi
  setsid nohup "$@" >> "$DSHC_LOG_DIR/$name.stderr.log" 2>&1 &
  echo $! > "$pidfile"
  dshc_log "$name 已启动 (PID $(cat "$pidfile"))"
}

stop_one() {
  local name="$1" pidfile="$PID_DIR/$1.pid"
  if [[ -f "$pidfile" ]]; then
    kill "$(cat "$pidfile")" 2>/dev/null || true
    rm -f "$pidfile"
    dshc_log "$name 已停止"
  fi
}

case "${1:-status}" in
  start)
    local_interval="${2:-300}"
    start_one monitor bash "$DSHC_ROOT/scripts/dshc-monitor.sh" "$local_interval"
    start_one watchdog bash "$DSHC_ROOT/scripts/dshc-watchdog.sh" "$local_interval"
    # 归因守护: 默认 30 分钟一轮, 样本门槛 20 笔
    start_one attribution python3 "$DSHC_ROOT/scripts/dshc_attribution.py" \
      --interval "${DSHC_ATTR_INTERVAL:-1800}" --min-trades "${DSHC_ATTR_MIN_TRADES:-20}"
    sleep 3
    "$0" status
    ;;
  stop)  stop_one monitor; stop_one watchdog; stop_one attribution ;;
  status)
    for n in monitor watchdog attribution; do
      pidfile="$PID_DIR/$n.pid"
      if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
        echo "  [运行中] $n (PID $(cat "$pidfile"))"
      else
        echo "  [已停止] $n"
      fi
    done
    ;;
  *) echo "用法: $0 {start|stop|status} [间隔秒]"; exit 1 ;;
esac
