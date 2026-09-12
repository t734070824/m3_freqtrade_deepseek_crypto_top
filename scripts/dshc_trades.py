"""交易归因分析: 从 freqtrade REST API 拉取全部交易, 按离场原因/方向/入场标签统计.

用法:
    python3 scripts/dshc_trades.py            # 汇总
    python3 scripts/dshc_trades.py --list 20  # 明细(最近 20 笔)
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _auth() -> str:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    user = os.environ.get("DSHC_FT_API_USER") or env.get("DSHC_FT_API_USER", "m3dsc")
    pw = os.environ.get("DSHC_FT_API_PASS") or env.get("DSHC_FT_API_PASS", "")
    return base64.b64encode(f"{user}:{pw}".encode()).decode()


def fetch(path: str) -> dict:
    base = os.environ.get("DSHC_FT_API", "http://127.0.0.1:18081")
    req = urllib.request.Request(f"{base}{path}",
                                 headers={"Authorization": f"Basic {_auth()}"})
    with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
        return json.loads(r.read().decode())


def fmt_both(ms: float) -> str:
    return (time.strftime("%m-%d %H:%M", time.localtime(ms / 1000 + 8 * 3600))
            + " CST / " + time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000)) + " UTC")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", type=int, default=0, help="打印最近 N 笔明细")
    args = ap.parse_args()

    tr = fetch("/api/v1/trades?limit=1000")
    trades = tr.get("trades", tr) if isinstance(tr, dict) else tr
    closed = [t for t in trades if not t.get("is_open")]
    open_t = [t for t in trades if t.get("is_open")]
    pf = fetch("/api/v1/profit")

    print("=" * 78)
    print("M3-DSH 交易归因 (数据源: freqtrade REST, 全部时间为 UTC)")
    print("=" * 78)
    print("账户: 总盈亏 %+.2f USDT | 已平仓 %+.2f | 未平仓 %+.2f | 交易 %s (已平仓 %s) | 胜率 %.1f%%"
          % (pf.get("profit_all_coin", 0), pf.get("profit_closed_coin", 0),
             pf.get("profit_all_coin", 0) - pf.get("profit_closed_coin", 0),
             pf.get("trade_count", 0), pf.get("closed_trade_count", 0),
             (pf.get("winrate", 0) or 0) * 100))
    print()

    if not closed:
        print("暂无已平仓交易")
        return 0

    # ---------------- 总体盈亏结构 ----------------
    profits = [(t.get("profit_abs") or 0) for t in closed]
    wins = [p for p in profits if p > 0]
    losses = [p for p in profits if p <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    print("--- 盈亏结构 ---")
    print("  盈利笔数 %d  合计 %+.2f   平均 %+.2f   中位 %+.2f"
          % (len(wins), gp, statistics.fmean(wins) if wins else 0,
             statistics.median(wins) if wins else 0))
    print("  亏损笔数 %d  合计 %+.2f   平均 %+.2f   中位 %+.2f"
          % (len(losses), -gl, statistics.fmean(losses) if losses else 0,
             statistics.median(losses) if losses else 0))
    print("  盈亏比(总盈利/总亏损) = %.2f   期望值/笔 = %+.3f USDT   利润因子 = %.2f"
          % (gp / gl if gl else float("inf"), statistics.fmean(profits),
             gp / gl if gl else float("inf")))
    fees = sum((t.get("fee_open") or 0) + (t.get("fee_close") or 0) for t in closed)
    funding = sum((t.get("funding_fees") or 0) for t in closed)
    print("  手续费合计 %.2f USDT   资金费合计 %+.2f USDT  (资金费为正=收到补贴)"
          % (fees, funding))
    print()

    # ---------------- 按离场原因 ----------------
    by_reason: dict[str, list[dict]] = defaultdict(list)
    for t in closed:
        by_reason[t.get("exit_reason") or "?"].append(t)
    print("--- 按离场原因 ---")
    print("  %-28s %5s %10s %10s %10s %8s" % ("原因", "笔数", "合计", "平均", "最好", "最差"))
    for reason, ts in sorted(by_reason.items(), key=lambda kv: sum(
            (x.get("profit_abs") or 0) for x in kv[1])):
        ps = [(x.get("profit_abs") or 0) for x in ts]
        print("  %-28s %5d %+10.2f %+10.2f %+10.2f %+10.2f"
              % (reason, len(ps), sum(ps), statistics.fmean(ps), max(ps), min(ps)))
    print()

    # ---------------- 按方向 ----------------
    by_dir: dict[str, list[dict]] = defaultdict(list)
    for t in closed:
        by_dir["空" if t.get("is_short") else "多"].append(t)
    print("--- 按方向 ---")
    for d, ts in by_dir.items():
        ps = [(x.get("profit_abs") or 0) for x in ts]
        print("  %s: %d 笔 合计 %+.2f 平均 %+.2f 胜率 %.0f%%"
              % (d, len(ps), sum(ps), statistics.fmean(ps),
                 sum(1 for p in ps if p > 0) / len(ps) * 100))
    print()

    # ---------------- 按入场标签 ----------------
    by_tag: dict[str, list[dict]] = defaultdict(list)
    for t in closed:
        by_tag[t.get("enter_tag") or "?"].append(t)
    print("--- 按入场信号 ---")
    for tag, ts in by_tag.items():
        ps = [(x.get("profit_abs") or 0) for x in ts]
        print("  %-20s %3d 笔 合计 %+8.2f 平均 %+7.3f 胜率 %.0f%%"
              % (tag, len(ps), sum(ps), statistics.fmean(ps),
                 sum(1 for p in ps if p > 0) / len(ps) * 100))
    print()

    # ---------------- 按标的 ----------------
    by_pair: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        by_pair[t.get("pair", "?")].append(t.get("profit_abs") or 0)
    worst = sorted(by_pair.items(), key=lambda kv: sum(kv[1]))[:8]
    print("--- 亏损最多的标的 ---")
    for pair, ps in worst:
        print("  %-24s %2d 笔 合计 %+8.2f" % (pair, len(ps), sum(ps)))
    print()

    # ---------------- 持仓时长 ----------------
    durs = []
    for t in closed:
        try:
            from datetime import datetime
            a = datetime.fromisoformat(t["open_date"]); b = datetime.fromisoformat(t["close_date"])
            durs.append((b - a).total_seconds() / 60)
        except Exception:
            pass
    if durs:
        import collections
        buckets = collections.Counter()
        for d in durs:
            if d < 5: buckets["<5分钟"] += 1
            elif d < 15: buckets["5-15分钟"] += 1
            elif d < 60: buckets["15-60分钟"] += 1
            elif d < 240: buckets["1-4小时"] += 1
            else: buckets[">4小时"] += 1
        print("--- 持仓时长分布 ---")
        for k in ("<5分钟", "5-15分钟", "15-60分钟", "1-4小时", ">4小时"):
            if buckets[k]:
                print("  %-12s %d 笔" % (k, buckets[k]))
        print("  平均 %.0f 分钟  中位 %.0f 分钟" % (statistics.fmean(durs), statistics.median(durs)))
    print()

    if args.list:
        print("--- 最近 %d 笔明细 ---" % args.list)
        print("  %-20s %-4s %-8s %10s %9s %9s %-24s %s"
              % ("标的", "方向", "开仓", "收益率%", "盈亏", "资金费", "离场原因", "入场信号"))
        for t in closed[-args.list:]:
            print("  %-20s %-4s %-8s %+10.2f %+9.2f %+9.4f %-24s %s"
                  % (t.get("pair", ""), "空" if t.get("is_short") else "多",
                     fmt_both(t.get("open_timestamp", 0)).split(" ")[0],
                     (t.get("profit_ratio") or 0) * 100, t.get("profit_abs") or 0,
                     t.get("funding_fees") or 0, t.get("exit_reason") or "",
                     t.get("enter_tag") or ""))
    if open_t:
        print()
        print("--- 当前未平仓 %d 笔 ---" % len(open_t))
        for t in open_t:
            print("  %-20s %-4s 开仓 %s  开仓价 现价 %+.2f%%"
                  % (t.get("pair", ""), "空" if t.get("is_short") else "多",
                     fmt_both(t.get("open_timestamp", 0)), t.get("profit_pct") or 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
