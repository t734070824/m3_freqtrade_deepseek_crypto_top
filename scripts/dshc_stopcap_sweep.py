#!/usr/bin/env python3
"""B 止损上限对照回放 v2 —— 用日志里逐笔记录的真实止损距离, 不再猜 ATR.

v1 的失败(记录在案):
    v1 用「5 分钟桶的(最高-最低)/最低」当 ATR 代理, 对逐分钟快照严重低估(算出 ~0.4%),
    于是 entry_stop_distance 被钳到 DIST_MIN=1.5%, 所有档位算出同一结果 —— 结论无效。
    v2 改为**从策略日志解析每笔真实使用的止损距离**(2.13%~4.76%), 这是地面真值。

回放规则(复刻 M3DipRevert):
    * 止盈: 权益收益 >= +3.0% -> 平仓
    * 硬止损: 权益收益 <= -4.5% -> 平仓
    * 跟踪止损: 距离 d 从当前价往上抬(只收紧); d 在浮盈 <=1.2% 时等于开仓距离,
      之后按 stops_core 的分档收紧(本次扫描额外施加上限 cap)
    * 最长持有 240 分钟
先跑基线(不加 cap)与实际账本对照; 对得上才看扫描结果。
"""
from __future__ import annotations
import argparse, re, sqlite3, statistics, sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "freqtrade" / "user_data"))
from stops_core import DIST_MAX, DIST_MIN, StopParams, stop_price_distance  # noqa: E402

UTC = timezone.utc
CST = timezone(timedelta(hours=8))
MDB = ROOT / "data" / "m3dsc_market.db"
LEDGERS = [ROOT / "freqtrade/user_data/archive/m3dsc-dip.sqlite.20260914-1454Z",
           ROOT / "freqtrade/user_data/m3dsc-dip.sqlite"]
LOGS = [ROOT / "logs/freqtrade-dip.log"] + sorted((ROOT / "logs/archive").glob("freqtrade-dip.*.log"))
EXIT_PROFIT, MAX_HOLD_MIN, HARD_STOP = 0.030, 240, -0.045
P = StopParams(stop_atr_mult=2.0, trail_atr_mult=1.6, trail_start_profit=0.012,
               hard_stop=HARD_STOP, profit_protect=0.55)
LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*\[DIP\] (\S+) 仓位: 止损距离([0-9.]+)% 杠杆([0-9.]+)")


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=UTC).timestamp() * 1000)


