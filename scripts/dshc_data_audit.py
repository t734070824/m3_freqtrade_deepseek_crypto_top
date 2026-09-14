#!/usr/bin/env python3
"""采集数据可信度校验: 用 freqtrade 自己记录的 min_rate/max_rate 作真值.

为什么需要这个:
    2026-09-14 的深挖中发现采集库存在**孤点坏价格** —— 例如 UAIUSDT 在真实价 0.60 附近时,
    库中反复出现 0.5402(-11.7%)这样的孤立坏值。这类坏点会把「信号检验」和「交易重演」全部带偏。

    好在 freqtrade 每笔交易都记录了它自己看到的 min_rate / max_rate(盘中最低/最高),
    这是独立于我采集库的一份真值。用它可以反过来校验采集数据的可信度。

方法(双重过滤):
    1. **包含性校验**: 若快照价格大幅跑出 [min_rate, max_rate] 区间, 该标的该段数据不可信;
    2. **滚动中位数剔除**: 逐点与前后 30 个点的中位数比较, 偏离超过 6% 的视为孤点坏值并剔除。
    过滤前后分别统计, 让坏点的影响可见。
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


def clean_path(rows: list[tuple[int, float]], win: int = 30, tol: float = 0.06):
    """滚动中位数剔除孤点坏值."""
    if len(rows) < 3:
        return rows, 0
    px = [p for _, p in rows]
    keep = []
    nbad = 0
    for i, (t, p) in enumerate(rows):
        lo = max(0, i - win)
        hiseg = px[lo:i + win + 1]
        med = statistics.median(hiseg)
        if med > 0 and abs(p / med - 1.0) > tol:
            nbad += 1
            continue
        keep.append((t, p))
    return keep, nbad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tol", type=float, default=0.06, help="单点相对滚动中位数的最大偏离")
    args = ap.parse_args()
    now = datetime.now(tz=timezone.utc)
    mdb = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "m3dsc_market.db"), uri=True)

    print("=" * 104)
    print("采集数据可信度校验 (真值 = freqtrade 记录的 min_rate / max_rate)")
    print("  时间: %s 北京时间 / %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M UTC")))
    print("  坏点判据: 与前后 30 点的滚动中位数偏离 > %.0f%%; 包含性判据: 快照跑出 [min_rate,max_rate] 超 2%%"
          % (args.tol * 100))
    print("=" * 104)

    allbad: dict[str, int] = defaultdict(int)
    total_pts = 0
    total_bad = 0
    for code, fname in EXPS:
        p = UD / fname
        if not p.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        trades = db.execute(
            "select pair,open_date,close_date,open_rate,min_rate,max_rate from trades "
            "where is_open=0 order by id").fetchall()
        n_checked = n_outside = n_badpts = n_pts = 0
        worst: list[tuple[float, str, int]] = []
        for pair, od, cd, orate, mn, mx in trades:
            if not orate:
                continue
            sym = pair.split("/")[0] + "USDT"
            t0, t1 = to_ms(od), to_ms(cd) if cd else to_ms(od) + 3600_000
            rows = mdb.execute(
                "select ts_ms,price from ticker_snap where symbol=? and ts_ms>=? and ts_ms<=? order by ts_ms",
                (sym, t0, min(t1, t0 + 3600_000))).fetchall()
            if len(rows) < 3:
                continue
            n_checked += 1
            n_pts += len(rows)
            lo, hi = (mn or orate), (mx or orate)
            out = [x for _, x in rows if x < lo * 0.98 or x > hi * 1.02]
            if out:
                n_outside += 1
                worst.append((len(out) / len(rows), pair, len(out)))
            _, nb = clean_path(rows, tol=args.tol)
            n_badpts += nb
            allbad[pair.split("/")[0]] += nb
        total_pts += n_pts
        total_bad += n_badpts
        print()
        print("【%s】可校验 %d 笔" % (code, n_checked))
        print("   快照点 %d 个, 其中偏离滚动中位数 >%.0f%% 的孤点坏值 %d 个 (%.2f%%)"
              % (n_pts, args.tol * 100, n_badpts, n_badpts / n_pts * 100 if n_pts else 0))
        print("   有快照跑出 [min_rate,max_rate] 的笔数: %d (%.1f%%)"
              % (n_outside, n_outside / n_checked * 100 if n_checked else 0))
        for share, pair, k in sorted(worst, reverse=True)[:3]:
            print("      最严重: %-16s %d/%d 个点跑出区间" % (pair, k, k))

    print()
    print("=" * 104)
    print("全局: 校验快照点 %d 个, 孤点坏值 %d 个 (%.2f%%)" % (total_pts, total_bad,
                                                          total_bad / total_pts * 100 if total_pts else 0))
    print("坏值最多的标的: " + ", ".join("%s=%d" % (k, v) for k, v in
                                    sorted(allbad.items(), key=lambda x: -x[1])[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
