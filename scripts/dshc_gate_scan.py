#!/usr/bin/env python3
"""G/F 入场门槛强度 vs 后续期望 —— 系统性扫描.

要回答的问题:
    「跌得越狠, 反弹越大」这个直觉在什么阈值上成立? 加一层负费率到底是提高还是降低期望?
    现有门槛(15m<=-3.5% 或 1h<=-5%, 年化费率<=-15%)是不是最优?

方法(吸取此前教训):
    * 入口使用 rank_snap 的**时点特征**(funding_ann / change_24h), 无未来函数;
    * 动量窗口用正确的 15 分钟(i-15)与 1 小时(i-60), 1 分钟粒度;
    * 前瞻窗口要求**逐分钟连续**, 断档(09-14 03:00~13:00 UTC 的 11 小时)的样本直接剔除;
    * 同时给**采样点口径**与**事件去重口径**(同标的 60 分钟内命中合并为一次);
    * 报告 均值/中位数/去极值均值/胜率, 并给出逐标的分布以便看集中度。

时间口径: 输出同时标注 北京时间(UTC+8) 与 UTC。
"""
from __future__ import annotations
import argparse, bisect, sqlite3, statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UTC, CST = timezone.utc, timezone(timedelta(hours=8))
MDB = ROOT / "data" / "m3dsc_market.db"
GAP_START = 1789405200000  # 09-14 03:00 UTC 断档起点(ms), 由采集库实测
GAP_END = 1789441200000    # 09-14 13:00 UTC


def trimmed(v, pct=0.10):
    if not v:
        return 0.0
    s = sorted(v)
    k = int(len(s) * pct)
    core = s[k:len(s) - k] if len(s) - 2 * k >= 3 else s
    return sum(core) / len(core)


