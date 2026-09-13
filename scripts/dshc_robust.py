#!/usr/bin/env python3
"""稳健性检验: 某个「看好的桶」到底靠不靠谱.

动机(2026-09-13): 早前的动量检验显示「24h 涨幅 > 40%」桶的未来 60 分钟收益高达 +4.506%
(胜率 67.1%), 看起来是极好的入场条件。但必须排除三种假象:
    ① 样本集中在少数几个合约(个别神币拉高均值)
    ② 样本集中在少数时间点(某一波行情, 不代表常态)
    ③ 少数极端值主导均值(中位数远低于均值)

因此本脚本对任意「条件」输出: 有效样本数 / 覆盖合约数 / 覆盖时间片数 /
均值·中位·胜率 / 去掉最大 5% 后的均值 / 前 5 大贡献样本的占比 / 逐合约与逐时间稳定性。

用法:
    python3 scripts/dshc_robust.py --cond chg24 --lo 40 --hi 1000 --horizon 60
    python3 scripts/dshc_robust.py --cond mom15 --lo 5 --hi 1000 --horizon 60
"""

from __future__ import annotations

import argparse
import sqlite3
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "m3dsc_market.db"


def load(conn) -> dict[str, list[tuple[int, float, float]]]:
    out: dict[str, list[tuple[int, float, float]]] = {}
    for r in conn.execute("SELECT symbol, ts_ms, price, price_change_pct FROM ticker_snap "
                          "WHERE price IS NOT NULL ORDER BY symbol, ts_ms"):
        out.setdefault(r[0], []).append((int(r[1]), float(r[2]), float(r[3] or 0.0)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cond", choices=["chg24", "mom15", "mom60"], default="chg24")
    # 注意: argparse 会把 "-5" 当成选项, 因此最小值用 --lo-mag(取正幅度) 传入
    ap.add_argument("--lo", type=float, default=40.0)
    ap.add_argument("--lo-mag", type=float, default=None,
                    help="下跌类条件用: 传入正幅度, 内部转为 -值, 例如 --lo-mag 5 表示 <= -5%%")
    ap.add_argument("--hi", type=float, default=1e9)
    ap.add_argument("--hi-mag", type=float, default=None)
    ap.add_argument("--horizon", type=int, default=60)
    args = ap.parse_args()
    if args.lo_mag is not None:
        args.lo = -abs(args.lo_mag)
    if args.hi_mag is not None:
        args.hi = -abs(args.hi_mag)

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    data = load(conn)
    if not data:
        sys.exit("ticker_snap 为空")
    step = 60_000
    n_fwd = max(1, int(args.horizon * 60_000 / step))

    obs: list[tuple[int, str, float]] = []     # (ts, symbol, forward_return%)
    for sym, s in data.items():
        if len(s) < 30:
            continue
        for i in range(15, len(s) - n_fwd - 1):
            ts, px, chg24 = s[i]
            if args.cond == "chg24":
                cond = chg24
            elif args.cond == "mom15":
                base = s[i - 3][1]
                cond = (px / base - 1) * 100 if base else 0.0
            else:
                base = s[i - 12][1]
                cond = (px / base - 1) * 100 if base else 0.0
            if not (args.lo <= cond < args.hi):
                continue
            fwd = (s[i + n_fwd][1] / px - 1) * 100
            obs.append((ts, sym, fwd))

    n = len(obs)
    print("=" * 92)
    print("稳健性检验: 条件=%s ∈ [%.1f, %.1f)  未来 %d 分钟收益" % (
        args.cond, args.lo, args.hi if args.hi < 1e8 else 9999, args.horizon))
    print("=" * 92)
    if n < 20:
        print("有效样本仅 %d 个 —— 样本过少, 任何结论都不可信。" % n)
        return 0

    rets = [o[2] for o in obs]
    rets_sorted = sorted(rets, reverse=True)
    trim = rets_sorted[int(n * 0.05):]           # 去掉最大的 5%
    syms = {o[1] for o in obs}
    slots = {o[0] for o in obs}
    t0, t1 = min(o[0] for o in obs), max(o[0] for o in obs)

    print("  有效样本        : %d" % n)
    print("  覆盖合约数      : %d" % len(syms))
    print("  覆盖时间片数    : %d (约 %.1f 小时)" % (len(slots), (t1 - t0) / 3_600_000))
    print("  时间跨度        : %s -> %s (UTC)" % (
        time.strftime("%m-%d %H:%M", time.gmtime(t0 / 1000)),
        time.strftime("%m-%d %H:%M", time.gmtime(t1 / 1000))))
    print("  ---- 收益分布 ----")
    print("  均值 %+.3f%%   中位 %+.3f%%   胜率 %.1f%%" % (
        statistics.fmean(rets), statistics.median(rets),
        sum(1 for x in rets if x > 0) / n * 100))
    print("  去掉最大 5%% 后的均值 %+.3f%%  (均值/去极值 = %.2fx)" % (
        statistics.fmean(trim), statistics.fmean(rets) / statistics.fmean(trim)
        if statistics.fmean(trim) else float("inf")))
    print("  最大 %+.2f%%  最小 %+.2f%%  标准差 %.2f%%" % (
        max(rets), min(rets), statistics.pstdev(rets)))

    # 集中度: 前 5 大样本贡献了多少总收益
    top5 = rets_sorted[:5]
    tot = sum(rets)
    print("  前 5 大样本合计贡献 %+.1f%%, 占全部 %+.1f%% 收益的 %.0f%%" % (
        sum(top5), tot, (sum(top5) / tot * 100) if tot else 0))

    # 逐合约
    per_sym: dict[str, list[float]] = defaultdict(list)
    for _, s, r in obs:
        per_sym[s].append(r)
    print("  ---- 逐合约(样本>=10 的前 8 个) ----")
    print("    %-16s %6s %9s %8s" % ("合约", "样本", "均值%", "胜率%"))
    for sym, v in sorted(per_sym.items(), key=lambda kv: -len(kv[1]))[:8]:
        if len(v) < 10:
            continue
        print("    %-16s %6d %+9.3f %8.1f" % (
            sym, len(v), statistics.fmean(v), sum(1 for x in v if x > 0) / len(v) * 100))
    print("  单合约最大样本占比: %.0f%%" % (max(len(v) for v in per_sym.values()) / n * 100))
    # 去掉贡献最大的合约后是否仍为正 —— 检验是否被个别"神币"拉高
    if len(per_sym) >= 3:
        by_share = sorted(per_sym.items(), key=lambda kv: -len(kv[1]))
        rest = [r for sym, v in by_share[1:] for r in v]
        if len(rest) >= 20:
            print("  去掉样本最多的合约(%s)后: n=%d 均值 %+.3f%% 胜率 %.1f%%" % (
                by_share[0][0], len(rest), statistics.fmean(rest),
                sum(1 for x in rest if x > 0) / len(rest) * 100))
        if len(per_sym) >= 4:
            rest2 = [r for sym, v in by_share[2:] for r in v]
            if len(rest2) >= 20:
                print("  再去掉第二多(%s)后      : n=%d 均值 %+.3f%% 胜率 %.1f%%" % (
                    by_share[1][0], len(rest2), statistics.fmean(rest2),
                    sum(1 for x in rest2 if x > 0) / len(rest2) * 100))

    # 分时段(切成 4 段看是否稳定)
    slots_sorted = sorted(slots)
    if len(slots_sorted) >= 8:
        q = len(slots_sorted) // 4
        print("  ---- 分时段稳定性(4 等分) ----")
        for k in range(4):
            lo_ts = slots_sorted[k * q]
            hi_ts = slots_sorted[min((k + 1) * q - 1, len(slots_sorted) - 1)]
            seg = [r for ts, _, r in obs if lo_ts <= ts <= hi_ts]
            if len(seg) < 5:
                continue
            print("    %s-%s  n=%4d  均值 %+.3f%%  胜率 %.1f%%" % (
                time.strftime("%m-%d %H:%M", time.gmtime(lo_ts / 1000)),
                time.strftime("%H:%M", time.gmtime(hi_ts / 1000)),
                len(seg), statistics.fmean(seg),
                sum(1 for x in seg if x > 0) / len(seg) * 100))

    print("\n判读标准:")
    print("  ✔ 可做: 样本 >= 200 且 覆盖合约 >= 30 且 时间跨度 >= 24h,")
    print("          且「去极值均值」仍显著为正(>0.3%), 且各时段方向一致")
    print("  ✘ 不可做: 前 5 样本贡献 > 50%, 或单合约占比 > 30%, 或去极值后均值 <= 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
