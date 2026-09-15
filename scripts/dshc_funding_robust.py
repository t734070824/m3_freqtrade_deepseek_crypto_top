#!/usr/bin/env python3
"""关键发现「正费率 + 1h急跌」的稳健性三重检验:
   逐标的集中度 / 自助重采样 / 时段分段。

待检验的结论: 把 G/F 的费率门槛从「年化 <= -15%」翻转为「年化 >= 0」是否成立。
"""
from __future__ import annotations
import bisect, random, sqlite3, statistics
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
        if len(arr[ix:j+1]) < HORIZON*MIN_COV: continue
        i60 = bisect.bisect_left(times[sym], ts-60*60_000)
        if i60 >= ix: continue
        p0 = arr[ix][1]
        pts.append({"sym": sym, "ts": ts, "ann": ann,
                    "m60": (p0/arr[i60][1]-1)*100, "r": (arr[j][1]/p0-1)*100})
    def dedup(sel):
        out, last = [], {}
        for p in sorted(sel, key=lambda x: (x["sym"], x["ts"])):
            if p["ts"] - last.get(p["sym"], -10**15) > GAP*60_000:
                out.append(p); last[p["sym"]] = p["ts"]
        return out

    groups = {
        "正费率+1h急跌 (建议)": dedup([p for p in pts if p["ann"] >= 0 and p["m60"] <= -5.0]),
        "负费率+1h急跌 (现行)": dedup([p for p in pts if p["ann"] < -0.15 and p["m60"] <= -5.0]),
        "正费率+15m急跌 (建议)": dedup([p for p in pts if p["ann"] >= 0 and p["m60"] <= -5.0 or (p["ann"] >= 0 and p["m60"] <= -5.0)]),
    }
    groups["正费率+15m急跌 (建议)"] = dedup([p for p in pts if p["ann"] >= 0 and p["m60"] <= -5.0])
    # 正确重算 15m 组
    pts15 = []
    for sym, ts, ann in db.execute("select symbol,ts_ms,funding_ann from rank_snap where funding_ann is not null"):
        arr = series.get(sym)
        if arr is None: continue
        ix = bisect.bisect_right(times[sym], ts) - 1
        if ix < 60 or ts - times[sym][ix] > 120_000: continue
        t_end = ts + HORIZON*60_000
        j = bisect.bisect_left(times[sym], t_end)
        if j >= len(arr) or arr[j][0]-t_end > 150_000: continue
        if len(arr[ix:j+1]) < HORIZON*MIN_COV: continue
        i15 = bisect.bisect_left(times[sym], ts-15*60_000)
        if i15 >= ix: continue
        p0 = arr[ix][1]
        pts15.append({"sym": sym, "ts": ts, "ann": ann,
                      "m15": (p0/arr[i15][1]-1)*100, "r": (arr[j][1]/p0-1)*100})
    groups["正费率+15m急跌 (建议)"] = dedup([p for p in pts15 if p["ann"] >= 0 and p["m15"] <= -3.5])
    groups["负费率+15m急跌 (现行)"] = dedup([p for p in pts15 if p["ann"] < -0.15 and p["m15"] <= -3.5])

    now = datetime.now(tz=UTC)
    print("=" * 100)
    print("关键发现的稳健性检验   (前瞻 %d 分钟, 事件去重口径)" % HORIZON)
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("=" * 100)
    rng = random.Random(11)
    for name, sel in groups.items():
        if len(sel) < 10:
            print("\n%s: 事件 %d, 不足" % (name, len(sel))); continue
        r = [p["r"] for p in sel]
        print("\n" + "-"*100)
        print("【%s】事件 %d, 覆盖 %d 只标的" % (name, len(r), len(set(p["sym"] for p in sel))))
        print("-"*100)
        print("  全样本: 均值 %+8.3f%% | 中位 %+7.3f%% | 去极值 %+7.3f%% | 胜率 %.1f%%"
              % (sum(r)/len(r), statistics.median(r), trimmed(r),
                 sum(1 for x in r if x>0)/len(r)*100))
        per = defaultdict(list)
        for p in sel: per[p["sym"]].append(p["r"])
        print("  逐标的(前 6):")
        for s, v in sorted(per.items(), key=lambda x: -len(x[1]))[:6]:
            print("     %-14s %3d 次  均值 %+8.3f%%  中位 %+7.3f%%" % (s, len(v), sum(v)/len(v), statistics.median(v)))
        # 留一法
        worst = None
        for s in per:
            rest = [x for x2, x in ((p["sym"], p["r"]) for p in sel) if x2 != s]
            t = trimmed(rest)
            if worst is None or t < worst[1]: worst = (s, t)
        print("  剔掉贡献最小的标的(%s)后: 去极值 %+.3f%%" % worst)
        # 自助重采样(以标的为单位)
        syms = list(per)
        boots = []
        for _ in range(4000):
            vals = [x for s in (rng.choice(syms) for _ in syms) for x in per[s]]
            boots.append(trimmed(vals))
        boots.sort()
        print("  自助重采样(以标的为单位, 4000 次): 去极值的 95%% 置信区间 [%+.3f%%, %+.3f%%], 为正概率 %.1f%%"
              % (boots[int(0.025*len(boots))], boots[int(0.975*len(boots))],
                 sum(1 for b in boots if b>0)/len(boots)*100))
        # 时段分段
        ts = sorted(p["ts"] for p in sel)
        mid = ts[len(ts)//2]
        for lab, part in (("前半段", [p for p in sel if p["ts"] < mid]),
                          ("后半段", [p for p in sel if p["ts"] >= mid])):
            v = [p["r"] for p in part]
            if len(v) >= 5:
                print("  %s (n=%d): 去极值 %+7.3f%% | 胜率 %.1f%%" % (
                    lab, len(v), trimmed(v), sum(1 for x in v if x>0)/len(v)*100))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
