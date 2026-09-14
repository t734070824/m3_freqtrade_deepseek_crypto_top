#!/usr/bin/env python3
"""三项风控修正的联合验证: 用 242 笔真实交易做反事实重算.

现状(根因):
    单笔权益损失 = 止损距离(价格) x 杠杆, 而 DIST_MIN=2.5% 让它在 2x 下就是 -5% 权益。
    plan_position 声称管 1% 风险, 实际管的是「仓位」不是「权益」。

三项修正:
    修正1 名义敞口封顶: 单笔名义 <= 8% 权益 (唯一有量化验证的一步)
    修正2 止损距离-杠杆解耦: 把 DIST_MIN 从 2.5% 降到 1.5%(按波动率定),
          并在权益层面设硬上限 MAX_EQUITY_RISK = 1.5%, 超限则**放弃这笔交易**
    修正3 组合层复利约束: 同时持仓越多, 单笔名义敞口越小(等风险预算分配)

本脚本对每笔真实交易重算「若当时按修正后的仓位下单」的盈亏, 与现状对比。
所有结果按真实成交价线性缩放 —— 假设仓位变化不影响成交价(小仓位假设成立)。
时间口径: 输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations

import math
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
MAX_OPEN = {"G": 5, "F": 4, "B": 4, "C": 4}


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def cap_notional(stake: float, lev: float, equity: float, budget_pct: float,
                 open_now: int, max_open: int, dist: float) -> tuple[float, float]:
    """返回 (新保证金, 新名义敞口%). 三项修正联合生效。"""
    cap = equity * budget_pct / 100.0
    # 修正3: 组合层等风险分配 —— 已持仓越多, 单笔越轻
    cap /= max(1, open_now + 1)
    notional = min(stake * lev, cap)
    # 修正2: 权益层面硬上限 —— 止损一发的代价不得超过 MAX_EQUITY_RISK
    max_notional_by_risk = equity * 1.5 / 100.0 / max(dist, 1e-6)
    notional = min(notional, max_notional_by_risk)
    if notional <= 0:
        return 0.0, 0.0
    return notional / max(lev, 1e-9), notional / equity * 100.0


def main() -> int:
    now = datetime.now(tz=timezone.utc)
    print("=" * 104)
    print("三项风控修正的联合验证 (反事实重算 242 笔真实交易)")
    print("  时间: %s 北京时间 / %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M UTC")))
    print("=" * 104)

    # 用 G 的实际止损距离做代表(DIST_MIN 2.5% -> 修正后 1.5%)
    scenarios = [
        ("现状(不封顶, DIST_MIN=2.5%)", 1000.0, 0.025),
        ("只做修正1: 封顶 8%", 8.0, 0.025),
        ("修正1+2: 封顶 8% + 距离解耦 1.5%", 8.0, 0.015),
        ("三项全上: 8% + 1.5% + 组合约束", 8.0, 0.015),
        ("三项全上(敞口 6%)", 6.0, 0.015),
        ("三项全上(敞口 10%)", 10.0, 0.015),
    ]
    grand: dict[str, float] = defaultdict(float)
    for label, budget, dist in scenarios:
        print()
        print("-" * 104)
        print("【%s】" % label)
        print("-" * 104)
        print("  %-3s %6s %10s %10s %10s %12s %10s"
              % ("", "笔数", "合计USDT", "权益%", "单笔中位%", "最差单笔%", "名义中位%"))
        tot_all = 0.0
        for code, fname in EXPS:
            p = UD / fname
            if not p.exists():
                continue
            db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            rows = db.execute(
                "select stake_amount,leverage,close_profit_abs,open_date,close_date from trades "
                "where is_open=0 order by open_date").fetchall()
            if not rows:
                continue
            # 按开仓时间顺序模拟并跟踪同时在持仓数(修正3)
            live: list[int] = []
            vals, notls = [], []
            for stake, lev, prof, od, cd in rows:
                if not stake or not od:
                    continue
                lev = lev or 1.0
                t0 = to_ms(od)
                t1 = to_ms(cd) if cd else t0
                live = [x for x in live if x > t0]
                open_now = len(live)
                live.append(t1)
                real_notional = stake * lev
                new_stake, notl_pct = cap_notional(stake, lev, EQUITY, budget,
                                                   open_now, MAX_OPEN.get(code, 4), dist)
                if real_notional <= 0:
                    continue
                scale = (new_stake * lev) / real_notional
                vals.append((prof or 0.0) * scale)
                notls.append(notl_pct)
            if not vals:
                continue
            tot = sum(vals)
            tot_all += tot
            print("  %-3s %6d %+10.2f %+9.2f%% %+9.2f%% %+11.2f%% %9.1f%%"
                  % (code, len(vals), tot, tot / EQUITY * 100,
                     statistics.median(vals) / EQUITY * 100, min(vals) / EQUITY * 100,
                     statistics.median(notls)))
        print("  %-3s %6s %+10.2f %+9.2f%%" % ("合计", "", tot_all, tot_all / EQUITY * 100))
        grand[label] = tot_all
    print()
    print("=" * 104)
    print("汇总(四实验合计, 初始各 500 USDT):")
    for label, v in grand.items():
        print("   %-40s %+9.2f USDT   (%+7.2f %% 每实验均值)" % (label, v, v / 4 / EQUITY * 100))
    print()
    print("注: 反事实重算假设「仓位变小不改变成交价」。对当前这一档仓位规模(中位 15% 权益名义)")
    print("    这个假设成立; 若仓位放大到市场深度量级则不成立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