def load():
    db = sqlite3.connect("file:%s?mode=ro" % MDB, uri=True)
    # 价格序列: rank_snap 的时间戳带毫秒偏移, 与 ticker_snap 不严格相等,
    # 因此保留时间数组, 用二分查找「不晚于该时刻的最近一分钟」
    series = defaultdict(list)
    times = {}
    for sym, ts, px in db.execute("select symbol, ts_ms, price from ticker_snap order by symbol, ts_ms"):
        series[sym].append((ts, px))
    for s, v in series.items():
        times[s] = [t for t, _ in v]
    return db, series, times


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=60, help="前瞻分钟数")
    ap.add_argument("--min-cov", type=float, default=0.5, help="前瞻窗口最低分钟覆盖率")
    ap.add_argument("--gap", type=int, default=60)
    args = ap.parse_args()
    now = datetime.now(tz=UTC)
    db, series, times = load()

    print("=" * 104)
    print("G/F 入场门槛强度 vs 后续期望扫描")
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("  动量窗口 15m=i-15 / 1h=i-60 (1 分钟粒度); 前瞻 %d 分钟; 要求逐分钟连续" % args.horizon)
    print("  特征取 rank_snap 时点值(无未来函数); 事件去重 = 同标的相邻命中 <= %d 分钟合并" % args.gap)
    print("=" * 104)

    pts = []
    skipped_gap = skipped_data = 0
    for sym, ts, ann in db.execute(
            "select symbol, ts_ms, funding_ann from rank_snap where funding_ann is not null"):
        if GAP_START <= ts <= GAP_END:
            skipped_gap += 1
            continue
        arr = series.get(sym)
        if arr is None:
            skipped_data += 1
            continue
        ix = bisect.bisect_right(times[sym], ts) - 1
        if ix < 60 or ix >= len(arr) - 1:
            skipped_data += 1
            continue
        if ts - times[sym][ix] > 120_000:      # 该分钟内不能离得太远
            skipped_data += 1
            continue
        # 前瞻: 按**时间**找到 >= ts+horizon 的第一个点(索引偏移在有小断档时不等价于时间)
        t_end = ts + args.horizon * 60_000
        j = bisect.bisect_left(times[sym], t_end)
        if j >= len(arr) or arr[j][0] - t_end > 150_000:
            skipped_data += 1
            continue
        seg_pts = arr[ix:j + 1]
        # 覆盖率: 窗口内应有足够的分钟点。实测采集库 ticker 逐标的覆盖率只有约 67%
        # (73 小时理论 4380 点, 实际 2936 点), 门槛设 50% 并在此说明该限制。
        if len(seg_pts) < args.horizon * args.min_cov:
            skipped_data += 1
            continue
        # 动量窗口同样按时间取, 避免断档偏移
        i15 = bisect.bisect_left(times[sym], ts - 15 * 60_000)
        i60 = bisect.bisect_left(times[sym], ts - 60 * 60_000)
        if i15 >= ix or i60 >= ix:
            skipped_data += 1
            continue
        p0 = arr[ix][1]
        m15 = (p0 / arr[i15][1] - 1) * 100
        m60 = (p0 / arr[i60][1] - 1) * 100
        seg = [p for _, p in seg_pts]
        pts.append({"sym": sym, "ts": ts, "ann": ann, "m15": m15, "m60": m60,
                    "r": (arr[j][1] / p0 - 1) * 100,
                    "mfe": (max(seg) / p0 - 1) * 100, "mae": (min(seg) / p0 - 1) * 100,
                    "px": p0})
    print("  可用采样点 %d 个 (断档剔除 %d, 数据不足剔除 %d)"
          % (len(pts), skipped_gap, skipped_data))
    print("  覆盖 %d 只标的" % len(set(p["sym"] for p in pts)))

    def dedup(sel):
        out, last = [], {}
        for p in sorted(sel, key=lambda x: (x["sym"], x["ts"])):
            if p["ts"] - last.get(p["sym"], -10**15) > args.gap * 60_000:
                out.append(p)
                last[p["sym"]] = p["ts"]
        return out

    def report(title, sel):
        if len(sel) < 8:
            print("\n  %s: 样本 %d, 不足" % (title, len(sel)))
            return
        ev = dedup(sel)
        r = [p["r"] for p in sel]
        re_ = [p["r"] for p in ev]
        mfe = [p["mfe"] for p in sel]
        print("\n  %s" % title)
        print("     采样点口径: n=%5d 均值%+7.3f%% 中位%+7.3f%% 去极值%+7.3f%% 胜率%5.1f%%"
              % (len(r), sum(r) / len(r), statistics.median(r), trimmed(r),
                 sum(1 for x in r if x > 0) / len(r) * 100))
        print("     事件去重后: n=%5d 均值%+7.3f%% 中位%+7.3f%% 去极值%+7.3f%% 胜率%5.1f%%"
              % (len(re_), sum(re_) / len(re_), statistics.median(re_), trimmed(re_),
                 sum(1 for x in re_ if x > 0) / len(re_) * 100))
        print("     前瞻期间 MFE中位%+6.3f%% MAE中位%+6.3f%% | 覆盖 %d 只标的"
              % (statistics.median(mfe), statistics.median([p["mae"] for p in sel]),
                 len(set(p["sym"] for p in sel))))

    print()
    print("=" * 104)
    print("【A】只按「15 分钟跌幅」分档 (不加费率门槛)")
    print("=" * 104)
    bands15 = [(-2.0, -2.5), (-2.5, -3.0), (-3.0, -3.5), (-3.5, -4.0),
               (-4.0, -5.0), (-5.0, -6.0), (-6.0, -8.0), (-8.0, -100.0)]
    print("  %-14s %7s %10s %10s %10s %8s" % ("15m 跌幅区间", "采样点", "均值%", "去极值%", "中位%", "胜率%"))
    for lo, hi in bands15:
        sel = [p for p in pts if hi <= p["m15"] < lo]
        if len(sel) < 8:
            continue
        r = [p["r"] for p in sel]
        print("  %-14s %7d %+10.3f %+10.3f %+10.3f %7.1f%%" % (
            "%.1f%% ~ %.1f%%" % (hi, lo), len(r), sum(r) / len(r), trimmed(r),
            statistics.median(r), sum(1 for x in r if x > 0) / len(r) * 100))

    print()
    print("=" * 104)
    print("【B】只按「1 小时跌幅」分档")
    print("=" * 104)
    print("  %-14s %7s %10s %10s %10s %8s" % ("1h 跌幅区间", "采样点", "均值%", "去极值%", "中位%", "胜率%"))
    for lo, hi in bands15:
        sel = [p for p in pts if hi <= p["m60"] < lo]
        if len(sel) < 8:
            continue
        r = [p["r"] for p in sel]
        print("  %-14s %7d %+10.3f %+10.3f %+10.3f %7.1f%%" % (
            "%.1f%% ~ %.1f%%" % (hi, lo), len(r), sum(r) / len(r), trimmed(r),
            statistics.median(r), sum(1 for x in r if x > 0) / len(r) * 100))

    print()
    print("=" * 104)
    print("【C】费率门槛的边际作用 (固定 15m<=-3.5%% 的急跌, 只看费率档位)")
    print("=" * 104)
    dips = [p for p in pts if p["m15"] <= -3.5]
    report("急跌 15m<=-3.5%, 不加费率门槛", dips)
    for lower, upper in ((0.0, 1e9), (-0.15, 0.0), (-0.30, -0.15), (-0.60, -0.30),
                         (-1.00, -0.60), (-1e9, -1.00)):
        sel = [p for p in dips if lower <= p["ann"] < upper]
        lab = ("正费率" if lower == 0.0 else
               "<= -100%%" if lower == -1e9 else
               "%.0f%% ~ %.0f%%" % (lower * 100, upper * 100))
        report("急跌 + 年化费率 %s" % lab, sel)

    print()
    print("=" * 104)
    print("【D】对照: 用「1h<=-5%%」的急跌档重复费率扫描")
    print("=" * 104)
    d1h = [p for p in pts if p["m60"] <= -5.0]
    report("急跌 1h<=-5%, 不加费率门槛", d1h)
    for lower, upper in ((0.0, 1e9), (-0.15, 0.0), (-0.30, -0.15), (-0.60, -0.30), (-1e9, -0.60)):
        sel = [p for p in d1h if lower <= p["ann"] < upper]
        lab = ("正费率" if lower == 0.0 else
               "<= -60%%" if lower == -1e9 else
               "%.0f%% ~ %.0f%%" % (lower * 100, upper * 100))
        report("1h急跌 + 年化费率 %s" % lab, sel)

    print()
    print("=" * 104)
    print("【E】现行门槛 vs 候选更严门槛 (事件去重口径, 便于直接比较)")
    print("=" * 104)
    print("  %-46s %6s %10s %10s %10s %8s" % ("门槛组合", "事件", "均值%", "去极值%", "中位%", "胜率%"))
    cand = [
        ("现行 G/F: (15m<=-3.5 或 1h<=-5) 且费率<=-15%",
         lambda p: (p["m15"] <= -3.5 or p["m60"] <= -5.0) and p["ann"] <= -0.15),
        ("收紧跌幅: (15m<=-4.5 或 1h<=-6.5) 且费率<=-15%",
         lambda p: (p["m15"] <= -4.5 or p["m60"] <= -6.5) and p["ann"] <= -0.15),
        ("收紧费率<=-30%: (15m<=-3.5 或 1h<=-5) 且费率<=-30%",
         lambda p: (p["m15"] <= -3.5 or p["m60"] <= -5.0) and p["ann"] <= -0.30),
        ("双收紧: (15m<=-4.5 或 1h<=-6.5) 且费率<=-30%",
         lambda p: (p["m15"] <= -4.5 or p["m60"] <= -6.5) and p["ann"] <= -0.30),
        ("只放宽费率<=-5%: (15m<=-3.5 或 1h<=-5) 且费率<=-5%",
         lambda p: (p["m15"] <= -3.5 or p["m60"] <= -5.0) and p["ann"] <= -0.05),
        ("不要费率: (15m<=-3.5 或 1h<=-5)",
         lambda p: p["m15"] <= -3.5 or p["m60"] <= -5.0),
    ]
    for label, fn in cand:
        ev = dedup([p for p in pts if fn(p)])
        if len(ev) < 8:
            print("  %-46s %6d  样本不足" % (label, len(ev)))
            continue
        r = [p["r"] for p in ev]
        print("  %-46s %6d %+10.3f %+10.3f %+10.3f %7.1f%%" % (
            label, len(r), sum(r) / len(r), trimmed(r), statistics.median(r),
            sum(1 for x in r if x > 0) / len(r) * 100))
    print()
    print("  注: 前瞻 %d 分钟的价格收益, 未扣手续费/滑点。现行配置在实盘上的每笔权益贡献约 -0.066%%,"
          % args.horizon)
    print("      远低于此表给出的数字 —— 说明「采样点口径」系统性高估, 只能用于**相对比较**。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
