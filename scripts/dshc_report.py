"""M3-DSH 运维报告工具 (供 shell 脚本调用, 免去内联 python 的引号地狱).

用法:
    dshc_report.py status   <freqtrade_status.json>
    dshc_report.py profit   <freqtrade_profit.json>
    dshc_report.py watchlist <watchlist.json>
    dshc_report.py collectors <collector_status.json>
    dshc_report.py tables   <m3dsc_market.db>
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from typing import Any


def _load(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_status(path: str) -> None:
    d = _load(path)
    print("  当前持仓 %d 笔" % len(d))
    for t in d:
        side = "空" if t.get("is_short") else "多"
        # 注意: freqtrade REST API 的 profit_pct 已是百分数(例如 -2.45 表示 -2.45%),
        # 不要再乘 100。profit_ratio 则是「已含杠杆」的收益率小数, 二者关系: pct = ratio*100。
        print("    %-24s %s  %.6g -> %.6g  %+.2f%%  (%+.2f USDT)  x%s" % (
            t.get("pair", ""), side, t.get("open_rate", 0), t.get("current_rate", 0),
            t.get("profit_pct", 0) or 0, t.get("profit_abs", 0) or 0,
            t.get("leverage", 1)))
        for o in (t.get("orders") or []):
            print("       单: %-10s %-8s %.6g x %s  %s" % (
                o.get("ft_order_side", ""), o.get("status", ""), o.get("safe_price", 0) or 0,
                o.get("safe_amount", 0) or 0, o.get("order_date", "")))


def cmd_profit(path: str) -> None:
    d = _load(path)
    print("  账户总盈亏: %+.2f USDT   已平仓: %+.2f USDT   未平仓: %+.2f USDT" % (
        d.get("profit_all_coin", 0) or 0, d.get("profit_closed_coin", 0) or 0,
        (d.get("profit_all_coin", 0) or 0) - (d.get("profit_closed_coin", 0) or 0)))
    print("  交易数: %s (已平仓 %s)   胜率: %.1f%%   平均持仓: %s" % (
        d.get("trade_count", 0), d.get("closed_trade_count", 0),
        (d.get("winrate", 0) or 0) * 100, d.get("avg_duration", "n/a")))
    print("  最佳单笔: %+.2f  最差单笔: %+.2f" % (
        d.get("best_trade_coin", 0) or 0, d.get("worst_trade_coin", 0) or 0))


def cmd_watchlist(path: str) -> None:
    d = _load(path)
    age = (time.time() * 1000 - d.get("generated_ms", 0)) / 1000
    print("  生成于 %s (距今 %.0fs); 可交易合约 %s; 恐贪 %s; 新闻情绪 %s" % (
        d.get("generated_cst", "?"), age, d.get("n_tradable", "?"),
        d.get("macro", {}).get("fear_greed", "n/a"),
        d.get("macro", {}).get("news_sentiment", "n/a")))
    print("  %-3s %-16s %8s %9s %10s %9s %8s %6s %s" % (
        "#", "合约", "得分", "24h%", "年化费率", "成交额M", "OI1h%", "LS比", "标签"))
    for i, c in enumerate(d.get("candidates", [])[:20], 1):
        print("  %-3d %-16s %8.1f %8.2f%% %9.1f%% %9.0f %8.2f %6.2f %s" % (
            i, c.get("symbol", ""), c.get("score", 0), c.get("change_24h", 0),
            (c.get("funding_ann", 0) or 0) * 100, (c.get("quote_vol", 0) or 0) / 1e6,
            c.get("oi_chg_1h", 0) or 0, c.get("ls_ratio", 0) or 0,
            " ".join(c.get("tags", []))))


def cmd_collectors(path: str) -> None:
    d = _load(path)
    print("  运行 %.1f 分钟   HTTP 统计: %s" % (d.get("uptime_min", 0), d.get("http", {})))
    now = time.time() * 1000
    for k, v in sorted(d.get("workers", {}).items()):
        age = (now - (v.get("last_ok_ms") or 0)) / 1000
        ok = age < max(600, v["interval"] * 4)
        print("  %s %-12s 周期%5ds 运行%5d 错误%3d 行%7d 距今%6.0fs %s" % (
            "OK " if ok else "!! ", k, v["interval"], v["runs"], v["errors"], v["rows"], age,
            (v.get("last_error") or "")[:60]))


def cmd_tables(path: str) -> None:
    c = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
    names = [r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in names:
        n = c.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        print("  %-18s %8d 行" % (t, n))


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    fn = {"status": cmd_status, "profit": cmd_profit, "watchlist": cmd_watchlist,
          "collectors": cmd_collectors, "tables": cmd_tables}.get(argv[1])
    if fn is None:
        print("未知命令:", argv[1])
        return 2
    fn(argv[2])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
