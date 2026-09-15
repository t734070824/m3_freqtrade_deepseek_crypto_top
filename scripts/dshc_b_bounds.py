#!/usr/bin/env python3
"""B 出场几何 —— 悲观边界检验.

为什么需要(自证的偏差):
    回放用逐分钟快照, **看不到分钟内的极值**。止损越紧, 越容易在分钟内被扫到又弹回,
    于是紧止损的档位被系统性高估。
    证据: 基线回放的均亏 -6.29% vs 账本实际 -9.16% —— 回放低估了约 32% 的损失。

做法(给结论上悲观边界):
    对每一步 k -> k+1, 只知道两端的价格, 不知道中间怎么走。取**最不利**的路径假设:
    若止损位落在 [min(px_k, px_k+1), max(px_k, px_k+1)] 之间, 就认为它**先被触发**。
    这样得到的是下界(悲观值)。再与乐观值(只看端点)并列, 结论必须两者都为正才站得住。
"""
from __future__ import annotations
import re, sqlite3, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "freqtrade" / "user_data"))
from stops_core import DIST_MIN, StopParams, stop_price_distance  # noqa: E402

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
MDB = ROOT / "data" / "m3dsc_market.db"
LEDGERS = [ROOT / "freqtrade/user_data/archive/m3dsc-dip.sqlite.20260914-1454Z",
           ROOT / "freqtrade/user_data/m3dsc-dip.sqlite"]
LOGS = [ROOT / "logs/freqtrade-dip.log"] + sorted((ROOT / "logs/archive").glob("freqtrade-dip.*.log"))
HARD_STOP, MAX_HOLD_MIN, COST = -0.045, 240, 0.10
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[DIP\] (\S+) 仓位: 止损距离([0-9.]+)% 杠杆([0-9.]+)")


def to_ms(s):
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def load():
    dists = {}
    for lg in LOGS:
        if not lg.exists():
            continue
        for line in lg.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = LINE.match(line)
            if m:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
                dists[(m.group(2), int(ts.timestamp()))] = float(m.group(3)) / 100.0
    mdb = sqlite3.connect("file:%s?mode=ro" % MDB, uri=True)
    ents = []
    for lg in LEDGERS:
        if not lg.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % lg, uri=True)
        for pair, od, orate, lev, stake, prof in db.execute(
                "select pair,open_date,open_rate,leverage,stake_amount,close_profit_abs from trades where is_open=0"):
            if not od or not orate or not stake:
                continue
            t0 = to_ms(od)
            d = next((dists[(pair, int(t0 / 1000) + o)] for o in range(-90, 91)
                      if (pair, int(t0 / 1000) + o) in dists), None)
            if d is None:
                continue
            sym = pair.split("/")[0] + "USDT"
            rows = mdb.execute("select ts_ms, price from ticker_snap where symbol=? and ts_ms>=? and ts_ms<=? "
                               "order by ts_ms", (sym, t0, t0 + MAX_HOLD_MIN * 60_000)).fetchall()
            if len(rows) < 15:
                continue
            ents.append({"lev": lev or 1.0, "dist": d, "path": rows, "real": (prof or 0) / stake})
    return ents


def sim(e, cap, tp, trail_only=False, pessimistic=False):
    lev, path = e["lev"], e["path"]
    orate = path[0][1]
    dist = max(min(e["dist"], cap) if cap else e["dist"], DIST_MIN)
    stop_px = orate * (1.0 - dist)
    peak = 0.0
    P = StopParams(stop_atr_mult=2.0, trail_atr_mult=1.6, trail_start_profit=0.012,
                   hard_stop=HARD_STOP, profit_protect=0.55)
    for k in range(len(path)):
        px = path[k][1]
        npx = path[k + 1][1] if k + 1 < len(path) else px
        cur = (px / orate - 1.0) * lev
        if not trail_only and cur >= tp:
            return tp - COST / 100.0 * lev
        if cur <= HARD_STOP:
            return cur - COST / 100.0 * lev
        if px <= stop_px:
            return (stop_px / orate - 1.0) * lev - COST / 100.0 * lev
        if pessimistic:
            # 悲观: 止损位若落在本步两端之间, 认为它先被打到
            lo, hi = min(px, npx), max(px, npx)
            if lo <= stop_px <= hi and npx > px:
                return (stop_px / orate - 1.0) * lev - COST / 100.0 * lev
        peak = max(peak, cur)
        d, _ = stop_price_distance(cur, lev, dist / 2.0, P, peak_profit=peak)
        d = min(d, cap) if cap else d
        stop_px = max(stop_px, px * (1.0 - d))
    return (path[-1][1] / orate - 1.0) * lev


def main():
    ents = load()
    now = datetime.now(tz=UTC)
    print("=" * 100)
    print("B 出场几何: 乐观 / 悲观 双边界  (样本 %d 笔)" % len(ents))
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("=" * 100)
    print()
    print("  %-26s %12s %12s %12s %10s" % ("配置", "乐观期望%", "悲观期望%", "账本实际%", "悲观胜率%"))
    print("  " + "-" * 84)
    rows = [("现状(平均距离4.87% + 止盈3%)", None, 0.030, False),
            ("上限1.5% + 止盈3%", 0.015, 0.030, False),
            ("上限1.5% + 纯跟踪", 0.015, 0, True),
            ("上限2.0% + 纯跟踪", 0.020, 0, True),
            ("上限2.5% + 纯跟踪", 0.025, 0, True),
            ("上限1.5% + 止盈6%", 0.015, 0.060, False),
            ("上限1.5% + 止盈18%", 0.015, 0.180, False)]
    real_exp = statistics.mean([e["real"] for e in ents]) * 100
    for label, cap, tp, to in rows:
        opt = [sim(e, cap, tp, to, False) for e in ents]
        pes = [sim(e, cap, tp, to, True) for e in ents]
        print("  %-26s %+12.3f %+12.3f %+12.3f %10.1f" % (
            label, statistics.mean(opt) * 100, statistics.mean(pes) * 100, real_exp,
            sum(1 for v in pes if v > 0) / len(pes) * 100))
    print()
    print("  >>> 判定: 只有**悲观列也为正**的配置才值得上线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
