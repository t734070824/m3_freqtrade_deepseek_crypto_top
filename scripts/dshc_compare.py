#!/usr/bin/env python3
"""A/B 对照实验对比: 同时拉取两个 dry-run 的账本与交易, 并排给出关键指标.

    A = 动量延续(M3GainersTrend, 端口 18081)
    B = 急跌反弹(M3DipRevert,     端口 18084)

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
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="打印最近若干笔明细")
    args = ap.parse_args()
    e = env()

    print("=" * 88)
    print("M3-DSH A/B 对照实验 (A=动量延续  B=急跌反弹)")
    print("=" * 88)
    results = {}
    for name, envkey, default in BOTS:
        port = int(os.environ.get(envkey) or e.get(envkey) or default)
        prof = fetch(port, "/api/v1/profit", e)
        tr = fetch(port, "/api/v1/trades?limit=1000", e)
        if "__error__" in prof:
            print("%-24s 不可用: %s" % (name, prof["__error__"])); continue
        trades = tr.get("trades", tr) if isinstance(tr, dict) else tr
        s = stats(trades or [])
        s["alloc"] = (prof or {}).get("starting_capital", 0) or 0
        results[name] = (s, trades or [])
        print("\n%-26s 权益 %+8.2f USDT | 交易 %3d(持仓 %d) | 胜率 %5.1f%%"
              % (name, (prof or {}).get("profit_all_coin", 0) or 0, s["n"], s["open"], s["winrate"]))
        print("%-26s 均盈 %+7.2f | 均亏 %+7.2f | 盈亏比 %5.2f | 期望/笔 %+6.3f | 利润因子 %5.2f"
              % ("", s["avg_win"], s["avg_loss"],
                 abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else float("inf"),
                 s["expectancy"], s["pf"]))
        print("%-26s 资金费 %+7.4f | 手续费 %.2f" % ("", s["funding"], s["fees"]))

    if len(results) == 2:
        print("\n" + "=" * 88)
        print("裁决(唯一标准: 期望值/笔 > 0 且 盈亏比 > 1)")
        print("=" * 88)
        for name, (s, _) in results.items():
            verdict = []
            verdict.append("期望值 %+.3f %s" % (s["expectancy"], "✔" if s["expectancy"] > 0 else "✘"))
            ratio = abs(s["avg_win"] / s["avg_loss"]) if s["avg_loss"] else 0
            verdict.append("盈亏比 %.2f %s" % (ratio, "✔" if ratio > 1 else "✘"))
            if s["n"] < 20:
                verdict.append("样本仅 %d 笔, 结论尚不可靠" % s["n"])
            print("  %-26s %s" % (name, " | ".join(verdict)))

    if args.list:
        for name, (s, trades) in results.items():
            closed = [t for t in trades if not t.get("is_open")][-12:]
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
