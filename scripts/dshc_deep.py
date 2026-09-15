#!/usr/bin/env python3
"""M3-DSH 全实验深度分析: 直接读 SQLite 真实账本, 不依赖 REST API.

为什么直接读库:
    REST 的 /profit 把「已实现」与「浮动」混在一起, 且各实验端口会随实验上下线而变动,
    容易出现串号(实测过一次: E 退役后端口给了 G, 报告里 E 与 G 数字完全一样)。
    直接读各实验自己的 sqlite 账本, 才是不被污染的唯一真相。

每笔交易的维度:
    收益率(权益) = close_profit_abs / stake_amount
    持仓时长, 出场原因, 入场标签, 杠杆, 资金费, 手续费
汇总维度:
    已实现盈亏 / 胜率 / 期望值每笔 / 盈亏比 / 盈利因子 / 最大回撤(按平仓序列)
    集中度: 前 1 笔与前 3 笔分别占已实现盈亏的比例; 逐标的盈亏
    资金费归因: 资金费合计占已实现盈亏的比例
"""
from __future__ import annotations

import json
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UD = ROOT / "freqtrade" / "user_data"
CST = timezone(timedelta(hours=8))

EXPS = [
    ("G", "B+F融合 M3CarryDipTurbo", "m3dsc-g.sqlite"),
    ("F", "负费率+急跌 M3CarryDip", "m3dsc-f.sqlite"),
    ("B", "急跌反弹 M3DipRevert", "m3dsc-dip.sqlite"),
    ("C", "负费率长持 M3CarryLong", "m3dsc-carry.sqlite"),
    ("H", "正费率+急跌 M3DipTrend", "m3dsc-h.sqlite"),
]


def cst(s: str | None) -> str:
    if not s:
        return "-"
    try:
        d = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
        return d.astimezone(CST).strftime("%m-%d %H:%M")
    except Exception:  # noqa: BLE001
        return "-"


def max_dd(seq: list[float]) -> float:
    peak = run = 0.0
    dd = 0.0
    for x in seq:
        run += x
        peak = max(peak, run)
        dd = min(dd, run - peak)
    return dd


def analyse(code: str, label: str, fname: str) -> dict:
    p = UD / fname
    if not p.exists():
        return {"code": code, "label": label, "missing": True}
    db = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    rows = db.execute(
        "select id,pair,is_open,open_date,close_date,close_profit_abs,exit_reason,"
        "leverage,stake_amount,funding_fees,fee_open_cost,fee_close_cost,enter_tag,"
        "amount,open_rate,close_rate,is_short,max_rate,min_rate "
        "from trades order by id").fetchall()
    cols = ["id", "pair", "is_open", "open_date", "close_date", "profit", "exit_reason",
            "leverage", "stake", "funding", "fee_open", "fee_close", "tag",
            "amount", "open_rate", "close_rate", "is_short", "max_rate", "min_rate"]
    T = [dict(zip(cols, r)) for r in rows]
    closed = [t for t in T if not t["is_open"]]
    open_ = [t for t in T if t["is_open"]]
    out: dict = {"code": code, "label": label, "fname": fname,
                 "n_closed": len(closed), "n_open": len(open_),
                 "ledger_mtime": datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
                 .astimezone(CST).strftime("%m-%d %H:%M")}
    if not closed:
        out["open_positions"] = open_
        return out
    prof = [t["profit"] or 0.0 for t in closed]
    wins = [x for x in prof if x > 0]
    loss = [x for x in prof if x <= 0]
    out.update({
        "realized": sum(prof),
        "win_rate": len(wins) / len(prof) * 100,
        "avg_win": sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(loss) / len(loss) if loss else 0.0,
        "payoff": (abs((sum(wins) / len(wins)) / (sum(loss) / len(loss)))
                   if wins and loss and sum(loss) else float("inf")),
        "expectancy": sum(prof) / len(prof),
        "pf": (sum(wins) / abs(sum(loss))) if loss and sum(loss) else float("inf"),
        "max_dd": max_dd(prof),
        "funding": sum(t["funding"] or 0.0 for t in closed),
        "fees": sum((t["fee_open"] or 0) + (t["fee_close"] or 0) for t in closed),
        "best": max(prof), "worst": min(prof),
        "open_positions": open_,
    })
    # 收益率(权益)口径
    rets = [(t["profit"] or 0.0) / (t["stake"] or 1.0) * 100 for t in closed]
    out["ret_mean"] = sum(rets) / len(rets)
    out["ret_median"] = statistics.median(rets)
    # 持仓时长
    durs = []
    for t in closed:
        if t["close_date"] and t["open_date"]:
            durs.append((datetime.fromisoformat(t["close_date"])
                         - datetime.fromisoformat(t["open_date"])).total_seconds() / 60)
    if durs:
        out["dur_median"] = statistics.median(durs)
        out["dur_max"] = max(durs)
    # 出场原因
    by_exit: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        by_exit[t["exit_reason"] or "?"].append(t["profit"] or 0.0)
    out["by_exit"] = {k: (len(v), sum(v), sum(v) / len(v)) for k, v in by_exit.items()}
    # 集中度
    srt = sorted(prof, reverse=True)
    tot = sum(prof)
    out["top1_share"] = (srt[0] / tot * 100) if tot else 0.0
    out["top3_share"] = (sum(srt[:3]) / tot * 100) if tot else 0.0
    # 逐标的
    by_pair: dict[str, list[float]] = defaultdict(list)
    for t in closed:
        by_pair[t["pair"].split("/")[0]].append(t["profit"] or 0.0)
    out["by_pair"] = dict(sorted(((k, (len(v), sum(v))) for k, v in by_pair.items()),
                                 key=lambda x: -x[1][1]))
    out["n_pairs"] = len(by_pair)
    # 杠杆分布
    levs: dict[float, list[float]] = defaultdict(list)
    for t in closed:
        levs[t["leverage"] or 1.0].append(t["profit"] or 0.0)
    out["by_lev"] = {k: (len(v), sum(v), sum(v) / len(v)) for k, v in sorted(levs.items())}
    return out


