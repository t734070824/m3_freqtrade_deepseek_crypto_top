#!/usr/bin/env python3
"""出场后价格走势 / 最大有利偏移(MFE) 分析.

核心问题: 亏损到底来自「止损太紧(被噪声扫出)」还是「入场时机本身错误」?
判定方法(用采集器的分钟级价格快照, 与 freqtrade 交易记录按时间戳对齐):

  MFE (Maximum Favorable Excursion): 持仓期间价格向有利方向走的最远幅度
  MAE (Maximum Adverse Excursion) : 持仓期间价格向不利方向走的最远幅度
  POST_MAX                        : 平仓后 4 小时内价格能走到的最大有利幅度

判据:
  * 亏损笔的 MFE 普遍很小(<0.5%)  -> 入场即错, 问题在信号不在止损
  * 亏损笔 MFE 不小但仍被扫出      -> 止损太紧, 应放宽或改结构
  * 盈利笔的 POST_MAX 远大于实际收益 -> 离场太早, 应放宽跟踪止损
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sqlite3
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "m3dsc_market.db"


def auth() -> str:
    env: dict[str, str] = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1)
                env[k] = v
    u = os.environ.get("DSHC_FT_API_USER") or env.get("DSHC_FT_API_USER", "m3dsc")
    pw = os.environ.get("DSHC_FT_API_PASS") or env.get("DSHC_FT_API_PASS", "")
    return base64.b64encode(f"{u}:{pw}".encode()).decode()


def fmt_utc(ms: float) -> str:
    return time.strftime("%m-%d %H:%M", time.gmtime(ms / 1000))


def load_prices(conn: sqlite3.Connection) -> dict[str, list[tuple[int, float]]]:
    out: dict[str, list[tuple[int, float]]] = {}
    for r in conn.execute("SELECT symbol, ts_ms, price FROM ticker_snap "
                          "WHERE price IS NOT NULL ORDER BY symbol, ts_ms"):
        out.setdefault(r[0], []).append((int(r[1]), float(r[2])))
    return out


def window(prices: list[tuple[int, float]], t0: int, t1: int) -> list[tuple[int, float]]:
    return [(t, p) for t, p in prices if t0 <= t <= t1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--post-hours", type=float, default=4.0)
    ap.add_argument("--list", type=int, default=0)
    args = ap.parse_args()

    conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    px = load_prices(conn)
    base = os.environ.get("DSHC_FT_API", "http://127.0.0.1:18081")
    req = urllib.request.Request(f"{base}/api/v1/trades?limit=1000",
                                 headers={"Authorization": f"Basic {auth()}"})
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        data = json.loads(r.read().decode())
    trades = data.get("trades", data) if isinstance(data, dict) else data
    closed = [t for t in trades if not t.get("is_open")]
    if not closed:
        print("暂无已平仓交易"); return 0

    post_ms = int(args.post_hours * 3600_000)
    rows = []
    for t in closed:
        pair = t.get("pair", "")
        sym = pair.split("/")[0] + "USDT"
        series = px.get(sym)
        if not series:
            continue
        o, c = t.get("open_rate"), t.get("close_rate")
        if not o or not c:
            continue
        is_short = bool(t.get("is_short"))
        # 用 ticker_snap 的时间戳近似持仓区间(快照精度 1 分钟)
        t0 = None
        t1 = None
        try:
            from datetime import datetime
            t0 = int(datetime.fromisoformat(t["open_date"]).timestamp() * 1000)
            t1 = int(datetime.fromisoformat(t["close_date"]).timestamp() * 1000)
        except Exception:
            pass
        if t0 is None:
            continue
        hold = window(series, t0, t1)
        after = window(series, t1, t1 + post_ms)
        sign = -1.0 if is_short else 1.0

        def rel(p: float) -> float:
            return (p / o - 1.0) * 100.0 * sign

        mfe = max((rel(p) for _, p in hold), default=float("nan"))
        mae = min((rel(p) for _, p in hold), default=float("nan"))
        post_max = max(((p / c - 1.0) * 100.0 * sign for _, p in after),
                       default=float("nan"))
        post_min = min(((p / c - 1.0) * 100.0 * sign for _, p in after),
                       default=float("nan"))
        rows.append({
            "id": t.get("trade_id"), "pair": pair, "short": is_short,
            "open_ms": t0, "close_ms": t1,
            "pnl_pct": (t.get("profit_ratio") or 0) * 100,
            "pnl": t.get("profit_abs") or 0,
            "reason": t.get("exit_reason"), "tag": t.get("enter_tag"),
            "hold_min": (t1 - t0) / 60000.0,
            "mfe": mfe, "mae": mae, "post_max": post_max, "post_min": post_min,
        })

    if not rows:
        print("无法对齐价格数据"); return 0

    wins = [r for r in rows if r["pnl"] > 0]
    losses = [r for r in rows if r["pnl"] <= 0]
    print("=" * 84)
    print("出场后价格走势 / MFE-MAE 分析  (样本 %d 笔: 盈 %d / 亏 %d)"
          % (len(rows), len(wins), len(losses)))
    print("=" * 84)

    def agg(name: str, rs: list[dict]) -> None:
        if not rs:
            return
        mfe = [r["mfe"] for r in rs if r["mfe"] == r["mfe"]]
        mae = [r["mae"] for r in rs if r["mae"] == r["mae"]]
        pm = [r["post_max"] for r in rs if r["post_max"] == r["post_max"]]
        print("\n--- %s (%d 笔) ---" % (name, len(rs)))
        print("  持仓期最大有利偏移 MFE  中位 %+.2f%%  平均 %+.2f%%  最好 %+.2f%%"
              % (statistics.median(mfe), statistics.fmean(mfe), max(mfe)))
        print("  持仓期最大不利偏移 MAE  中位 %+.2f%%  平均 %+.2f%%  最差 %+.2f%%"
              % (statistics.median(mae), statistics.fmean(mae), min(mae)))
        if pm:
            print("  平仓后 4 小时最大有利   中位 %+.2f%%  平均 %+.2f%%"
                  % (statistics.median(pm), statistics.fmean(pm)))
        print("  平均持仓 %.0f 分钟" % statistics.fmean([r["hold_min"] for r in rs]))

    agg("全部", rows)
    agg("盈利单", wins)
    agg("亏损单", losses)

    # 关键判据
    print("\n" + "=" * 84)
    print("判据解读")
    print("=" * 84)
    if losses:
        mfe_l = [r["mfe"] for r in losses if r["mfe"] == r["mfe"]]
        small = sum(1 for x in mfe_l if x < 0.5) / len(mfe_l) * 100 if mfe_l else 0
        print("  亏损单中「MFE < 0.5%%(几乎从未浮盈)」的比例: %.0f%%" % small)
        if small >= 60:
            print("  => 入场即错: 信号在逆势位置触发, 放宽止损救不了, 必须收紧入场条件")
        else:
            print("  => 多数亏损单曾浮盈: 问题更可能在止损/离场节奏")
    if wins:
        pm = [r["post_max"] for r in wins if r["post_max"] == r["post_max"]]
        pnl = [r["pnl_pct"] for r in wins]
        if pm:
            print("  盈利单平仓后 4 小时还能多走 %.2f%%(中位), 而实际只赚 %.2f%%"
                  % (statistics.median(pm), statistics.median(pnl)))
            print("  => 差距越大说明离场越早(跟踪止损可考虑放宽或改分批)")
    if args.list:
        print("\n--- 明细(最近 %d 笔) ---" % args.list)
        print("  %-4s %-18s %-3s %8s %8s %8s %8s %-22s" %
              ("id", "标的", "方向", "收益%", "MFE", "MAE", "后4h高", "离场原因"))
        for r in rows[-args.list:]:
            print("  %-4s %-18s %-3s %+8.2f %+8.2f %+8.2f %+8.2f %-22s" % (
                r["id"], r["pair"], "空" if r["short"] else "多", r["pnl_pct"],
                r["mfe"], r["mae"], r["post_max"], r["reason"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
