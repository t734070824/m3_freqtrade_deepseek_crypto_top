#!/usr/bin/env bash
# 进入容器: bash scripts/dshc-shell.sh [collector|freqtrade|dashboard]
source "$(dirname "${BASH_SOURCE[0]}")/dshc-env.sh"
WHAT="${1:-freqtrade}"
docker exec -it "${DSHC_PREFIX}-${WHAT}" /bin/bash 2>/dev/null || docker exec -it "${DSHC_PREFIX}-${WHAT}" /bin/sh