def main() -> int:
    now = datetime.now(tz=timezone.utc)
    print("=" * 104)
    print("M3-DSH 全实验深度分析 (直接读 SQLite 真实账本)")
    print("  生成时间: %s 北京时间(UTC+8) | %s UTC"
          % (now.astimezone(CST).strftime("%Y-%m-%d %H:%M:%S"), now.strftime("%Y-%m-%d %H:%M:%S")))
    print("=" * 104)
    res = [analyse(*e) for e in EXPS]

    print()
    print("【一】总览")
    print("-" * 104)
    print("%-3s %-22s %6s %6s %10s %8s %8s %8s %8s %8s" % (
        "", "实验", "已平", "持仓", "已实现", "胜率%", "期望/笔", "盈亏比", "PF", "回撤"))
    for r in res:
        if r.get("missing") or not r.get("n_closed"):
            print("%-3s %-22s %6d %6d %10s" % (r["code"], r["label"], r.get("n_closed", 0),
                                               r.get("n_open", 0), "样本不足"))
            continue
        print("%-3s %-22s %6d %6d %+10.2f %8.1f %+8.3f %8.2f %8.2f %+8.2f" % (
            r["code"], r["label"], r["n_closed"], r["n_open"], r["realized"],
            r["win_rate"], r["expectancy"], r["payoff"], r["pf"], r["max_dd"]))

    for r in res:
        if r.get("missing") or not r.get("n_closed"):
            continue
        print()
        print("=" * 104)
        print("【%s】%s   (账本 %s, 更新于 %s 北京时间)" % (r["code"], r["label"], r["fname"], r["ledger_mtime"]))
        print("=" * 104)
        print("  已实现 %+.2f | 最大回撤 %.2f | 单笔最好 %+.2f / 最差 %+.2f | 标的数 %d"
              % (r["realized"], r["max_dd"], r["best"], r["worst"], r["n_pairs"]))
        print("  收益率(权益)口径: 均值 %+.3f%% | 中位数 %+.3f%%" % (r["ret_mean"], r["ret_median"]))
        if "dur_median" in r:
            print("  持仓: 中位 %.0f 分钟 | 最长 %.0f 分钟" % (r["dur_median"], r["dur_max"]))
        print("  集中度: 最大单笔占已实现 %.0f%% | 前 3 笔占 %.0f%%" % (r["top1_share"], r["top3_share"]))
        print("  资金费合计 %+.4f (占已实现 %.1f%%) | 手续费合计 %.4f"
              % (r["funding"], (r["funding"] / r["realized"] * 100) if r["realized"] else 0.0, r["fees"]))
        print("  出场原因分布:")
        for k, (n, tot, avg) in sorted(r["by_exit"].items(), key=lambda x: -x[1][0]):
            print("     %-24s %3d 笔  合计 %+8.2f  均 %+7.2f" % (k, n, tot, avg))
        print("  杠杆分布:")
        for k, (n, tot, avg) in r["by_lev"].items():
            print("     %.0fx  %3d 笔  合计 %+8.2f  均 %+7.2f" % (k, n, tot, avg))
        print("  逐标的(前 8):")
        for k, (n, tot) in list(r["by_pair"].items())[:8]:
            print("     %-14s %3d 笔  合计 %+8.2f" % (k, n, tot))
        if r["open_positions"]:
            print("  当前持仓:")
            for t in r["open_positions"]:
                print("     %-16s %s %s  开于 %s 北京时间  杠杆 %.0fx  仓位 %.2f"
                      % (t["pair"].split("/")[0], "空" if t["is_short"] else "多",
                         "%.6g" % (t["open_rate"] or 0), cst(t["open_date"]),
                         t["leverage"] or 1, t["stake"] or 0))
    print()
    print("=" * 104)
    print("判据(全项目唯一): 期望值/笔 > 0 且 盈亏比 > 1; 样本 < 20 笔不下结论。")
    print("所有金额为 USDT, 各实验初始资金 500(除注明), 互不通用。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
