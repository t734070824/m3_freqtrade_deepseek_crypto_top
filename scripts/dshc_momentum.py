#!/usr/bin/env python3
"""动量条件收益检验: 直接用分钟级行情回答「该追涨还是该等回踩」.

对每个 (symbol, t) 计算:
    条件变量: 过去 15 分钟的收益率(短动量) 与 24h 涨幅(涨幅榜位置)
    目标变量: 之后 15 / 60 分钟的收益率
按条件分桶统计, 直接给出「在什么位置买、期望收益是多少」。

这是本策略最核心的实证问题: 如果「过去 15 分钟大涨」的下一段期望收益为负,
则任何追涨式入场都是负 alpha, 必须改为「等回踩」。
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "m3dsc_market.db"


def load(conn: sqlite3.Connection) -> dict[str, list[tuple[int, float, float]]]:
    """symbol -> [(ts_ms, price, change_24h)]"""
    out: dict[str, list[tuple[int, float, float]]] = {}
    for r in conn.execute(
            "SELECT symbol, ts_ms, price, price_change_pct FROM ticker_snap "
            "WHERE price IS NOT NULL ORDER BY symbol, ts_ms"):
        out.setdefault(r[0], []).append((int(r[1]), float(r[2]), float(r[3] or 0.0)))
    return out


def fwd(series: list[tuple[int, float, float]], i: int, minutes: int, step_ms: int) -> float | None:
    n = max(1, int(minutes * 60_000 / step_ms))
    j = i + n
    if j >= len(series):
        return None
    return (series[j][1] / series[i][1] - 1.0) * 100.0


def past(series: list[tuple[int, float, float]], i: int, minutes: int, step_ms: int) -> float | None:
    n = max(1, int(minutes * 60_000 / step_ms))
    j = i - n
    if j < 0:
        return None
    return (series[i][1] / series[j][1] - 1.0) * 100.0


def bucket_stats(pairs: list[tuple[float, float]], edges: list[float],
                 labels: list[str]) -> None:
    print("  %-22s %7s %12s %10s %8s" % ("条件区间", "样本", "平均未来收益", "中位", "胜率%"))
    for k in range(len(edges) - 1):
        lo, hi = edges[k], edges[k + 1]
        sel = [y for x, y in pairs if lo <= x < hi]
        if len(sel) < 15:
            continue
        print("  %-22s %7d %11.3f%% %9.3f%% %7.1f" % (
            labels[k], len(sel), statistics.fmean(sel), statistics.median(sel),
            sum(1 for v in sel if v > 0) / len(sel) * 100))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mom-min", type=int, default=15, help="短动量窗口(分钟)")
    args = ap.parse_args()
    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    data = load(conn)
    if not data:
        sys.exit("ticker_snap 为空")
    step = 60_000
    print("=" * 84)
    print("动量条件收益检验 (短动量窗口 %d 分钟; 时间戳均为 UTC)" % args.mom_min)
    print("=" * 84)

    for horizon in (15, 60):
        pairs_line = []
        for sym, s in data.items():
            if len(s) < args.mom_min + horizon + 5:
                continue
            for i in range(args.mom_min, len(s) - int(horizon * 60_000 / step) - 1):
                m = past(s, i, args.mom_min, step)
                f = fwd(s, i, horizon, step)
                if m is None or f is None:
                    continue
                pairs_line.append((m, f))
        if len(pairs_line) < 50:
            print("horizon=%dm 样本不足(%d)" % (horizon, len(pairs_line))); continue
        print("\n### 未来 %d 分钟收益, 按「过去 %d 分钟动量」分桶 (n=%d)"
              % (horizon, args.mom_min, len(pairs_line)))
        bucket_stats(pairs_line,
                     [-100, -5, -2, -0.5, 0.5, 2, 5, 100],
                     ["跌超5%", "-5~-2%", "-2~-0.5%", "横盘±0.5%",
                      "0.5~2%", "2~5%", "涨超5%"])
        xs = [x for x, _ in pairs_line]; ys = [y for _, y in pairs_line]
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        sxy = sum((a - mx) * (b - my) for a, b in pairs_line)
        sxx = sum((a - mx) ** 2 for a in xs); syy = sum((b - my) ** 2 for b in ys)
        corr = sxy / (sxx * syy) ** 0.5 if sxx > 0 and syy > 0 else float("nan")
        print("  相关系数(短动量 vs 未来收益) = %+.4f" % corr)

    # 24h 涨幅(涨幅榜位置) 作为条件
    print("\n### 未来 60 分钟收益, 按「24h 涨幅」分桶 (涨幅榜位置)")
    pairs_line = []
    for sym, s in data.items():
        for i in range(0, len(s) - 61):
            f = fwd(s, i, 60, step)
            if f is None:
                continue
            pairs_line.append((s[i][2], f))
    if len(pairs_line) >= 50:
        bucket_stats(pairs_line, [-100, -10, -3, 0, 3, 10, 20, 40, 1000],
                     ["跌超10%", "-10~-3%", "-3~0%", "0~3%",
                      "3~10%", "10~20%", "20~40%", "涨超40%"])
    print()
    print("解读: 若「涨超5%」桶的未来收益显著低于「横盘/下跌」桶, 说明追涨是负 alpha,")
    print("      策略应改为「回踩后买入」; 若相反, 则动量延续成立, 应保留突破入场。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
