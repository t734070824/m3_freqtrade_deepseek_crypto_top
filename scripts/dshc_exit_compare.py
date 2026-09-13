#!/usr/bin/env python3
"""出场设计对照: 阶梯止盈档位 / 最长持有 的组合测试 (急跌买入方向).

背景:
    G 用的是「阶梯 +3.5% / +8% + 最长 6 小时」, 而 B 是「反弹即走(中位 5 分钟)」,
    F 是「阶梯 +6% / +15% + 最长 24 小时」。这三套出场从未被同一份数据一起比过。
    本脚本直接在急跌事件上模拟, 给出同一入场条件下不同出场设计的期望。

方法(逐分钟快照近似, 不含手续费/滑点, 故只看相对优劣):
    对每个急跌事件, 从入场点向前逐分钟走:
      - 若先行触及某个阶梯止盈档 -> 记为命中该档, 按该档价格退出;
      - 若先行触及止损 -> 记为止损退出;
      - 否则到最长持有时间按市价退出。
    对同一批事件, 换不同档位/时长各跑一遍, 横向对比。
时间口径: 输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MARKET_DB = ROOT / "data" / "m3dsc_market.db"
WATCHLIST = ROOT / "data" / "live" / "watchlist.json"
CST = timezone(timedelta(hours=8))


def stamp(ms: int) -> str:
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return "%s 北京时间 / %s UTC" % (dt.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S"),
                                     dt.strftime("%Y-%m-%d %H:%M:%S"))


def dedup(hits, gap_min):
    out = []
    gap_ms = gap_min * 60_000
    for ts, px in hits:
        if not out or ts - out[-1][0] > gap_ms:
            out.append((ts, px))
    return out


def simulate(ar, i0, tiers, stop_pct, max_min, cost_pct):
    """从 ar[i0] 入场, 返回净收益率%(单边成本 cost_pct 已计入)."""
    px0 = ar[i0][1]
    stop_px = px0 * (1 + stop_pct / 100.0)
    n_left, realized = 1.0, 0.0
    nxt = 0
    end = min(i0 + max_min, len(ar) - 1)
    for k in range(i0 + 1, end + 1):
        px = ar[k][1]
        if px <= stop_px:
            return realized + n_left * ((px / px0 - 1) * 100 - cost_pct)
        while nxt < len(tiers) and px >= px0 * (1 + tiers[nxt] / 100.0):
            share = 0.40
            realized += n_left * share * (tiers[nxt] - cost_pct)
            n_left -= n_left * share
            nxt += 1
        if n_left <= 0.01:
            return realized
    px = ar[end][1]
    return realized + n_left * ((px / px0 - 1) * 100 - cost_pct)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=34.0)
    ap.add_argument("--gap", type=int, default=60)
    ap.add_argument("--cost", type=float, default=0.10, help="单边成本%%(手续费+滑点)")
    args = ap.parse_args()

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since = now_ms - int(args.hours * 3600_000)
    db = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    pool = {c["symbol"] for c in wl.get("candidates", []) if c.get("symbol")}

    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for sym, ts, px in db.execute(
            "select symbol, ts_ms, price from ticker_snap where ts_ms>=? order by symbol, ts_ms", (since,)):
        series[sym].append((ts, px))

    events: list[tuple[str, int]] = []
    for sym, ar in series.items():
        if sym not in pool or len(ar) < 70:
            continue
        hits = []
        for i in range(60, len(ar)):
            if (ar[i][1] / ar[i - 15][1] - 1) * 100 <= -3.5:
                hits.append((ar[i][0], ar[i][1]))
        for ts, _ in dedup(hits, args.gap):
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts), None)
            if j is not None and j < len(ar) - 10:
                events.append((sym, j))

    print("=" * 104)
    print("出场设计对照 —— 同一批急跌事件, 不同出场参数")
    print("  窗口: %s 起, %.1f 小时 | 入场: 15m<=-3.5%% 急跌(事件去重后 %d 次, %d 只标的)"
          % (stamp(since), args.hours, len(events), len(set(s for s, _ in events))))
    print("  逐分钟快照模拟, 单边成本 %.2f%%; 阶梯每次减仓 40%%; 止损 -5%%" % args.cost)
    print("=" * 104)
    print()
    print("%-38s %8s %9s %9s %9s" % ("出场设计", "样本", "均值%", "中位数%", "胜率%"))
    print("-" * 104)

    designs = [
        ("F: 阶梯 +6/+15, 最长 24 小时", (6.0, 15.0), 1440),
        ("F': 阶梯 +6/+15, 最长 6 小时", (6.0, 15.0), 360),
        ("G: 阶梯 +3.5/+8, 最长 6 小时", (3.5, 8.0), 360),
        ("G': 阶梯 +3.5/+8, 最长 3 小时", (3.5, 8.0), 180),
        ("B: 单一 +4% 即走, 最长 1 小时", (4.0,), 60),
        ("B': 单一 +4% 即走, 最长 6 小时", (4.0,), 360),
        ("B'': 单一 +2.5% 即走, 最长 1 小时", (2.5,), 60),
        ("无阶梯: 纯持有 1 小时", (), 60),
        ("无阶梯: 纯持有 6 小时", (), 360),
    ]
    for label, tiers, maxmin in designs:
        rets = [simulate(series[s], i, tiers, -5.0, maxmin, args.cost) for s, i in events]
        print("%-38s %8d %+9.3f %+9.3f %9.1f" % (
            label, len(rets), sum(rets) / len(rets), statistics.median(rets),
            sum(1 for r in rets if r > 0) / len(rets) * 100))

    print()
    print("注: 逐分钟快照无法反映盘中极值(真实盘中可能先触档再回头), 因此以上比较只用于")
    print("    判断出场设计的**相对优劣**, 不代表真实成交价。所有结果均未含复利与仓位差异。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
