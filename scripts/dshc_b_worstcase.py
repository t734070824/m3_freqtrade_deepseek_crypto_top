#!/usr/bin/env python3
"""B 出场几何 —— 用 freqtrade 记录的真实盘中极值(min_rate/max_rate)做最坏情况判定.

为什么不用分钟快照(自证的偏差):
    逐分钟回放看不到分钟内极值, 止损越紧被高估越多。实测基线均亏 -6.29% vs 账本 -9.16%,
    回放把损失低估了约 1.46 倍 —— 紧止损的档位最不可信。

本脚本改用**地面真值**, 不做任何顺序假设:
    每笔交易 freqtrade 都记录了 min_rate(盘中最低) 与 max_rate(盘中最高)。
    * 最坏情况: 只要 min_rate 触及止损位, 就算**亏损**(不看止盈是否先到);
    * 盈利: 仅当 min_rate 没碰止损 **且** max_rate 达到止盈位。
    这是**下界**(把每一笔可能的止损都算成亏损), 因此结果偏保守 —— 若保守下界仍为正, 才可信。
"""
from __future__ import annotations
import sqlite3, statistics, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UTC, CST = timezone.utc, timezone(timedelta(hours=8))
LEDGERS = [("归档", ROOT / "freqtrade/user_data/archive/m3dsc-dip.sqlite.20260914-1454Z"),
           ("当前", ROOT / "freqtrade/user_data/m3dsc-dip.sqlite")]
COST = 0.10  # 单边成本 pct(价格口径)


def load():
    out = []
    for tag, lg in LEDGERS:
        if not lg.exists():
            continue
        db = sqlite3.connect("file:%s?mode=ro" % lg, uri=True)
        for pair, orate, mn, mx, lev, stake, prof, er in db.execute(
                "select pair,open_rate,min_rate,max_rate,leverage,stake_amount,close_profit_abs,exit_reason "
                "from trades where is_open=0"):
            if not orate or not mn or not mx or not stake:
                continue
            out.append({"pair": pair, "orate": orate, "min": mn, "max": mx,
                        "lev": lev or 1.0, "real": (prof or 0) / stake, "er": er, "src": tag})
    return out


def main():
    ents = load()
    now = datetime.now(tz=UTC)
    print("=" * 100)
    print("B 出场几何 —— 最坏情况判定(用 freqtrade 真实盘中极值)")
    print("  时间: %s 北京时间 / %s UTC" % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M"),
                                            now.strftime("%Y-%m-%d %H:%M")))
    print("  样本 %d 笔 (归档 %d + 当前 %d)"
          % (len(ents), sum(1 for e in ents if e["src"] == "归档"), sum(1 for e in ents if e["src"] == "当前")))
    print("=" * 100)
    print()
    print("  规则: min_rate 触及止损位 -> 计入亏损(不看止盈先后); 仅当没碰止损且 max_rate 达止盈 -> 计入盈利")
    print()
    print("  %-22s %8s %9s %10s %10s %11s %9s" % ("配置", "笔数", "胜率%", "均盈%", "均亏%", "期望/笔%", "盈亏比"))
    print("  " + "-" * 92)

    refs = [("现状(不设止损上限)", None, None)]
    grid = []
    for cap in (0.015, 0.020, 0.025):
        for tp in (0.030, 0.045, 0.060):
            grid.append(("止损上限%.1f%%+止盈%.1f%%" % (cap * 100, tp * 100), cap, tp))
    for label, cap, tp in refs + grid:
        vals = []
        for e in ents:
            lev, orate = e["lev"], e["orate"]
            stop_px = orate * (1 - (min(cap, 1.0) if cap else 1.0))
            tp_eq = tp if tp is not None else 0.030
            stop_hit = e["min"] <= stop_px if cap else False
            if stop_hit:
                vals.append(-(cap * lev) - COST / 100.0 * lev)
                continue
            tp_px = orate * (1 + tp_eq / lev)
            if e["max"] >= tp_px:
                vals.append(tp_eq - COST / 100.0 * lev)
            else:
                # 未达止盈也未碰止损: 按实际结果的方向取保守值(亏损按实际, 盈利按 0)
                vals.append(min(0.0, e["real"]))
        w = [v for v in vals if v > 0]
        l = [v for v in vals if v <= 0]
        aw = statistics.mean(w) if w else 0.0
        al = statistics.mean(l) if l else 0.0
        print("  %-22s %8d %9.1f %+10.2f %+10.2f %+11.3f %9.2f" % (
            label, len(vals), len(w) / len(vals) * 100, aw * 100, al * 100,
            statistics.mean(vals) * 100, abs(aw / al) if al else float("inf")))
    print()
    print("  对照: 账本实际的期望 = %+.3f%%/笔" % (statistics.mean([e["real"] for e in ents]) * 100))
    print()
    print("  >>> 这一列是**下界**: 把所有可能的止损都当成亏损。若仍为正, 该配置就值得上线。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
