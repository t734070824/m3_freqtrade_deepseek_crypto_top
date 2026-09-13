#!/usr/bin/env python3
"""「急跌买入」方向的三重稳健性检验: 事件口径 + 剔除离群标的 + 自助重采样.

为什么要做这个:
    之前的结论都是按「采样点」统计的 —— 同一场急跌在逐分钟快照里会连续贡献
    十几个样本, 它们高度相关, 会把样本量虚高、也会让某一只暴涨标的的多次事件
    主导整组统计量(实测: LSK 一只标的就贡献了 B+F 组均值的一大半)。

    本脚本用三个互补的角度给结论上强度:
      1. **事件口径**: 同标的相邻命中合并为一次独立急跌事件;
      2. **剔除离群标的**: 逐标的留一法, 看结论是否依赖单一标的;
      3. **自助重采样**: 以「标的」为重采样单位(而非采样点), 给出期望值的置信区间。

    只有当三关都过, 结论才算站得住。
时间口径: 所有输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import argparse
import json
import random
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


def trimmed(vals: list[float], pct: float = 0.10) -> float:
    if not vals:
        return 0.0
    v = sorted(vals)
    k = int(len(v) * pct)
    core = v[k:len(v) - k] if len(v) - 2 * k >= 3 else v
    return sum(core) / len(core)


def dedup(hits: list[tuple[int, float]], gap_min: int) -> list[tuple[int, float]]:
    out = []
    gap_ms = gap_min * 60_000
    for ts, px in hits:
        if not out or ts - out[-1][0] > gap_ms:
            out.append((ts, px))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=34.0)
    ap.add_argument("--gap", type=int, default=60)
    ap.add_argument("--horizon", type=int, default=60)
    ap.add_argument("--boot", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    now_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
    since_ms = now_ms - int(args.hours * 3600_000)
    db = sqlite3.connect(f"file:{MARKET_DB}?mode=ro", uri=True)
    wl = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    pool = {c["symbol"]: c for c in wl.get("candidates", []) if c.get("symbol")}

    series: dict[str, list[tuple[int, float]]] = defaultdict(list)
    for sym, ts, px in db.execute(
            "select symbol, ts_ms, price from ticker_snap where ts_ms>=? order by symbol, ts_ms",
            (since_ms,)):
        series[sym].append((ts, px))

    print("=" * 100)
    print("「急跌买入」方向的三重稳健性检验")
    print("  窗口: %s 起, %.1f 小时 | 粒度: 1 分钟快照 | 动量窗口 15m=i-15 / 1h=i-60" % (stamp(since_ms), args.hours))
    print("  事件去重: 同标的相邻命中 <= %d 分钟合并 | 前瞻 %d 分钟" % (args.gap, args.horizon))
    print("=" * 100)

    # ---- 收集事件 ----
    grp: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    for sym, ar in series.items():
        if sym not in pool or len(ar) < 70:
            continue
        ann = float(pool[sym].get("funding_ann", 0.0) or 0.0)
        h15, h1h = [], []
        for i in range(60, len(ar)):
            ts, px = ar[i]
            if (px / ar[i - 15][1] - 1) * 100 <= -3.5:
                h15.append((ts, px))
            if (px / ar[i - 60][1] - 1) * 100 <= -5.0:
                h1h.append((ts, px))
        for ts, px in dedup(h15, args.gap):
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts + args.horizon * 60_000), None)
            if j is None:
                continue
            r = (ar[j][1] / px - 1) * 100
            grp["B 急跌(15m<=-3.5%)"].append((sym, ts, r))
            if ann <= -0.15:
                grp["B+F 急跌+负费率"].append((sym, ts, r))
        for ts, px in dedup(h1h, args.gap):
            if ann > -0.15:
                continue
            j = next((k for k, (t2, _) in enumerate(ar) if t2 >= ts + args.horizon * 60_000), None)
            if j is None:
                continue
            grp["F 负费率+1h<=-5%"].append((sym, ts, (ar[j][1] / px - 1) * 100))

    rng = random.Random(args.seed)
    for name in ("B 急跌(15m<=-3.5%)", "B+F 急跌+负费率", "F 负费率+1h<=-5%"):
        rows = grp.get(name, [])
        if len(rows) < 10:
            print("\n%s: 事件数 %d, 样本不足" % (name, len(rows)))
            continue
        rets = [r for _, _, r in rows]
        print("\n" + "-" * 100)
        print("【%s】事件数 %d, 覆盖标的 %d 只" % (name, len(rets), len(set(s for s, _, _ in rows))))
        print("-" * 100)
        print("  全样本: 均值 %+7.3f%% | 中位 %+7.3f%% | 去极值 %+7.3f%% | 胜率 %.1f%%"
              % (sum(rets) / len(rets), statistics.median(rets), trimmed(rets),
                 sum(1 for r in rets if r > 0) / len(rets) * 100))

        # 留一法(按标的)
        per: dict[str, list[float]] = defaultdict(list)
        for s, _, r in rows:
            per[s].append(r)
        print("  逐标的留一(按事件数降序, 只看去掉该标的后整组的变化):")
        impact = []
        for s, rs in per.items():
            rest = [r for s2, _, r in rows if s2 != s]
            impact.append((sum(rs) / len(rs), len(rs), s, trimmed(rest), len(rest)))
        impact.sort(key=lambda x: -abs(x[0]) * x[1])
        for mean_s, n_s, s, trim_rest, n_rest in impact[:6]:
            print("     去掉 %-14s(%2d 次, 该标的均值 %+7.3f%%) -> 整组去极值 %+7.3f%% (剩 %d 事件)"
                  % (s, n_s, mean_s, trim_rest, n_rest))

        rest_all = [r for s, _, r in rows if s != impact[0][2]]
        print("  剔掉贡献最大的标的(%s)后: 去极值 %+7.3f%% | 胜率 %.1f%% | 事件 %d"
              % (impact[0][2], trimmed(rest_all),
                 sum(1 for r in rest_all if r > 0) / len(rest_all) * 100, len(rest_all)))

        # 自助重采样: 以「标的」为重采样单位
        syms = list(per.keys())
        boots = []
        for _ in range(args.boot):
            pick = [rng.choice(syms) for _ in syms]
            vals = [r for s in pick for r in per[s]]
            boots.append(sum(vals) / len(vals))
        boots.sort()
        lo = boots[int(0.025 * len(boots))]
        hi = boots[int(0.975 * len(boots))]
        pos = sum(1 for b in boots if b > 0) / len(boots) * 100
        print("  自助重采样(以标的为重采样单位, %d 次): 期望的 95%% 置信区间 [%+.3f%%, %+.3f%%], "
              "为正的概率 %.1f%%" % (args.boot, lo, hi, pos))

    print()
    print("=" * 100)
    print("判定规则: 全样本去极值为正 且 剔除最大贡献标的后仍为正 且 自助置信区间下界 > 0, 才算方向成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
