#!/usr/bin/env python3
"""单笔归因: 入场后价格走势 vs 实际兑现, 找出「为什么赢面没兑现」.

关键校正(2026-09-14):
    之前的重演脚本用 1~24 小时窗口去重演这些策略的仓位 —— 但它们的持仓中位只有
    13 分钟(B 更短, 期间只有 6 个快照)。用长窗口重演等于让仓位活到真实平仓之后,
    结论完全失真(实测「97% 会触及 -5% 止损」这个数字是错的: 真值显示只有 1%)。
    另外发现采集库在**持仓期间**与 freqtrade 记录的 min_rate 高度一致(70 笔仅 1 笔不符),
    所以采集数据本身可信, 问题只在重演窗口的定义上。

正确做法(全部严格限制在实际持仓区间内):
    MFE  = 持仓期间最大浮盈(权益%, 含杠杆)  —— 这段行情本来给了多少
    MAE  = 持仓期间最大浮亏(权益%)          —— 这段行情最多要求扛多少
    兑现 = 实际平仓盈亏 / 仓位(权益%)
    捕获率 = 兑现 / MFE(仅对 MFE>0 的笔统计) —— 赢面被拿走了多少
    另: 若平仓时点不动, 只是把仓位多拿 15/30/60 分钟会怎样(用真实后续价格)。
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


def to_ms(s: str) -> int:
    return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def main() -> int:
    now = datetime.now(tz=timezone.utc)
    mdb = sqlite3.connect("file:%s?mode=ro" % (ROOT / "data" / "m3dsc_market.db"), uri=True)
    print("=" * 106)
    print("单笔归因: 入场后行情给了多少 vs 实际兑现了多少")
    print("  时间: %s 北京时间 / %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H:%M UTC")))
    print("  全部指标严格限制在**实际持仓区间内**; 收益率含杠杆(权益口径)")
    print("=" * 106)

    for code, fname in EXPS:
        p = UD / fname
        if not p.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
        trades = db.execute(
            "select pair,open_date,close_date,open_rate,close_rate,leverage,stake_amount,"
            "close_profit_abs,exit_reason from trades where is_open=0 order by id").fetchall()
        recs = []
        for pair, od, cd, orate, crate, lev, stake, prof, er in trades:
            if not od or not cd or not orate or not stake:
                continue
            sym = pair.split("/")[0] + "USDT"
            t0, t1 = to_ms(od), to_ms(cd)
            path = mdb.execute(
                "select ts_ms,price from ticker_snap where symbol=? and ts_ms>=? and ts_ms<=? order by ts_ms",
                (sym, t0, t1)).fetchall()
            if len(path) < 2:
                continue
            lev = lev or 1.0
            px = [q for _, q in path] + [crate] if crate else [q for _, q in path]
            mfe = (max(px) / orate - 1.0) * lev * 100
            mae = (min(px) / orate - 1.0) * lev * 100
            realized = (prof or 0.0) / stake * 100
            hold = (t1 - t0) / 60000
            # 平仓后若继续持有
            follow = {}
            for extra in (15, 30, 60):
                frow = mdb.execute(
                    "select price from ticker_snap where symbol=? and ts_ms>=? order by ts_ms limit 1",
                    (sym, t1 + extra * 60_000)).fetchone()
                if frow and frow[0]:
                    follow[extra] = (frow[0] / orate - 1.0) * lev * 100
            recs.append({"sym": sym, "er": er, "mfe": mfe, "mae": mae, "real": realized,
                         "hold": hold, "follow": follow, "profit": prof or 0.0})
        if not recs:
            continue
        print()
        print("-" * 106)
        print("【%s】可归因 %d 笔" % (code, len(recs)))
        print("-" * 106)
        mfes = [r["mfe"] for r in recs]
        maes = [r["mae"] for r in recs]
        reals = [r["real"] for r in recs]
        print("  持仓期间最大浮盈 MFE : 中位 %+7.2f%% | 均值 %+7.2f%% | >0 的占 %.0f%%"
              % (statistics.median(mfes), sum(mfes) / len(mfes),
                 sum(1 for x in mfes if x > 0) / len(mfes) * 100))
        print("  持仓期间最大浮亏 MAE : 中位 %+7.2f%% | 均值 %+7.2f%%" % (statistics.median(maes), sum(maes) / len(maes)))
        print("  实际兑现(权益)       : 中位 %+7.2f%% | 均值 %+7.2f%% | >0 的占 %.0f%%"
              % (statistics.median(reals), sum(reals) / len(reals),
                 sum(1 for x in reals if x > 0) / len(reals) * 100))
        cap = [r["real"] / r["mfe"] for r in recs if r["mfe"] > 1.0]
        if cap:
            print("  捕获率(兑现/MFE, MFE>1%%): 中位 %.0f%% | 均值 %.0f%% |  平均让出 %.0f 个百分点"
                  % (statistics.median(cap) * 100, sum(cap) / len(cap) * 100,
                     (1 - sum(cap) / len(cap)) * 100))
        print("  让出的赢面(MFE - 兑现) 总和: %+.1f 个百分点 | 均每笔 %+.2f 个点"
              % (sum(mfes) - sum(reals), (sum(mfes) - sum(reals)) / len(recs)))
        print()
        print("  出场原因     笔数   MFE中位    MAE中位   兑现中位   持有中位")
        byer: dict[str, list[dict]] = defaultdict(list)
        for r in recs:
            byer[r["er"] or "?"].append(r)
        for k, v in sorted(byer.items(), key=lambda x: -len(x[1])):
            print("  %-18s %4d  %+8.2f%%  %+8.2f%%  %+8.2f%%  %6.0f分钟" % (
                k, len(v), statistics.median([x["mfe"] for x in v]),
                statistics.median([x["mae"] for x in v]),
                statistics.median([x["real"] for x in v]),
                statistics.median([x["hold"] for x in v])))
        print()
        print("  若平仓时点不动、只把仓位多拿一段时间(用真实后续价格, 含杠杆):")
        for extra in (15, 30, 60):
            vals = [r["follow"][extra] for r in recs if extra in r["follow"]]
            if vals:
                print("     再多拿 %2d 分钟: 合计 %+8.1f 点 | 均值 %+6.2f%% | 胜率 %.0f%% (对照 实际兑现均值 %+.2f%%)"
                      % (extra, sum(vals), sum(vals) / len(vals),
                         sum(1 for v in vals if v > 0) / len(vals) * 100, sum(reals) / len(reals)))
    print()
    print("=" * 106)
    print("读法: MFE 明显高于兑现, 说明行情给过机会但出场机制把赢面让掉了;")
    print("      MAE 很浅而兑现为负, 说明问题不在扛不住, 而在出场时点选错。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
