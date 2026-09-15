#!/usr/bin/env python3
"""B 的出场几何二维扫描: 止损上限 x 止盈目标 (用日志真实止损距离 + 真实分钟价格).

一维扫描的结论(2026-09-15): 只收紧止损不够 —— 1.5% 档期望 -0.491%/笔, 仍是负的。
原因是止损越紧胜率越低(61.4% -> 56.8%), 而止盈还钉在 +3.0% 权益。
所以真正要做的是同时调整**止盈**: 让赢的那一边至少和输的那一边一样大。
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


def sim(e, cap, tp, trail_only=False):
    lev, path = e["lev"], e["path"]
    orate = path[0][1]
    dist = max(min(e["dist"], cap), DIST_MIN)
    stop_px = orate * (1.0 - dist)
    peak = 0.0
    P = StopParams(stop_atr_mult=2.0, trail_atr_mult=1.6, trail_start_profit=0.012,
                   hard_stop=HARD_STOP, profit_protect=0.55)
    for k in range(len(path)):
        px = path[k][1]
        cur = (px / orate - 1.0) * lev
        if not trail_only and cur >= tp:
            return tp - COST / 100.0 * lev
        if cur <= HARD_STOP:
            return cur - COST / 100.0 * lev
        if px <= stop_px:
            return (stop_px / orate - 1.0) * lev - COST / 100.0 * lev
        peak = max(peak, cur)
        d, _ = stop_price_distance(cur, lev, dist / 2.0, P, peak_profit=peak)
        d = min(d, cap)
        stop_px = max(stop_px, px * (1.0 - d))
    return (path[-1][1] / orate - 1.0) * lev


def main():
    ents = load()
    now = datetime.now(tz=UTC)
    print("=" * 104)
    print("B 出场几何二维扫描  (样本 %d 笔, 基线胜率 %.1f%%)"
          % (len(ents), sum(1 for e in ents if e["real"] > 0) / len(ents) * 100))
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("  单元格 = 期望值(%%)/笔; 括号内为胜率。绿=正期望")
    print("=" * 104)
    caps = [0.015, 0.020, 0.025, 0.030, 0.040]
    tps = [0.03, 0.045, 0.06, 0.09, 0.12, 0.18]
    print()
    print("  止损上限\\止盈   " + "".join("%14s" % ("+%.1f%%" % (t * 100)) for t in tps))
    for cap in caps:
        row = ""
        for tp in tps:
            vals = [sim(e, cap, tp) for e in ents]
            exp = statistics.mean(vals) * 100
            wr = sum(1 for v in vals if v > 0) / len(vals) * 100
            row += "%14s" % ("%+.2f(%.0f%%)" % (exp, wr))
        print("  %-14s" % ("%.1f%% 价格" % (cap * 100)) + row)
    print()
    print("  纯跟踪止损(不设固定止盈):")
    for cap in caps:
        vals = [sim(e, cap, 0, trail_only=True) for e in ents]
        exp = statistics.mean(vals) * 100
        wr = sum(1 for v in vals if v > 0) / len(vals) * 100
        print("     止损上限 %.1f%% -> 期望 %+.3f%%/笔 | 胜率 %.1f%% | 合计 %+.1f%%"
              % (cap * 100, exp, wr, sum(vals) * 100))
    print()
    best = None
    for cap in caps:
        for tp in tps:
            vals = [sim(e, cap, tp) for e in ents]
            e_ = statistics.mean(vals)
            if best is None or e_ > best[2]:
                best = (cap, tp, e_)
    print("  >>> 网格最优: 止损上限 %.1f%% + 止盈 +%.1f%% 权益 -> 期望 %+.3f%%/笔"
          % (best[0] * 100, best[1] * 100, best[2] * 100))
    print()
    print("  参照: 当前实际配置 = 止损上限 无(平均 %.2f%% 价格) + 止盈 +3.0%% 权益 -> 期望 %.3f%%/笔"
          % (statistics.mean([e["dist"] for e in ents]) * 100, statistics.mean([e["real"] for e in ents]) * 100))


if __name__ == "__main__":
    main()
