#!/usr/bin/env python3
"""B+F 融合前提检验: 负费率这一层到底有没有增量价值?

问题: 之前所有检验都是**按采样点**统计的。同一场急跌在逐分钟快照里会连续贡献
十几个样本, 它们是高度相关的, 会把样本量虚高、也会把统计量带偏。
真正决定 G 是否值得跑的, 是**把相邻样本合并成一次独立"急跌事件"后**,
「急跌」与「负费率+急跌」的期望差是否依然存在。

做法:
  1. 从采集库取最近 N 小时逐分钟 ticker 快照 (1 分钟粒度);
  2. 对候选池内每个标的, 逐分钟计算 15 分钟动量 (i-15) 与 1 小时动量 (i-60);
  3. 命中阈值即记为一个采样点;
  4. **事件去重**: 同一标的相邻命中点间隔 <= GAP 分钟视为同一场急跌, 只保留首点;
  5. 对事件首点计算前瞻 60 分钟收益, 分组对比:
       A 组 = 急跌(15m<=-3.5%)
       B 组 = 急跌 + 负费率(年化 <= -15%)
       C 组 = 负费率 + 1h<=-5%
  6. 输出 均值 / 中位数 / 去极值均值(截尾 10%) / 胜率 / 独立事件数。

时间口径: 所有输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
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


def trimmed(vals: list[float], pct: float = 0.10) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    k = int(len(v) * pct)
    core = v[k:len(v) - k] if len(v) - 2 * k >= 3 else v
    return sum(core) / len(core)


def stats(name: str, rets: list[float]) -> dict:
    if not rets:
        return {"name": name, "n": 0}
    return {
        "name": name, "n": len(rets),
        "mean": sum(rets) / len(rets),
        "median": statistics.median(rets),
        "trim": trimmed(rets),
        "win": sum(1 for r in rets if r > 0) / len(rets) * 100.0,
    }


def load_series(db: sqlite3.Connection, since_ms: int) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for sym, ts, px in db.execute(
            "select symbol, ts_ms, price from ticker_snap where ts_ms>=? order by symbol, ts_ms",
            (since_ms,)):
        out[sym].append((ts, px))
    return out


def dedup_events(hits: list[tuple[int, float]], gap_min: int) -> list[tuple[int, float]]:
    """同一标的相邻命中点间隔 <= gap_min 视为同一场急跌, 只保留首点."""
    out: list[tuple[int, float]] = []
    gap_ms = gap_min * 60_000
    for ts, px in hits:
        if not out or ts - out[-1][0] > gap_ms:
            out.append((ts, px))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=34.0)
    ap.add_argument("--gap", type=int, default=60, help="事件合并间隔(分钟)")
    ap.add_argument("--horizon", type=int, default=60, help="前瞻分钟数")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since_ms = now_ms - int(args.hours * 3600_000)
    db = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)

    try:
        wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
        pool = {c["symbol"]: c for c in wl.get("candidates", []) if c.get("symbol")}
    except Exception as exc:  # noqa: BLE001
        print("无法读取候选池: %s" % exc, file=sys.stderr)
        return 2

    series = load_series(db, since_ms)
    span = [t for ar in series.values() for t, _ in ar]
    print("=" * 100)
    print("B+F 融合前提检验 —— 按「急跌事件」去重后的期望对比")
    print("  窗口: %s 起, 共 %.1f 小时" % (stamp(since_ms), args.hours))
    print("  粒度: 采集库 1 分钟逐标的快照; 动量窗口 15m=i-15 / 1h=i-60 (1 分钟粒度)")
    print("  事件去重: 同标的相邻命中间隔 <= %d 分钟合并为同一场急跌" % args.gap)
    print("  前瞻: %d 分钟; 去极值 = 截尾 10%%" % args.horizon)
    print("=" * 100)

    groups: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    raw_counts: dict[str, int] = defaultdict(int)

    for sym, ar in series.items():
        c = pool.get(sym)
        if not c or len(ar) < 70:
            continue
        ann = float(c.get("funding_ann", 0.0) or 0.0)
        h15: list[tuple[int, float]] = []
        h1h: list[tuple[int, float]] = []
        for i in range(60, len(ar)):
            ts, px = ar[i]
            p15 = ar[i - 15][1]
            p60 = ar[i - 60][1]
            if not p15 or not p60:
                continue
            if (px / p15 - 1) * 100 <= -3.5:
                h15.append((ts, px))
            if (px / p60 - 1) * 100 <= -5.0:
                h1h.append((ts, px))
        ev15 = dedup_events(h15, args.gap)
        ev1h = dedup_events(h1h, args.gap)
        raw_counts["B 急跌(15m<=-3.5%)"] += len(h15)
        raw_counts["B+F 急跌+负费率"] += sum(1 for ts, _ in h15 if ann <= -0.15)
        raw_counts["F 1h<=-5%+负费率"] += sum(1 for ts, _ in h1h if ann <= -0.15)

        # 前瞻收益
        for ts, px in ev15:
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts + args.horizon * 60_000), None)
            if j is None:
                continue
            r = (ar[j][1] / px - 1) * 100
            groups["B 急跌(15m<=-3.5%)"].append((sym, ts, r))
            if ann <= -0.15:
                groups["B+F 急跌+负费率"].append((sym, ts, r))
        for ts, px in ev1h:
            if ann > -0.15:
                continue
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts + args.horizon * 60_000), None)
            if j is None:
                continue
            groups["F 1h<=-5%+负费率"].append((sym, ts, (ar[j][1] / px - 1) * 100))

    print()
    print("%-24s %7s %9s %9s %9s %8s" % ("分组(事件去重后)", "事件数", "均值%", "中位数%", "去极值%", "胜率%"))
    print("-" * 100)
    summary = {}
    for name in ("B 急跌(15m<=-3.5%)", "B+F 急跌+负费率", "F 1h<=-5%+负费率"):
        rows = groups.get(name, [])
        s = stats(name, [r for _, _, r in rows])
        summary[name] = s
        if s["n"]:
            print("%-24s %7d %+9.3f %+9.3f %+9.3f %8.1f" % (
                name, s["n"], s["mean"], s["median"], s["trim"], s["win"]))
        else:
            print("%-24s %7d %9s" % (name, 0, "样本不足"))

    print()
    print("采样点口径(未去重, 仅供对照 —— 之前的结论就是按这个口径得出的):")
    for k, v in raw_counts.items():
        print("   %-24s %6d 个采样点" % (k, v))

    # 每标的贡献
    print()
    print("B+F 组的事件按标的分布(去重后):")
    rows = groups.get("B+F 急跌+负费率", [])
    per: dict[str, list[float]] = defaultdict(list)
    for sym, _, r in rows:
        per[sym].append(r)
    for sym, rs in sorted(per.items(), key=lambda x: -len(x[1])):
        print("   %-14s %2d 次  均值 %+7.3f%%  中位 %+7.3f%%" % (
            sym, len(rs), sum(rs) / len(rs), statistics.median(rs)))

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    for gname in ("B 急跌(15m<=-3.5%)", "F 1h<=-5%+负费率"):
        print()
        print("%s 的事件按标的分布(去重后):" % gname)
        per2: dict[str, list[float]] = defaultdict(list)
        for sym, _, r in groups.get(gname, []):
            per2[sym].append(r)
        for sym, rs in sorted(per2.items(), key=lambda x: -len(x[1]))[:10]:
            print("   %-14s %2d 次  均值 %+7.3f%%  中位 %+7.3f%%" % (
                sym, len(rs), sum(rs) / len(rs), statistics.median(rs)))

    print()
    print("解读口径: 若「B+F 急跌+负费率」的去极值均值与胜率并不优于「B 急跌」, 则负费率这一层没有增量,")
    print("          G 相对 B 就只剩「更短的持有」, 需要重新考虑融合的前提。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
