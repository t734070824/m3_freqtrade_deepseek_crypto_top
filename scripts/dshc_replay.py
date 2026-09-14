#!/usr/bin/env python3
"""用真实分钟价格重演每一笔已平仓交易, 分离「入场质量」与「出场设计」.

问题: 2026-09-14 四个实验全线转负, 且亏损绝大多数来自 trailing_stop_loss。
      到底是入场信号不行, 还是出场把赢面吃掉了? 这决定了下一步该改哪一边。

方法:
  1. 从各实验账本取每一笔已平仓交易的 标的 + 开仓时间 + 开仓价 + 杠杆 + 仓位;
  2. 从采集库取该标的开仓后最长 24 小时的逐分钟真实价格;
  3. 用**同一入场点**重演多套出场规则, 与「实际发生的结果」并列对比;
  4. 另算「入场质量」指标: 开仓后 30/60 分钟内曾达到的最大浮盈(不看出场)。

  这样就能把「这笔开仓对不对」与「这套出场冤不冤」分开衡量。
  逐分钟快照看不到盘中极值, 因此结果偏保守(会低估触及档位的概率), 但对比是公平的。
时间口径: 输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import argparse
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


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def simulate(path: list[tuple[int, float]], px0: float, lev: float, stake: float,
             tiers: tuple, stop_pct: float, max_min: int, cost: float) -> float:
    """返回该笔的盈亏(USDT). 仓位 = stake 保证金, 名义 = stake*lev。"""
    notional = stake * lev
    stop_frac = 1.0 - stop_pct / 100.0
    n_left, realized, nxt = 1.0, 0.0, 0
    tlist = sorted(t for t in tiers if t)
    end = px0 * 1e18
    for k in range(1, min(max_min + 1, len(path))):
        px = path[k][1]
        ret = px / px0 - 1.0
        if ret <= -stop_pct / 100.0:
            return realized + n_left * (ret - cost) * notional
        while nxt < len(tlist) and ret >= tlist[nxt] / 100.0:
            realized += n_left * 0.40 * (tlist[nxt] / 100.0 - cost) * notional
            n_left *= 0.60
            nxt += 1
        if n_left <= 0.01:
            return realized
    if len(path) > 1:
        ret = path[min(max_min, len(path) - 1)][1] / px0 - 1.0
        realized += n_left * (ret - cost) * notional
    return realized


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost", type=float, default=0.10, help="单边成本 pct")
    args = ap.parse_args()
    now = datetime.now(tz=timezone.utc)
    mdb = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "m3dsc_market.db"), uri=True)

    print("=" * 106)
    print("真实账本重演: 入场质量 vs 出场设计")
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M UTC")))
    print("  逐分钟快照重演, 单边成本 %.2f%%; 同一入场点换不同出场规则" % args.cost)
    print("=" * 106)

    for code, fname in EXPS:
        p = UD / fname
        if not p.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        trades = db.execute(
            "select pair,open_date,open_rate,leverage,stake_amount,close_profit_abs,"
            "exit_reason,is_short from trades where is_open=0 order by id").fetchall()
        if not trades:
            continue
        rows, mfe30, mfe60, mfe_full = [], [], [], []
        for pair, od, orate, lev, stake, prof, er, short in trades:
            sym = pair.split("/")[0] + "USDT"
            t0 = to_ms(od)
            path = mdb.execute(
                "select ts_ms,price from ticker_snap where symbol=? and ts_ms>=? order by ts_ms limit 1500",
                (sym, t0)).fetchall()
            if len(path) < 5 or not orate or not stake:
                continue
            lev = lev or 1.0
            # 入场质量: 入场后 30/60 分钟内的最高浮盈(名义收益率 x 杠杆 = 权益收益)
            def peak(win: int) -> float:
                seg = path[:min(win, len(path))]
                return (max(px for _, px in seg) / orate - 1.0) * lev * 100
            mfe30.append(peak(30))
            mfe60.append(peak(60))
            r = {"sym": sym, "actual": prof or 0.0, "lev": lev, "stake": stake,
                 "path": path, "orate": orate}
            rows.append(r)
        if not rows:
            continue
        print()
        print("-" * 106)
        print("【%s】可重演 %d 笔 (共 %d 笔已平仓)" % (code, len(rows), len(trades)))
        print("-" * 106)
        act = [r["actual"] for r in rows]
        print("  实际结果        : 合计 %+8.2f | 均值 %+6.3f | 胜率 %5.1f%%"
              % (sum(act), sum(act) / len(act), sum(1 for x in act if x > 0) / len(act) * 100))
        if mfe30:
            print("  入场质量(不看出场): 开仓后 30 分钟内最高浮盈 中位 %+6.3f%% | 均值 %+6.3f%% | 曾转正占比 %4.1f%%"
                  % (statistics.median(mfe30), sum(mfe30) / len(mfe30),
                     sum(1 for x in mfe30 if x > 0) / len(mfe30) * 100))
            print("                      开仓后 60 分钟内最高浮盈 中位 %+6.3f%% | 均值 %+6.3f%% | 曾转正占比 %4.1f%%"
                  % (statistics.median(mfe60), sum(mfe60) / len(mfe60),
                     sum(1 for x in mfe60 if x > 0) / len(mfe60) * 100))
        print()
        designs = [
            ("现行: 阶梯 +3.5/+8, 止损 -5%, 最长 6h", (3.5, 8.0), 5.0, 360),
            ("B式: 单一 +4% 即走, 止损 -4.5%, 最长 1h", (4.0,), 4.5, 60),
            ("F式: 阶梯 +6/+15, 止损 -5%, 最长 24h", (6.0, 15.0), 5.0, 1440),
            ("纯持有 1h, 止损 -5%", (), 5.0, 60),
            ("纯持有 3h, 止损 -5%", (), 5.0, 180),
            ("纯持有 6h, 止损 -5%", (), 5.0, 360),
            ("纯持有 24h, 止损 -5%", (), 5.0, 1440),
            ("近档: 阶梯 +2/+4, 止损 -3%, 最长 2h", (2.0, 4.0), 3.0, 120),
            ("无止损纯持有 6h", (), 99.0, 360),
        ]
        print("  %-40s %10s %10s %10s" % ("出场规则(同一入场点)", "合计", "均值/笔", "胜率%"))
        print("  " + "-" * 100)
        for label, tiers, stop, mx in designs:
            vals = []
            for r in rows:
                vals.append(simulate(r["path"], r["orate"], r["lev"], r["stake"],
                                     tiers, stop, mx, args.cost))
            print("  %-40s %+10.2f %+10.3f %10.1f"
                  % (label, sum(vals), sum(vals) / len(vals),
                     sum(1 for v in vals if v > 0) / len(vals) * 100))
    print()
    print("=" * 106)
    print("读法: 若「入场质量」显示开仓后多数时间曾浮盈, 而各套出场规则合计仍为负,")
    print("      则问题在出场/止损太紧; 若多数交易一开仓就再没回头(曾转正占比很低), 则入场信号本身有问题。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
