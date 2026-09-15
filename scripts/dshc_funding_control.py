#!/usr/bin/env python3
"""关键对照: 「正费率 + 急跌」的优势是急跌带来的, 还是只是「市场在涨」的 beta?

为什么必须先做这个:
    扫描发现正费率组表现远好于负费率组。但正费率 = 市场在做多该标的。若这几天大盘在涨,
    那么任何"正费率"标的都会涨, 与是否急跌无关 —— 那就是 beta, 不是入场信号。
    必须做 2x2 对照(费率符号 x 是否急跌) 才能分离。
"""
from __future__ import annotations
import bisect, sqlite3, statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UTC, CST = timezone.utc, timezone(timedelta(hours=8))
MDB = ROOT / "data" / "m3dsc_market.db"
HORIZON, MIN_COV, GAP = 60, 0.5, 60


def trimmed(v, pct=0.10):
    if not v: return 0.0
    s = sorted(v); k = int(len(s)*pct)
    c = s[k:len(s)-k] if len(s)-2*k >= 3 else s
    return sum(c)/len(c)


def main():
    now = datetime.now(tz=UTC)
    db = sqlite3.connect("file:%s?mode=ro" % MDB, uri=True)
    series = defaultdict(list)
    for sym, ts, px in db.execute("select symbol,ts_ms,price from ticker_snap order by symbol,ts_ms"):
        series[sym].append((ts, px))
    times = {s: [t for t, _ in v] for s, v in series.items()}

    pts = []
    for sym, ts, ann in db.execute("select symbol,ts_ms,funding_ann from rank_snap where funding_ann is not null"):
        arr = series.get(sym)
        if arr is None: continue
        ix = bisect.bisect_right(times[sym], ts) - 1
        if ix < 60 or ts - times[sym][ix] > 120_000: continue
        t_end = ts + HORIZON*60_000
        j = bisect.bisect_left(times[sym], t_end)
        if j >= len(arr) or arr[j][0]-t_end > 150_000: continue
        seg = arr[ix:j+1]
        if len(seg) < HORIZON*MIN_COV: continue
        i15 = bisect.bisect_left(times[sym], ts-15*60_000)
        i60 = bisect.bisect_left(times[sym], ts-60*60_000)
        if i15 >= ix or i60 >= ix: continue
        p0 = arr[ix][1]
        pts.append({"sym": sym, "ts": ts, "ann": ann,
                    "m15": (p0/arr[i15][1]-1)*100, "m60": (p0/arr[i60][1]-1)*100,
                    "r": (arr[j][1]/p0-1)*100})
    print("=" * 100)
    print("2x2 对照: 费率符号 x 是否急跌   (前瞻 %d 分钟, 采样点 %d 个)" % (HORIZON, len(pts)))
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("=" * 100)

    def dedup(sel):
        out, last = [], {}
        for p in sorted(sel, key=lambda x: (x["sym"], x["ts"])):
            if p["ts"] - last.get(p["sym"], -10**15) > GAP*60_000:
                out.append(p); last[p["sym"]] = p["ts"]
        return out

    def row(label, sel):
        ev = dedup(sel)
        if len(ev) < 8:
            print("  %-34s %6d  样本不足" % (label, len(ev))); return None
        r = [p["r"] for p in ev]
        print("  %-34s %6d %+9.3f %+9.3f %+9.3f %8.1f" % (
            label, len(r), sum(r)/len(r)*100 if abs(sum(r)/len(r))<1 else sum(r)/len(r),
            trimmed(r), statistics.median(r), sum(1 for x in r if x>0)/len(r)*100))
        return sum(r)/len(r)
    print()
    print("  %-34s %6s %9s %9s %9s %8s" % ("分组(事件去重)", "事件", "均值%", "去极值%", "中位%", "胜率%"))
    print("  " + "-"*88)
    pos = lambda p: p["ann"] >= 0
    neg = lambda p: p["ann"] < -0.15
    dip15 = lambda p: p["m15"] <= -3.5
    dip1h = lambda p: p["m60"] <= -5.0
    print("  —— 15 分钟急跌档 ——")
    base_pos = row("正费率, 不急跌 (对照/beta)", [p for p in pts if pos(p) and not dip15(p)])
    dip_pos  = row("正费率 + 15m急跌  <-- 信号?", [p for p in pts if pos(p) and dip15(p)])
    base_neg = row("负费率, 不急跌 (对照/beta)", [p for p in pts if neg(p) and not dip15(p)])
    dip_neg  = row("负费率 + 15m急跌  <-- G/F 现行", [p for p in pts if neg(p) and dip15(p)])
    print()
    print("  —— 1 小时急跌档 ——")
    b2 = row("正费率, 不急跌 (对照/beta)", [p for p in pts if pos(p) and not dip1h(p)])
    d2 = row("正费率 + 1h急跌   <-- 信号?", [p for p in pts if pos(p) and dip1h(p)])
    b3 = row("负费率, 不急跌 (对照/beta)", [p for p in pts if neg(p) and not dip1h(p)])
    d3 = row("负费率 + 1h急跌   <-- G/F 现行", [p for p in pts if neg(p) and dip1h(p)])
    print()
    print("  —— 分离 beta 与信号 ——")
    if None not in (base_pos, dip_pos):
        print("   正费率: 急跌 vs 不急跌 的差 = %+.3f%%  <- 这才是急跌的增量" % (dip_pos - base_pos))
    if None not in (base_neg, dip_neg):
        print("   负费率: 急跌 vs 不急跌 的差 = %+.3f%%" % (dip_neg - base_neg))
    if None not in (b2, d2):
        print("   正费率(1h): 急跌 vs 不急跌 的差 = %+.3f%%" % (d2 - b2))
    if None not in (b3, d3):
        print("   负费率(1h): 急跌 vs 不急跌 的差 = %+.3f%%" % (d3 - b3))
    print()
    print("  参照: 全体采样点平均前瞻收益 = %+.3f%%  (若各组都≈这个值, 说明只是大盘在动)"
          % (sum(p['r'] for p in pts)/len(pts)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
