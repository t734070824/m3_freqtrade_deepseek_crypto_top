#!/usr/bin/env python3
"""M3-DSH 数据研究与策略校准工具 (纯标准库实现, 无 pandas/numpy 依赖).

在 dry-run 期间用采集到的真实数据回答「打分是否有效」这类问题, 而不是靠直觉调参。

子命令:
    summary   数据概览(各表行数与时间区间)
    alpha     打分分桶 vs 未来收益 (核心: 验证 score 是否有预测力)
    funding   资金费率分桶 vs 未来收益 (验证拥挤度因子方向性)
    stability 榜单名次稳定性 (涨幅榜标的能霸榜多久)
    bench     涨幅榜篮子 vs 全体市场 (验证「只做涨幅榜」前提)

时间: 数据库内为 UTC 毫秒; 输出同时标注 北京时间(UTC+8) 与 UTC。

用法:
    python3 scripts/dshc_analyze.py alpha --horizons 15 60 240
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
import time
from pathlib import Path
from typing import Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "data" / "m3dsc_market.db"

DEFAULT_HORIZONS = (15, 60, 240, 720)   # 分钟


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_both(ms: int | float) -> str:
    ms = int(ms)
    utc = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))
    cst = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ms / 1000 + 8 * 3600))
    return f"{cst} 北京时间(UTC+8) / {utc} UTC"


def load(db: Path) -> sqlite3.Connection:
    if not db.exists():
        sys.exit(f"数据库不存在: {db}")
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=20)
    c.row_factory = sqlite3.Row
    return c


# ---------------------------------------------------------------- 统计工具
def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 3:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return float("nan")
    return sxy / math.sqrt(sxx * syy)


def quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos)); hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def bucket_stats(pairs: Sequence[tuple[float, float]], n_buckets: int = 5,
                 label: str = "区间") -> list[tuple[str, int, float, float, float]]:
    """把 (因子, 未来收益) 按因子分位切成 n 桶, 返回每桶统计."""
    vals = sorted(p[0] for p in pairs)
    edges = [quantile(vals, i / n_buckets) for i in range(n_buckets + 1)]
    out: list[tuple[str, int, float, float, float]] = []
    for i in range(n_buckets):
        lo, hi = edges[i], edges[i + 1]
        sel = [y for x, y in pairs
               if (x >= lo and (x < hi or (i == n_buckets - 1 and x <= hi)))]
        if not sel:
            continue
        out.append((f"[{lo:+.2f}, {hi:+.2f}]", len(sel), statistics.fmean(sel),
                    statistics.median(sel),
                    sum(1 for v in sel if v > 0) / len(sel) * 100.0))
    return out


def print_buckets(title: str, rows: Iterable[tuple[str, int, float, float, float]]) -> None:
    print(title)
    print("  %-22s %8s %12s %10s %8s" % ("分桶", "样本", "平均未来收益", "中位", "胜率%"))
    for label, n, mean, med, win in rows:
        print("  %-22s %8d %11.3f%% %9.3f%% %7.1f" % (label, n, mean, med, win))
    print()


# ---------------------------------------------------------------- 数据装配
def load_ticker_series(conn: sqlite3.Connection) -> tuple[dict[str, list[tuple[int, float]]],
                                                          int]:
    """返回 {symbol: [(ts_ms, price)...]} (按时间升序) 与采样步长(ms)."""
    series: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute("SELECT symbol, ts_ms, price FROM ticker_snap "
                          "WHERE price IS NOT NULL ORDER BY symbol, ts_ms"):
        series.setdefault(r["symbol"], []).append((int(r["ts_ms"]), float(r["price"])))
    step = 60_000
    for v in series.values():
        if len(v) > 2:
            diffs = sorted(b - a for (a, _), (b, _) in zip(v, v[1:]) if b > a)
            if diffs:
                step = diffs[len(diffs) // 2]
            break
    return series, step


def fwd_return(series: dict[str, list[tuple[int, float]]], symbol: str, ts: int,
               horizon_min: int, step_ms: int) -> float | None:
    """给定时刻 ts 之后 horizon_min 分钟的价格收益率(%)."""
    v = series.get(symbol)
    if not v:
        return None
    target = ts + horizon_min * 60_000
    # 二分找 target 之后最近的采样点
    lo, hi = 0, len(v) - 1
    if v[hi][0] < target:
        return None      # 未来数据还没采到
    while lo < hi:
        mid = (lo + hi) // 2
        if v[mid][0] < target:
            lo = mid + 1
        else:
            hi = mid
    base = None
    for t, p in reversed(v):
        if t <= ts:
            base = p
            break
    if not base:
        return None
    return (v[lo][1] / base - 1.0) * 100.0


# ---------------------------------------------------------------- 子命令
def cmd_summary(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    print("=== M3-DSH 数据概览 ===")
    print("  当前时间:", fmt_both(now_ms()))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        rng = ""
        for col in ("ts_ms", "updated_ms", "fetched_ms"):
            try:
                lo, hi = conn.execute(f"SELECT MIN({col}), MAX({col}) FROM {t}").fetchone()
                if lo:
                    rng = f"  {fmt_both(lo)}  ->  {fmt_both(hi)}"
                    break
            except sqlite3.OperationalError:
                continue
        print(f"  {t:<18} {n:>9} 行{rng}")


def cmd_alpha(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    snap = conn.execute(
        "SELECT ts_ms, symbol, score, funding_ann, oi_chg_1h, rank FROM rank_snap "
        "ORDER BY ts_ms").fetchall()
    if not snap:
        sys.exit("rank_snap 为空: 采集器每 5 分钟写一条, 请先运行一段时间")
    series, step = load_ticker_series(conn)
    print("样本区间: %s  ->  %s" % (fmt_both(snap[0]["ts_ms"]), fmt_both(snap[-1]["ts_ms"])))
    print("快照条数: %d   合约数: %d   价格采样步长: %d 分钟\n" % (
        len(snap), len({r["symbol"] for r in snap}), step // 60_000))
    for h in args.horizons:
        pairs, fund_pairs, oi_pairs = [], [], []
        for r in snap:
            f = fwd_return(series, r["symbol"], int(r["ts_ms"]), h, step)
            if f is None:
                continue
            pairs.append((float(r["score"] or 0), f))
            fund_pairs.append((float(r["funding_ann"] or 0) * 100, f))
            oi_pairs.append((float(r["oi_chg_1h"] or 0), f))
        if len(pairs) < 30:
            print("--- horizon=%dm 样本不足(%d), 跳过 ---\n" % (h, len(pairs)))
            continue
        print("=========== horizon = %d 分钟  (n=%d) ===========" % (h, len(pairs)))
        print_buckets("  [按 M3 打分分桶]", bucket_stats(pairs))
        print("  皮尔逊相关(score vs 未来收益) = %+.4f" % pearson(
            [p[0] for p in pairs], [p[1] for p in pairs]))
        print("  多头口径: score>0 的样本平均收益 %+.3f%%(n=%d) / score<0 的样本 %+.3f%%(n=%d)" % (
            statistics.fmean([y for x, y in pairs if x > 0]) if any(x > 0 for x, _ in pairs) else 0.0,
            sum(1 for x, _ in pairs if x > 0),
            statistics.fmean([-y for x, y in pairs if x < 0]) if any(x < 0 for x, _ in pairs) else 0.0,
            sum(1 for x, _ in pairs if x < 0)))
        print("         (score>0 看多: 真实收益; score<0 看空: 取负号后为做空收益)")
        print("  皮尔逊相关(年化费率 vs 未来收益) = %+.4f" % pearson(
            [p[0] for p in fund_pairs], [p[1] for p in fund_pairs]))
        print("  皮尔逊相关(OI 1h 变化 vs 未来收益) = %+.4f\n" % pearson(
            [p[0] for p in oi_pairs], [p[1] for p in oi_pairs]))
        print_buckets("  [按资金费率年化(%) 分桶]", bucket_stats(fund_pairs))


def cmd_funding(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    snap = conn.execute("SELECT ts_ms, symbol, funding_ann FROM rank_snap ORDER BY ts_ms").fetchall()
    if not snap:
        sys.exit("rank_snap 为空")
    series, step = load_ticker_series(conn)
    pairs = []
    for r in snap:
        f = fwd_return(series, r["symbol"], int(r["ts_ms"]), args.horizon, step)
        if f is not None:
            pairs.append((float(r["funding_ann"] or 0) * 100, f))
    if len(pairs) < 30:
        sys.exit("样本不足: %d" % len(pairs))
    print_buckets("资金费率年化(%%) vs 未来 %d 分钟收益 (n=%d)" % (args.horizon, len(pairs)),
                  bucket_stats(pairs))
    print("解读: 若高正费率桶的未来收益显著低于负费率桶, 则「高费率=拥挤=压制」成立,")
    print("      可继续提高打分引擎中 funding 因子的权重; 反之应降低。")


def cmd_stability(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    rows = conn.execute(
        "SELECT ts_ms, symbol, rank FROM rank_snap WHERE rank <= ? ORDER BY symbol, ts_ms",
        (args.top_n,)).fetchall()
    if not rows:
        sys.exit("rank_snap 中暂无进入 TOP%d 的记录" % args.top_n)
    per: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        per.setdefault(r["symbol"], []).append((int(r["ts_ms"]), int(r["rank"])))
    step_min = 5.0
    out = []
    for sym, v in per.items():
        ts = [t for t, _ in v]
        runs, cur = [], 1
        for a, b in zip(ts, ts[1:]):
            if b - a <= step_min * 60_000 * 1.5:
                cur += 1
            else:
                runs.append(cur); cur = 1
        runs.append(cur)
        out.append((sym, len(ts) * step_min, len(runs), max(runs) * step_min,
                    statistics.fmean([r for _, r in v])))
    out.sort(key=lambda x: x[1], reverse=True)
    print("进入过 TOP%d 的合约数: %d\n" % (args.top_n, len(out)))
    print("  %-16s %12s %10s %12s %10s" % ("合约", "累计在榜(分)", "上榜次数", "最长连续(分)", "平均名次"))
    for sym, tot, runs, longest, avg_rank in out[:args.limit]:
        print("  %-16s %12.0f %10d %12.0f %10.1f" % (sym, tot, runs, longest, avg_rank))
    print()
    print("解读: 「最长连续在榜」远大于「累计/上榜次数」的标的是可长期持有的趋势票;")
    print("      上榜次数多但每次都只有几分钟的属于脉冲型标的, 不适合趋势跟随。")


def cmd_bench(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    snap = conn.execute("SELECT ts_ms, symbol, rank FROM rank_snap ORDER BY ts_ms").fetchall()
    if not snap:
        sys.exit("rank_snap 为空")
    series, step = load_ticker_series(conn)
    all_syms = sorted(series)
    buckets = {"涨幅榜 TOP10": set(), "涨幅榜 TOP30": set()}
    per_bucket: dict[str, list[float]] = {k: [] for k in [*buckets, "全体市场"]}
    for r in snap:
        f = fwd_return(series, r["symbol"], int(r["ts_ms"]), args.horizon, step)
        if f is None:
            continue
        rk = int(r["rank"] or 9999)
        if rk <= 10:
            per_bucket["涨幅榜 TOP10"].append(f)
        if rk <= 30:
            per_bucket["涨幅榜 TOP30"].append(f)
        per_bucket["全体市场"].append(f)
    print("未来 %d 分钟平均价格收益率 (等权, 基于每分钟行情快照)  n=%d\n" % (
        args.horizon, len(per_bucket["全体市场"])))
    for k in ("涨幅榜 TOP10", "涨幅榜 TOP30", "全体市场"):
        v = per_bucket[k]
        if not v:
            print("  %-12s 无样本" % k); continue
        print("  %-12s 平均 %+.3f%%   中位 %+.3f%%   胜率 %.1f%%   (n=%d)" % (
            k, statistics.fmean(v), statistics.median(v),
            sum(1 for x in v if x > 0) / len(v) * 100, len(v)))
    print("\n解读: 若「涨幅榜 TOP10/30」的平均收益显著低于「全体市场」, 说明追涨幅榜是负 alpha,")
    print("      策略重心应从「追涨」转向「做空过热」或「等回踩」。")


def main() -> int:
    ap = argparse.ArgumentParser(description="M3-DSH 数据研究工具 (纯标准库)")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--horizons", type=int, nargs="*", default=list(DEFAULT_HORIZONS))
    ap.add_argument("--top_n", type=int, default=10)
    ap.add_argument("--limit", type=int, default=25)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("summary", "alpha", "funding", "stability", "bench"):
        # 每个子命令都接受这些参数, 便于 `cmd --horizon 15` 这种自然写法
        sp = sub.add_parser(name)
        sp.add_argument("--db", type=Path, default=DEFAULT_DB)
        sp.add_argument("--horizon", type=int, default=60)
        sp.add_argument("--horizons", type=int, nargs="*", default=list(DEFAULT_HORIZONS))
        sp.add_argument("--top_n", type=int, default=10)
        sp.add_argument("--limit", type=int, default=25)
    args = ap.parse_args()
    conn = load(args.db)
    {"summary": cmd_summary, "alpha": cmd_alpha, "funding": cmd_funding,
     "stability": cmd_stability, "bench": cmd_bench}[args.cmd](conn, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
