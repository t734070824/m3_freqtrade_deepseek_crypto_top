#!/usr/bin/env python3
"""系统性检验: 把本项目用过的所有「入场条件」放在同一套严格口径下评判.

⚠️ 历史教训(2026-09-13): 本脚本的上一版把 m15 = i-3 当成「15 分钟动量」,
   实际算的是 3 分钟动量, 导致同一条件在不同脚本间给出矛盾结论。修正后:
     m15 = close[i] / close[i-15]  -> 15 分钟(ticker_snap 为 1 分钟粒度)
     m60 = close[i] / close[i-60]  -> 1 小时
   与策略源码一致(M3DipRevert/M3CarryDip 使用 pandas pct_change(3) 于 5m K 线 = 15 分钟)。

判读标准(写死在输出里, 避免事后挑数据):
    ✔ 可继续投入: 样本 >= 150 且 覆盖合约 >= 20 且 跨度 >= 24h
                  且 去极值均值 > 0 且 胜率 > 52% 且 >=3/4 时段为正

注意: 本检验不含资金费率维度(历史费率覆盖尚不完整), 因此对 F/C 类
      「负费率 + 入场条件」的组合只能评估入场那一半。
"""
from __future__ import annotations

import statistics
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "m3dsc_market.db"

CONDS = [
    ("A 追涨: 24h>=20%", lambda c, a, b: c >= 20.0, "24h 涨幅 >= 20%"),
    ("A 追涨(严): 24h>=40%", lambda c, a, b: c >= 40.0, "24h 涨幅 >= 40%"),
    ("B 急跌: 15m<=-3.5%", lambda c, a, b: a <= -3.5, "15 分钟跌超 3.5%(B 真实门槛)"),
    ("B 急跌(严): 15m<=-5%", lambda c, a, b: a <= -5.0, "15 分钟跌超 5%"),
    ("F 急跌: 15m<=-2.5%", lambda c, a, b: a <= -2.5, "15 分钟跌超 2.5%(F 门槛之一)"),
    ("F 缓跌: 1h<=-5%", lambda c, a, b: b <= -5.0, "1 小时跌超 5%(F 另一门槛)"),
    ("C 趋势票: 24h 5~60%", lambda c, a, b: 5.0 <= c <= 60.0, "24h 涨幅 5~60%"),
    ("E 做空候选: 24h>25%", lambda c, a, b: c > 25.0, "24h 涨幅 > 25%(E 候选池)"),
    ("D 突破代理: 15m>=+2%", lambda c, a, b: a >= 2.0, "15 分钟涨超 2%"),
    ("对照: 15m 涨超 5%", lambda c, a, b: a >= 5.0, "15 分钟涨超 5%"),
    ("对照: 15m 跌超 10%", lambda c, a, b: a <= -10.0, "15 分钟跌超 10%(极端)"),
]


def main() -> int:
    horizon = 60
    conn = sqlite3.connect("file:" + str(DB) + "?mode=ro", uri=True)
    data: dict[str, list[tuple[int, float, float]]] = {}
    for r in conn.execute("SELECT symbol, ts_ms, price, price_change_pct FROM ticker_snap "
                          "WHERE price IS NOT NULL ORDER BY symbol, ts_ms"):
        data.setdefault(r[0], []).append((int(r[1]), float(r[2]), float(r[3] or 0.0)))
    if not data:
        sys.exit("ticker_snap 为空")

    all_ts = [ts for v in data.values() for ts, _, _ in v]
    print("=" * 108)
    print("入场条件统一检验 (未来 %d 分钟收益)" % horizon)
    print("=" * 108)
    print("数据窗口: %s -> %s UTC (约 %.1f 小时), 合约 %d 个, 快照 %d 条" % (
        time.strftime("%m-%d %H:%M", time.gmtime(min(all_ts) / 1000)),
        time.strftime("%m-%d %H:%M", time.gmtime(max(all_ts) / 1000)),
        (max(all_ts) - min(all_ts)) / 3_600_000, len(data), len(all_ts)))
    print("动量窗口: m15 = i-15 | m60 = i-60 (ticker_snap 为 1 分钟粒度)")
    print()
    print("  %-24s %7s %5s %9s %9s %8s %11s %6s %s" % (
        "条件", "样本", "合约", "均值%", "中位%", "胜率%", "去极值均值%", "时段正", "判定"))

    results = []
    for name, fn, desc in CONDS:
        obs: list[tuple[int, str, float]] = []
        for sym, v in data.items():
            if len(v) < 30:
                continue
            for i in range(60, len(v) - horizon - 1):
                ts, px, chg = v[i]
                b15 = v[i - 15][1]
                b60 = v[i - 60][1]
                m15 = (px / b15 - 1) * 100 if b15 else 0.0
                m60 = (px / b60 - 1) * 100 if b60 else 0.0
                if not fn(chg, m15, m60):
                    continue
                obs.append((ts, sym, (v[i + horizon][1] / px - 1) * 100))
        n = len(obs)
        if n < 20:
            print("  %-24s %7d %5s %9s %9s %8s %11s %6s %s" % (
                name, n, "-", "-", "-", "-", "-", "-", "样本过少"))
            continue
        rets = sorted((o[2] for o in obs), reverse=True)
        trim = rets[int(n * 0.05):] or rets
        mean = statistics.fmean(rets)
        med = statistics.median(rets)
        wr = sum(1 for x in rets if x > 0) / n * 100
        tm = statistics.fmean(trim)
        syms = len({o[1] for o in obs})
        span_h = (max(o[0] for o in obs) - min(o[0] for o in obs)) / 3_600_000
        slots = sorted({o[0] for o in obs})
        q = max(len(slots) // 4, 1)
        seg_pos = 0
        for k in range(4):
            lo = slots[k * q]
            hi = slots[min((k + 1) * q - 1, len(slots) - 1)]
            seg = [r for ts, _, r in obs if lo <= ts <= hi]
            if len(seg) >= 10 and statistics.fmean(seg) > 0:
                seg_pos += 1
        ok = (n >= 150 and syms >= 20 and span_h >= 24 and tm > 0 and wr > 52 and seg_pos >= 3)
        print("  %-24s %7d %5d %+9.3f %+9.3f %8.1f %+11.3f %4d/4 %s" % (
            name, n, syms, mean, med, wr, tm, seg_pos, "✔ 可投入" if ok else "✘ 不达标"))
        results.append((name, n, syms, mean, med, wr, tm, seg_pos, ok))

    print()
    print("判读标准: 样本>=150 且 合约>=20 且 跨度>=24h 且 去极值均值>0 且 胜率>52% 且 >=3/4 时段为正")
    print()
    ok_list = [r for r in results if r[8]]
    if ok_list:
        print("通过检验的条件:")
        for r in ok_list:
            print("  %-24s 样本 %5d, 去极值均值 %+.3f%%, 胜率 %.1f%%, 时段 %d/4" % (
                r[0], r[1], r[6], r[5], r[7]))
    else:
        print("⚠️ 没有任何条件通过全部检验。")
        print("   含义: 在 34 小时真实数据上, 这些入场条件本身都不构成稳健的边;")
        print("   已上线实验的正收益更可能来自小样本运气或执行细节。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
