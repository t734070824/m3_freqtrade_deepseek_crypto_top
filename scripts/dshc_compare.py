#!/usr/bin/env python3
"""M3-DSH 多实验并行对比: 同时拉取所有 dry-run 的账本与交易, 并排给出关键指标.

四个实验共用同一采集器数据与同一 stops_core 风控内核, 因此期望值可直接比较:

    A 追涨 M3GainersTrend  动量延续(甜区)          :18081
    B 反弹 M3DipRevert     急跌反弹               :18084
    C Carry M3CarryLong    负费率长持(收资金费)     :18085
    D 突破 M3VolBreakout   波动压缩后放量突破       :18086

用法: python3 scripts/dshc_compare.py [--list]
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOTS = [
    ("A 追涨 M3GainersTrend", "DSHC_FT_API_PORT", 18081),
    ("B 反弹 M3DipRevert", "DSHC_DIP_API_PORT", 18084),
    ("C Carry M3CarryLong", "DSHC_CARRY_API_PORT", 18085),
    ("D 突破 M3VolBreakout", "DSHC_VOL_API_PORT", 18086),
    ("E 费率空 M3FundingShort", "DSHC_FSHORT_API_PORT", 18087),
]


def env() -> dict[str, str]:
    out: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                out[k] = v
    return out


def fetch(port: int, path: str, e: dict[str, str]):
    u = os.environ.get("DSHC_FT_API_USER") or e.get("DSHC_FT_API_USER", "m3dsc")
    pw = os.environ.get("DSHC_FT_API_PASS") or e.get("DSHC_FT_API_PASS", "")
    tok = base64.b64encode(f"{u}:{pw}".encode()).decode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 headers={"Authorization": f"Basic {tok}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310
            return json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        return {"__error__": str(exc)}


def stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if not t.get("is_open")]
    ps = [(t.get("profit_abs") or 0) for t in closed]
    wins = [x for x in ps if x > 0]
    losses = [x for x in ps if x <= 0]
    gp, gl = sum(wins), abs(sum(losses))
    holds = []
    for t in closed:
        try:
            from datetime import datetime
            a = datetime.fromisoformat(t["open_date"]); b = datetime.fromisoformat(t["close_date"])
            holds.append((b - a).total_seconds() / 60.0)
        except Exception:
            pass
    return {
        "n": len(closed), "open": len(trades) - len(closed),
        "total": sum(ps), "win": len(wins), "loss": len(losses),
        "winrate": (len(wins) / len(closed) * 100) if closed else 0.0,
        "avg_win": statistics.fmean(wins) if wins else 0.0,
        "avg_loss": statistics.fmean(losses) if losses else 0.0,
        "pf": (gp / gl) if gl else float("inf"),
        "expectancy": statistics.fmean(ps) if ps else 0.0,
        "funding": sum((t.get("funding_fees") or 0) for t in closed),
        "fees": sum((t.get("fee_open") or 0) + (t.get("fee_close") or 0) for t in closed),
        "hold_med": statistics.median(holds) if holds else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="打印各实验最近若干笔明细")
    args = ap.parse_args()
    e = env()

    print("=" * 96)
    print("M3-DSH 多实验对比 (A 追涨 / B 反弹 / C Carry / D 突破 / E 费率空)")
    print("=" * 96)
    results = {}
    for name, envkey, default in BOTS:
        port = int(os.environ.get(envkey) or e.get(envkey) or default)
        prof = fetch(port, "/api/v1/profit", e)
        tr = fetch(port, "/api/v1/trades?limit=1000", e)
        if "__error__" in prof:
            print("\n%-24s [不可用] %s" % (name, prof["__error__"][:50]))
            continue
        trades = tr.get("trades", tr) if isinstance(tr, dict) else tr
        s = stats(trades or [])
        results[name] = (s, trades or [])
        print("\n%-26s 权益 %+8.2f | 交易 %3d(持仓 %d) | 胜率 %5.1f%% | 持仓中位 %.0f 分钟"
              % (name, (prof or {}).get("profit_all_coin", 0) or 0, s["n"], s["open"],
                 s["winrate"], s["hold_med"]))
        print("%-26s 均盈 %+7.2f | 均亏 %+7.2f | 盈亏比 %5.2f | 期望/笔 %+7.3f | PF %5.2f | 资金费 %+.4f"
              % ("", s["avg_win"], s["avg_loss"],
                 abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else float("inf"),
                 s["expectancy"], s["pf"], s["funding"]))

    print("\n" + "=" * 96)
    print("裁决(唯一判据: 期望值/笔 > 0 且 盈亏比 > 1; 样本 < 20 笔不下结论)")
    print("=" * 96)
    ranking = sorted(results.items(), key=lambda kv: -kv[1][0]["expectancy"])
    for name, (s, _) in ranking:
        ok = s["expectancy"] > 0 and (s["avg_loss"] == 0 or abs(s["avg_win"] / s["avg_loss"]) > 1)
        verdict = "✔ 达标" if ok else "✘ 未达标"
        note = "样本不足(%d<20)" % s["n"] if s["n"] < 20 else ""
        print("  %-26s 期望 %+7.3f  盈亏比 %5.2f  %s  %s"
              % (name, s["expectancy"],
                 abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else float("inf"),
                 verdict, note))

    if args.list:
        for name, (s, trades) in results.items():
            closed = [t for t in trades if not t.get("is_open")][-10:]
            if not closed:
                continue
            print("\n--- %s 最近 %d 笔 ---" % (name, len(closed)))
            print("  %-5s %-20s %-3s %8s %9s %9s %-22s %s" % (
                "id", "标的", "向", "收益%", "盈亏", "资金费", "离场原因", "入场信号"))
            for t in closed:
                print("  %-5s %-20s %-3s %+8.2f %+9.2f %+9.4f %-22s %s" % (
                    t.get("trade_id"), t.get("pair", ""),
                    "空" if t.get("is_short") else "多", (t.get("profit_ratio") or 0) * 100,
                    t.get("profit_abs") or 0, t.get("funding_fees") or 0,
                    t.get("exit_reason") or "", t.get("enter_tag") or ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
