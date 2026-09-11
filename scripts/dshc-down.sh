#!/usr/bin/env bash
# 停止全部 M3-DSH 服务 (默认保留数据卷目录)
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"
dshc_log "停止容器 (前缀 $DSHC_PREFIX) ..."
$DC down --remove-orphans "$@"
dshc_log "已停止。数据仍在 $DSHC_DATA_DIR"
