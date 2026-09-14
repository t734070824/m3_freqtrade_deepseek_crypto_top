#!/usr/bin/env python3
"""风控缺陷验证: 「止损距离 x 杠杆」决定单笔权益损失, 与设计风险预算无关.

缺陷(2026-09-14 用真实账本定位):
    策略用 plan_position() 按 risk_budget(0.7%) 反推仓位, 并自称把单笔权益风险
    控制在 risk_ceiling(1.0%) 以内。但止损距离的下限是 DIST_MIN=2.5%(价格), 而
    杠杆会把它放大: 2x 时止损一发就是 -5.0% 权益, 3x 时是 -7.5% 权益。
    于是实际亏损中位数恰好落在 risk_ceiling 上, 而不是 risk_budget 上。

    真正的等式: 单笔权益损失 = 止损距离(价格) x 杠杆
                = min(仓位/权益 x 杠杆 x 距离, risk_ceiling)
    在距离被 DIST_MIN 卡死的前提下, 唯一能控制权益损失的是**名义敞口**。

本脚本对每一笔真实交易回答:
    A. 实际结果;
    B. 若单笔名义敞口封顶在 X% 权益, 结果会怎样(其余规则不变);
    C. 触发止损的那些笔, 若给更长时间/更宽距离, 会不会转正(用真实后续价格)。
时间口径: 输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UD = ROOT / "freqtrade" / "user_data"
CST = timezone(timedelta(hours=8))
EXPS = [("G", "m3dsc-g.sqlite"), ("F", "m3dsc-f.sqlite"),
        ("B", "m3dsc-dip.sqlite"), ("C", "m3dsc-carry.sqlite")]
EQUITY = 500.0


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    now = datetime.now(tz=timezone.utc)
    mdb = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "m3dsc_market.db"), uri=True)
    print("=" * 106)
    print("风控缺陷验证: 名义敞口封顶的收益/风险对比")
    print("  时间: %s 北京时间 / %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M UTC")))
    print("  各实验初始权益按 500 USDT 计; 结果按真实成交价线性缩放(不含复利与滑点差异)")
    print("=" * 106)

    allcap: dict[float, list[float]] = defaultdict(list)
    for code, fname in EXPS:
        p = UD / fname
        if not p.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        trades = db.execute(
            "select pair,open_date,close_date,open_rate,close_rate,leverage,stake_amount,"
            "close_profit_abs,exit_reason from trades where is_open=0 order by id").fetchall()
        rows = []
        for pair, od, cd, orate, crate, lev, stake, prof, er in trades:
            if not stake or not prof is None:
                pass
            if not stake:
                continue
            rows.append({"pair": pair, "lev": lev or 1.0, "stake": stake, "profit": prof or 0.0,
                         "er": er or "?", "od": od, "cd": cd, "orate": orate, "crate": crate})
        if not rows:
            continue
        print()
        print("-" * 106)
        print("【%s】%d 笔" % (code, len(rows)))
        print("-" * 106)
        real_ret = [r["profit"] / EQUITY * 100 for r in rows]
        print("  现状: 合计 %+8.2f USDT (权益 %+6.2f%%) | 单笔权益中位 %+6.2f%% | 最差 %+6.2f%% | 名义敞口中位 %.1f%%"
              % (sum(r["profit"] for r in rows), sum(real_ret), statistics.median(real_ret),
                 min(real_ret), statistics.median([r["stake"] * r["lev"] / EQUITY * 100 for r in rows])))
        print()
        print("  %-34s %10s %10s %12s %12s" % ("名义敞口封顶", "合计USDT", "权益%", "最差单笔%", "单笔中位%"))
        print("  " + "-" * 100)
        for cap in (6.0, 8.0, 10.0, 12.0, 15.0, 20.0, 1000.0):
            vals = []
            for r in rows:
                notional = r["stake"] * r["lev"]
                capn = EQUITY * cap / 100.0
                scale = min(1.0, capn / notional) if notional > 0 else 0.0
                v = r["profit"] * scale
                vals.append(v)
                allcap[cap].append(v / EQUITY * 100)
            tot = sum(vals)
            print("  %-34s %+10.2f %+10.2f %+12.2f %+12.2f"
                  % ("%s" % ("不封顶(现状)" if cap > 100 else "%.0f%% 权益" % cap),
                     tot, tot / EQUITY * 100, min(vals) / EQUITY * 100,
                     statistics.median(vals) / EQUITY * 100))
        # 止损笔的后续走势
        st = [r for r in rows if r["er"] == "trailing_stop_loss"]
        if st and st[0]["orate"]:
            follow = []
            for r in st:
                if not r["cd"] or not r["orate"]:
                    continue
                f = mdb.execute("select price from ticker_snap where symbol=? and ts_ms>=? order by ts_ms limit 1",
                                (r["pair"].split("/")[0] + "USDT", to_ms(r["cd"]) + 30 * 60_000)).fetchone()
                if f and f[0]:
                    follow.append((f[0] / r["orate"] - 1) * r["lev"] * 100)
            if follow:
                print()
                print("  被止损的 %d 笔: 平仓 30 分钟后的价格对应权益收益 中位 %+.2f%% | 均值 %+.2f%% | 转正的占 %.0f%%"
                      % (len(follow), statistics.median(follow), sum(follow) / len(follow),
                         sum(1 for x in follow if x > 0) / len(follow) * 100))
                print("  (若此为中位仍为负, 说明止损砍对了 —— 止损本身不是问题, 止损的**距离**才是)")
    print()
    print("=" * 106)
    print("全局对照(全部实验合并):")
    print("  %-20s %12s %12s" % ("名义敞口封顶", "权益收益合计%", "单笔权益中位%"))
    for cap in sorted(allcap):
        v = allcap[cap]
        print("  %-20s %+12.2f %+12.2f"
              % ("不封顶" if cap > 100 else "%.0f%% 权益" % cap, sum(v), statistics.median(v)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
