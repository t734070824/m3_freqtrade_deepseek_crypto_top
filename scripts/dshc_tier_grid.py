#!/usr/bin/env python3
"""阶梯止盈档位灵敏度: 第一档 / 第二档 / 最长持有 的网格扫描.

目的: G 的上线参数(第一档 +3.5%, 最长 6 小时)是拍脑袋定的。本脚本在真实的急跌事件上
      把三个参数做网格扫描, 看期望值随参数怎么变 —— 是单调的、有峰值的, 还是纯噪声。

方法: 逐分钟快照 + 事件去重; 单边成本 0.10%; 止损 -5%; 每档减仓 40%。
      同时输出「均值」与「中位数」两列 —— 均值反应总收益(受离群事件影响大),
      中位数反应"每笔交易是否赚钱"(更接近实盘体感)。两者背离时, 说明收益靠少数极端事件。
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
    return "%s 北京时间 / %s UTC" % (dt.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                     dt.strftime("%Y-%m-%d %H:%M"))


def dedup(hits, gap_min):
    out, gap_ms = [], gap_min * 60_000
    for ts, px in hits:
        if not out or ts - out[-1][0] > gap_ms:
            out.append((ts, px))
    return out


def simulate(ar, i0, t1, t2, max_min, cost):
    px0 = ar[i0][1]
    stop_px = px0 * 0.95
    n_left, realized, nxt = 1.0, 0.0, 0
    tiers = [t for t in (t1, t2) if t]
    tiers.sort()
    end = min(i0 + max_min, len(ar) - 1)
    for k in range(i0 + 1, end + 1):
        px = ar[k][1]
        if px <= stop_px:
            return realized + n_left * ((px / px0 - 1) * 100 - cost)
        while nxt < len(tiers) and px >= px0 * (1 + tiers[nxt] / 100.0):
            realized += n_left * 0.40 * (tiers[nxt] - cost)
            n_left *= 0.60
            nxt += 1
        if n_left <= 0.01:
            return realized
    return realized + n_left * ((ar[end][1] / px0 - 1) * 100 - cost)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=34.0)
    ap.add_argument("--gap", type=int, default=60)
    ap.add_argument("--cost", type=float, default=0.10)
    args = ap.parse_args()

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since = now_ms - int(args.hours * 3600_000)
    db = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    pool = {c["symbol"] for c in json.loads(WATCHLIST.read_text(encoding="utf-8")).get("candidates", [])}

    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for sym, ts, px in db.execute(
            "select symbol, ts_ms, price from ticker_snap where ts_ms>=? order by symbol, ts_ms", (since,)):
        series[sym].append((ts, px))

    events = []
    for sym, ar in series.items():
        if sym not in pool or len(ar) < 70:
            continue
        hits = [(ar[i][0], ar[i][1]) for i in range(60, len(ar))
                if (ar[i][1] / ar[i - 15][1] - 1) * 100 <= -3.5]
        for ts, _ in dedup(hits, args.gap):
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts), None)
            if j is not None and j < len(ar) - 10:
                events.append((sym, j))

    print("=" * 100)
    print("阶梯止盈档位网格扫描  窗口 %s 起 %.0f 小时 | %d 个独立急跌事件 | 单边成本 %.2f%%"
          % (stamp(since), args.hours, len(events), args.cost))
    print("=" * 100)

    years = {(2.5, 6.0), (3.0, 7.0), (3.5, 8.0), (4.0, 9.0), (5.0, 12.0), (6.0, 15.0)}
    print()
    print("%-16s %10s %10s %10s %10s" % ("档位(+T1/+T2)", "+1h 均值%", "+3h 均值%", "+6h 均值%", "+24h 均值%"))
    print("-" * 100)
    best = []
    for t1, t2 in sorted(years):
        row = []
        for mx in (60, 180, 360, 1440):
            rets = [simulate(series[s], i, t1, t2, mx, args.cost) for s, i in events]
            row.append(sum(rets) / len(rets))
        best.append((max(row), (t1, t2), row))
        print("%-16s %+10.3f %+10.3f %+10.3f %+10.3f" % ("+%.1f/+%.1f" % (t1, t2), *row))

    print()
    print("中位数口径(每笔交易是否赚钱, 实体感):")
    print("%-16s %10s %10s %10s %10s" % ("档位(+T1/+T2)", "+1h 中位%", "+3h 中位%", "+6h 中位%", "+24h 中位%"))
    print("-" * 100)
    for t1, t2 in sorted(years):
        row = []
        for mx in (60, 180, 360, 1440):
            rets = [simulate(series[s], i, t1, t2, mx, args.cost) for s, i in events]
            row.append(statistics.median(rets))
        print("%-16s %+10.3f %+10.3f %+10.3f %+10.3f" % ("+%.1f/+%.1f" % (t1, t2), *row))

    print()
    print("胜率口径:")
    print("%-16s %10s %10s %10s %10s" % ("档位(+T1/+T2)", "+1h 胜率%", "+3h 胜率%", "+6h 胜率%", "+24h 胜率%"))
    print("-" * 100)
    for t1, t2 in sorted(years):
        row = []
        for mx in (60, 180, 360, 1440):
            rets = [simulate(series[s], i, t1, t2, mx, args.cost) for s, i in events]
            row.append(sum(1 for r in rets if r > 0) / len(rets) * 100)
        print("%-16s %10.1f %10.1f %10.1f %10.1f" % ("+%.1f/+%.1f" % (t1, t2), *row))

    print()
    print("均值最优组合: %s -> %s" % (max(best, key=lambda x: x[0])[1],
                                  ["%+.3f" % v for v in max(best, key=lambda x: x[0])[2]]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
