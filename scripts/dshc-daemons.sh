#!/usr/bin/env bash
# 启动/停止/查看 M3-DSH 的两个常驻观测进程:
#   monitor  每 5 分钟落盘账户/持仓/采集快照 -> logs/monitor.log
#   watchdog 每 5 分钟检查容器/心跳/API/候选池/429 -> logs/watchdog.log
# 用法: bash scripts/dshc-daemons.sh {start|stop|status}
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"

PID_DIR="$DSHC_ROOT/.run"
mkdir -p "$PID_DIR"

start_one() {
  local name="$1" script="$2" interval="${3:-300}"
  local pidfile="$PID_DIR/$name.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    dshc_log "$name 已在运行 (PID $(cat "$pidfile"))"
    return 0
  fi
  setsid nohup bash "$DSHC_ROOT/scripts/$script" "$interval" \
    >> "$DSHC_LOG_DIR/$name.stderr.log" 2>&1 &
  echo $! > "$pidfile"
  dshc_log "$name 已启动 (PID $(cat "$pidfile"), 间隔 ${interval}s)"
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
    start_one monitor dshc-monitor.sh "${2:-300}"
    start_one watchdog dshc-watchdog.sh "${2:-300}"
    sleep 3
    "$0" status
    ;;
  stop)  stop_one monitor; stop_one watchdog ;;
  status)
    for n in monitor watchdog; do
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