def parse_log_distances() -> dict[tuple[str, int], float]:
    """从日志解析 (pair, 开仓UTC秒) -> 真实止损距离."""
    out = {}
    for lg in LOGS:
        if not lg.exists():
            continue
        for line in lg.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = LINE.match(line)
            if not m:
                continue
            ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=CST)
            out[(m.group(2), int(ts.timestamp()))] = float(m.group(3)) / 100.0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-hold", type=int, default=MAX_HOLD_MIN)
    ap.add_argument("--cost", type=float, default=0.10, help="单边成本 pct(价格口径)")
    args = ap.parse_args()
    now = datetime.now(tz=UTC)
    mdb = sqlite3.connect("file:%s?mode=ro" % MDB, uri=True)
    dists = parse_log_distances()
    print("=" * 104)
    print("B(M3DipRevert) 止损上限对照回放 v2  (用日志真实止损距离)")
    print("  时间: %s 北京时间 / %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M")))
    print("  日志解析到 %d 条逐笔止损距离" % len(dists))
    print("=" * 104)

    entries = []
    for lg in LEDGERS:
        if not lg.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % lg, uri=True)
        for pair, od, orate, lev, stake, prof in db.execute(
                "select pair,open_date,open_rate,leverage,stake_amount,close_profit_abs "
                "from trades where is_open=0"):
            if not od or not orate or not stake:
                continue
            t0 = to_ms(od)
            # 匹配日志里的真实止损距离(±90 秒)
            d = None
            for off in range(-90, 91):
                if (pair, t0 // 1 * 0 + int(t0 / 1000) + off) in dists:
                    d = dists[(pair, int(t0 / 1000) + off)]
                    break
            if d is None:
                continue
            sym = pair.split("/")[0] + "USDT"
            rows = mdb.execute(
                "select ts_ms, price from ticker_snap where symbol=? and ts_ms>=? and ts_ms<=? order by ts_ms",
                (sym, t0, t0 + args.max_hold * 60_000)).fetchall()
            if len(rows) < 15:
                continue
            entries.append({"pair": pair, "lev": lev or 1.0, "stake": stake, "dist": d,
                            "path": rows, "real": (prof or 0.0) / stake})
    print("  可用样本 %d 笔 (既有真实止损距离、又有足够分钟价格)" % len(entries))

    def run(cap):
        out = []
        for e in entries:
            dist = min(e["dist"], cap) if cap else e["dist"]
            dist = max(dist, DIST_MIN)
            lev, orate, path = e["lev"], e["path"][0][1], e["path"]
            stop_px = orate * (1.0 - dist)
            peak = 0.0
            res = None
            for k in range(len(path)):
                px = path[k][1]
                cur = (px / orate - 1.0) * lev
                if cur >= EXIT_PROFIT:
                    res = EXIT_PROFIT - args.cost / 100.0 * lev
                    break
                if cur <= HARD_STOP:
                    res = cur - args.cost / 100.0 * lev
                    break
                if px <= stop_px:
                    res = (stop_px / orate - 1.0) * lev - args.cost / 100.0 * lev
                    break
                peak = max(peak, cur)
                d, _ = stop_price_distance(cur, lev, dist / 2.0, P, peak_profit=peak)
                d = min(d, dist if not cap else min(dist, cap))
                stop_px = max(stop_px, px * (1.0 - d))
            if res is None:
                res = (path[-1][1] / orate - 1.0) * lev
            out.append(res)
        return out

    print()
    print("-" * 104)
    print("【基线校验】不加 cap, 用日志里的真实止损距离复刻 —— 应与账本接近")
    print("-" * 104)
    base = run(None)
    real = [e["real"] for e in entries]
    print("  %-16s %6s %9s %10s %10s %10s" % ("", "笔数", "胜率%", "均盈%", "均亏%", "合计%"))
    for tag, vals in (("账本实际", real), ("回放(基线)", base)):
        w = [v for v in vals if v > 0]
        l = [v for v in vals if v <= 0]
        print("  %-16s %6d %9.1f %+10.2f %+10.2f %+10.1f" % (
            tag, len(vals), len(w) / len(vals) * 100,
            statistics.mean(w) * 100 if w else 0, statistics.mean(l) * 100 if l else 0,
            sum(vals) * 100))

    print()
    print("-" * 104)
    print("【止损上限扫描】其余不变(止盈 +3.0% 权益, 硬止损 -4.5% 权益, 最长 4 小时)")
    print("-" * 104)
    print("  %-10s %6s %9s %10s %10s %11s %9s %10s" % (
        "止损上限", "笔数", "胜率%", "均盈%", "均亏%", "期望/笔%", "盈亏比", "合计%"))
    best = None
    for cap in (0.015, 0.020, 0.025, 0.030, 0.035, 0.040, 0.050, 0.080):
        vals = run(cap)
        w = [v for v in vals if v > 0]
        l = [v for v in vals if v <= 0]
        aw = statistics.mean(w) if w else 0
        al = statistics.mean(l) if l else 0
        exp = statistics.mean(vals)
        print("  %-10s %6d %9.1f %+10.2f %+10.2f %+11.3f %9.2f %+10.1f" % (
            "%.1f%%" % (cap * 100), len(vals), len(w) / len(vals) * 100,
            aw * 100, al * 100, exp * 100, abs(aw / al) if al else float("inf"), sum(vals) * 100))
        if best is None or exp > best[1]:
            best = (cap, exp)
    print()
    print("  >>> 最优止损上限 %.1f%%  ->  期望 %+.3f%% / 笔" % (best[0] * 100, best[1] * 100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
