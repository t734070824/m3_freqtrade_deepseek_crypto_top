"""M3-DSH 监控看板 (容器 m3dsc-dashboard).

数据来源:
  * /workspace/data/m3dsc_market.db        : 采集器时序库 (只读)
  * /workspace/data/live/watchlist.json    : 当前候选池打分
  * http://m3dsc-freqtrade-dryrun:8080     : freqtrade REST API (dry-run 账户/持仓)

时间展示: 同时给出「北京时间(UTC+8)」与「UTC」, 避免歧义。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request

from .config import SETTINGS
from .timeutil import CST, UTC, fmt, fmt_both, fmt_cn, ms_to_dt, utc_ms

log = logging.getLogger("dshc.dashboard")
app = Flask("m3dsc-dashboard")

DATA = SETTINGS.data_dir
DB = SETTINGS.db_path
LIVE = DATA / "live"


# ------------------------------------------------------------------ 工具
def db() -> sqlite3.Connection | None:
    if not DB.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=15,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c
    except sqlite3.Error:
        return None


def wl() -> dict[str, Any]:
    try:
        return json.loads((LIVE / "watchlist.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _hx(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _ft_api(path: str) -> dict[str, Any] | None:
    """调用 freqtrade REST API."""
    import base64
    import urllib.request
    url = os.environ.get("DSHC_FT_API_URL", "http://m3dsc-freqtrade-dryrun:8080") + path
    user = os.environ.get("DSHC_FT_API_USER", "m3dsc")
    pw = os.environ.get("DSHC_FT_API_PASS", "change_me_m3dsc")
    token = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(req, timeout=6) as r:  # noqa: S310
            return json.loads(r.read().decode())
    except Exception:  # noqa: BLE001
        return None


# ------------------------------------------------------------------ 页面
@app.get("/")
def index() -> str:
    payload = wl()
    now = utc_ms()
    conn = db()
    cands = payload.get("candidates", [])
    rows_html = []
    for i, c in enumerate(cands[:60], 1):
        score = c.get("score", 0)
        cls = "pos" if score > 0 else "neg"
        ann = c.get("funding_ann", 0) * 100
        rows_html.append(f"""<tr>
<td>{i}</td><td><b>{_hx(c.get('symbol',''))}</b></td>
<td class="{cls}">{score:+.1f}</td>
<td class="{'up' if c.get('change_24h',0)>0 else 'dn'}">{c.get('change_24h',0):+.2f}%</td>
<td>{c.get('price',0):,.6g}</td>
<td>{c.get('quote_vol',0)/1e6:,.0f}M</td>
<td class="{'dn' if ann>0 else 'up'}">{ann:+.1f}%</td>
<td>{c.get('oi_chg_1h',0):+.2f}%</td>
<td>{c.get('ls_ratio',0):.2f}</td>
<td>{c.get('taker_ratio',1):.2f}</td>
<td>{c.get('spread_bps',0):.1f}</td>
<td>{c.get('rank_gain',0)}</td>
<td>{' '.join(c.get('tags',[]))}</td>
</tr>""")

    gainers = sorted([c for c in cands if c.get("quote_vol", 0) >= 8e6],
                     key=lambda x: x.get("change_24h", 0), reverse=True)[:20]
    g_parts = []
    for i, g in enumerate(gainers, 1):
        ann = (g.get("funding_ann") or 0) * 100
        cls = "dn" if ann > 0 else "up"
        g_parts.append(
            f"<tr><td>{i}</td><td><b>{_hx(g.get('symbol',''))}</b></td>"
            f"<td class='up'>{g.get('change_24h',0):+.2f}%</td>"
            f"<td>{g.get('quote_vol',0)/1e6:,.0f}M</td>"
            f"<td class='{cls}'>{ann:+.1f}%</td>"
            f"<td>{g.get('score',0):+.1f}</td>"
            f"<td>{' '.join(g.get('tags',[]))}</td></tr>")
    g_html = "".join(g_parts)

    # ---- 收益曲线(按日) ----
    daily_rows = ""
    try:
        dl = _ft_api("/api/v1/daily?timescale=30") or {}
        data = (dl.get("data") or []) if isinstance(dl, dict) else []
        dp_html = []
        for d in data[-14:][::-1]:
            pa = d.get("abs_profit") or 0.0
            pr = (d.get("rel_profit") or 0.0) * 100
            ff = d.get("funding_fees") or 0.0
            dp_html.append(
                f"<tr><td>{_hx(str(d.get('date','')))}</td>"
                f"<td>{d.get('trade_count',0)}</td>"
                f"<td class='{'up' if pa>0 else 'dn'}'>{pa:+.2f}</td>"
                f"<td class='{'up' if pr>0 else 'dn'}'>{pr:+.2f}%</td>"
                f"<td class='{'up' if ff>0 else 'dn'}'>{ff:+.4f}</td></tr>")
        daily_rows = "".join(dp_html)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取按日收益失败: %s", exc)

    # ---- 已平仓交易 ----
    closed_rows = ""
    closed_sum = 0.0
    closed_funding = 0.0
    try:
        tr = _ft_api("/api/v1/trades?limit=200") or {}
        tlist = tr.get("trades", tr) if isinstance(tr, dict) else tr
        closed = [t for t in (tlist or []) if not t.get("is_open")]
        closed = closed[-20:][::-1]
        parts = []
        for t in closed:
            pr = t.get("profit_ratio") or 0.0
            pr_pct = pr * 100
            pa = t.get("profit_abs") or 0.0
            ff = t.get("funding_fees") or 0.0
            closed_sum += pa
            closed_funding += ff
            dur = _duration_min(t.get("open_date"), t.get("close_date"))
            parts.append(
                f"<tr><td>{_hx(t.get('pair',''))}</td>"
                f"<td>{'空' if t.get('is_short') else '多'}</td>"
                f"<td>{_hx(str(t.get('open_date',''))[:19])}</td>"
                f"<td>{_hx(str(t.get('close_date',''))[:19])}</td>"
                f"<td>{(t.get('open_rate') or 0):,.6g}</td>"
                f"<td>{(t.get('close_rate') or 0):,.6g}</td>"
                f"<td class='{'up' if pr_pct>0 else 'dn'}'>{pr_pct:+.2f}%</td>"
                f"<td class='{'up' if pa>0 else 'dn'}'>{pa:+.2f}</td>"
                f"<td class='{'up' if ff>0 else 'dn'}'>{ff:+.4f}</td>"
                f"<td>{'' if dur is None else f'{dur:.0f}'}</td>"
                f"<td>{_hx(t.get('exit_reason','') or '')}</td>"
                f"<td>{_hx(t.get('enter_tag','') or '')}</td></tr>")
        closed_rows = "".join(parts)
    except Exception as exc:  # noqa: BLE001
        log.debug("读取已平仓交易失败: %s", exc)

    # 采集健康
    health = []
    if conn:
        for r in conn.execute("SELECT * FROM collector_status ORDER BY collector"):
            age = (now - (r["last_ok_ms"] or 0)) / 1000
            ok = age < max(600, (r["rows_last"] or 0) * 0 + 600)
            health.append(
                f"<tr><td>{_hx(r['collector'])}</td><td>{r['runs']}</td><td>{r['errors']}</td>"
                f"<td>{r['rows_last']}</td><td class='{'up' if ok else 'dn'}'>"
                f"{fmt_both(r['last_ok_ms'] or 0)}</td>"
                f"<td>{'<span class=err>' + _hx((r['last_error'] or '')[:60]) + '</span>' if r['last_error'] else 'OK'}</td></tr>")
    h_html = "".join(health)

    macro = payload.get("macro", {})
    fng = macro.get("fear_greed", "n/a")
    news = macro.get("news_sentiment", "n/a")

    ft_status = _ft_api("/api/v1/status") or []
    ft_profit = _ft_api("/api/v1/profit") or {}
    ft_balance = _ft_api("/api/v1/balance") or {}
    trade_rows = "".join(
        f"<tr><td>{_hx(t.get('pair',''))}</td>"
        f"<td>{'空' if t.get('is_short') else '多'}</td>"
        f"<td>{t.get('open_date','')}</td>"
        f"<td>{t.get('amount',0)}</td><td>{t.get('open_rate',0):,.6g}</td>"
        f"<td>{t.get('current_rate',0):,.6g}</td>"
        f"<td class='{'up' if (t.get('profit_pct') or 0)>0 else 'dn'}'>"
        f"{(t.get('profit_pct') or 0):+.2f}%</td>"
        f"<td>{(t.get('profit_abs') or 0):+.2f}</td>"
        f"<td>{t.get('leverage',1)}</td><td>{_hx(t.get('enter_tag','') or '')}</td></tr>"
        for t in (ft_status if isinstance(ft_status, list) else []))
    prof = ft_profit.get("profit_all_coin", ft_profit.get("profit_closed_coin", 0)) or 0
    prof_pct = (ft_profit.get("profit_all_percent", 0) or 0) * 100
    bal = ft_balance.get("total", 0) if isinstance(ft_balance, dict) else 0
    n_open = len(ft_status) if isinstance(ft_status, list) else 0

    age = (now - payload.get("generated_ms", 0)) / 1000 if payload else -1

    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>M3-DSH 看板 · 涨幅榜合约趋势系统</title>
<style>
:root {{ color-scheme: dark; }}
body {{ background:#0d1117; color:#c9d1d9; font-family: -apple-system,"PingFang SC","Microsoft YaHei",monospace; margin:0; padding:16px; }}
h1 {{ font-size:18px; margin:0 0 4px; color:#58a6ff; }}
h2 {{ font-size:14px; margin:18px 0 6px; color:#79c0ff; border-left:3px solid #1f6feb; padding-left:8px; }}
table {{ border-collapse:collapse; width:100%; font-size:12px; }}
th,td {{ border-bottom:1px solid #21262d; padding:3px 6px; text-align:right; white-space:nowrap; }}
th {{ color:#8b949e; font-weight:500; text-align:right; position:sticky; top:0; background:#0d1117; }}
td:nth-child(2), td:last-child {{ text-align:left; }}
.up {{ color:#3fb950; }} .dn {{ color:#f85149; }}
.pos {{ color:#3fb950; font-weight:600; }} .neg {{ color:#f85149; font-weight:600; }}
.cards {{ display:flex; gap:12px; flex-wrap:wrap; margin:10px 0; }}
.card {{ background:#161b22; border:1px solid #30363d; border-radius:6px; padding:8px 14px; min-width:130px; }}
.card .k {{ font-size:11px; color:#8b949e; }} .card .v {{ font-size:17px; color:#e6edf3; }}
.err {{ color:#f85149; font-size:11px; }}
.tag {{ color:#d29922; font-size:10px; }}
.note {{ color:#8b949e; font-size:11px; margin-top:10px; line-height:1.6; }}
</style></head><body>
<h1>M3-DSH · 涨幅榜合约趋势系统 <span style="color:#8b949e;font-size:12px">(dry-run)</span></h1>
<div class="note">页面刷新: 每 60 秒 · 候选池生成于 {fmt_cn(payload.get('generated_ms',0)) if payload else 'n/a'}
&nbsp;|&nbsp; {fmt_both(payload.get('generated_ms',0)) if payload else ''} (距今 {age:.0f}s)</div>

<div class="cards">
  <div class="card"><div class="k">dry-run 总权益</div><div class="v">{bal:,.2f} USDT</div></div>
  <div class="card"><div class="k">累计盈亏</div><div class="v {'up' if prof>0 else 'dn'}">{prof:+.2f} ({prof_pct:+.2f}%)</div></div>
  <div class="card"><div class="k">当前持仓</div><div class="v">{n_open}</div></div>
  <div class="card"><div class="k">候选池</div><div class="v">{len(cands)}</div></div>
  <div class="card"><div class="k">可交易合约</div><div class="v">{payload.get('n_tradable','n/a')}</div></div>
  <div class="card"><div class="k">恐贪指数</div><div class="v">{fng}</div></div>
  <div class="card"><div class="k">新闻情绪</div><div class="v">{news}</div></div>
</div>

<h2>当前持仓 (freqtrade dry-run)</h2>
<table><tr><th>标的</th><th>方向</th><th>开仓时间(UTC)</th><th>数量</th><th>开仓价</th><th>现价</th><th>收益率</th><th>盈亏USDT</th><th>杠杆</th><th>信号</th></tr>
{trade_rows or '<tr><td colspan=10>暂无持仓</td></tr>'}</table>

<h2>收益曲线 (按日/按周)</h2>
<table><tr><th>周期</th><th>交易数</th><th>盈亏USDT</th><th>收益率</th><th>资金费</th></tr>
{daily_rows or '<tr><td colspan=5>暂无数据</td></tr>'}</table>

<h2>已平仓交易 (最近 20 笔)</h2>
<table><tr><th>标的</th><th>方向</th><th>开仓(UTC)</th><th>平仓(UTC)</th><th>开仓价</th><th>平仓价</th>
<th>收益率</th><th>盈亏USDT</th><th>资金费</th><th>持仓分钟</th><th>离场原因</th><th>信号</th></tr>
{closed_rows or '<tr><td colspan=12>暂无已平仓交易</td></tr>'}</table>
<div class="note">已平仓合计: <b>{closed_sum:+.2f} USDT</b> · 资金费合计: <b>{closed_funding:+.2f} USDT</b>
(资金费为正表示「收到」补贴, 为负表示「付出」成本)</div>

<h2>候选池打分 (score&gt;0 利多 / score&lt;0 利空)</h2>
<table><tr><th>#</th><th>合约</th><th>M3得分</th><th>24h涨幅</th><th>价格</th><th>24h额</th>
<th>资金费率年化</th><th>OI 1h</th><th>大户多空比</th><th>Taker</th><th>价差bps</th><th>涨幅榜名次</th><th>标签</th></tr>
{''.join(rows_html) or '<tr><td colspan=13>等待采集器产出数据 ...</td></tr>'}</table>

<h2>涨幅榜 TOP20 (流动性过滤后)</h2>
<table><tr><th>#</th><th>合约</th><th>24h涨幅</th><th>24h成交额</th><th>资金费率年化</th><th>M3得分</th><th>标签</th></tr>
{g_html or '<tr><td colspan=7>n/a</td></tr>'}</table>

<h2>采集器健康</h2>
<table><tr><th>采集器</th><th>运行轮次</th><th>错误</th><th>最近行数</th><th>最近成功(北京时间 / UTC)</th><th>状态</th></tr>
{h_html or '<tr><td colspan=6>n/a</td></tr>'}</table>

<div class="note">
时间约定: 本页所有时间均显式标注时区 —— 北京时间(UTC+8) 与 UTC。<br>
资金费率年化 = 当期费率 × (24/结算周期) × 365, 正数表示多头付给空头(持仓成本), 负数表示空头付给多头(持仓补贴)。<br>
数据源: Binance USD-M 公共接口 (ticker/24hr, premiumIndex, openInterest, longShortRatio, bookTicker, basis) + alternative.me 恐贪指数 + 公开 RSS 新闻。
</div>
</body></html>"""


# ------------------------------------------------------------------ API
@app.get("/api/summary")
def api_summary() -> Response:
    p = wl()
    conn = db()
    out: dict[str, Any] = {
        "now_ms": utc_ms(),
        "now_cst": fmt_cn(utc_ms()),
        "now_utc": fmt(utc_ms()),
        "watchlist_age_s": (utc_ms() - p.get("generated_ms", 0)) / 1000 if p else None,
        "macro": p.get("macro", {}),
        "n_candidates": len(p.get("candidates", [])),
    }
    if conn:
        try:
            out["tables"] = {r["name"]: r["c"] for r in conn.execute("""
                SELECT 'ticker_snap' name, COUNT(*) c FROM ticker_snap
                UNION ALL SELECT 'perp_mark', COUNT(*) FROM perp_mark
                UNION ALL SELECT 'oi_now', COUNT(*) FROM oi_now
                UNION ALL SELECT 'ls_ratio', COUNT(*) FROM ls_ratio
                UNION ALL SELECT 'funding_hist', COUNT(*) FROM funding_hist
                UNION ALL SELECT 'book_snap', COUNT(*) FROM book_snap
                UNION ALL SELECT 'basis_snap', COUNT(*) FROM basis_snap
                UNION ALL SELECT 'news', COUNT(*) FROM news
                UNION ALL SELECT 'macro', COUNT(*) FROM macro
                UNION ALL SELECT 'rank_snap', COUNT(*) FROM rank_snap
            """)}
        except sqlite3.Error as exc:
            out["tables_error"] = str(exc)
    return jsonify(out)


_PAIRLIST_CACHE: dict[str, Any] = {"ms": 0, "doc": None, "pairs": []}

# 候选池更新迟滞参数。
# ⚠️ 为什么需要(2026-09-12 实测): 打分每分钟都在变, 若每次都把全新排序返回给 freqtrade,
#    白名单会持续抖动; 每出现一个新交易对, freqtrade 都要重新拉取启动历史 K 线,
#    在 5m+1h 两个周期上 × 数十个交易对 → 直接打爆币安「每 IP 2400 weight/min」配额(429)。
#    因此这里加迟滞: 至少间隔 PAIRLIST_MIN_INTERVAL_S 秒, 且变化幅度超过
#    PAIRLIST_MAX_CHURN 才真正换池; 同时把名额填满以尽量保持集合稳定。
PAIRLIST_MIN_INTERVAL_S = 600      # 最短 10 分钟才允许换池
PAIRLIST_MAX_CHURN = 0.20          # 变动超过 20% 才换池

# 与 dshc/binance.py::NON_TRADABLE_BASES 保持一致(看板为独立文件, 避免相互导入)
_NON_TRADABLE_BASES = {
    "USDC", "BUSD", "TUSD", "FDUSD", "DAI", "USDP", "EUR", "AEUR", "USDTB",
    "USD1", "XUSD", "EURI", "BFUSD", "PAXG",
}


@app.get("/api/pairlist")
def api_pairlist() -> Response:
    """freqtrade RemotePairList 专用端点.

    契约: {"pairs": ["BTC/USDT:USDT", ...], "refresh_period": N}

    两个关键设计(都是 429 限流事故的产物):
    1. **主池缓存 + 迟滞**: 主池(DSHC_TOP_N 条)每 PAIRLIST_MIN_INTERVAL_S 才重算,
       且变动 < PAIRLIST_MAX_CHURN 时沿用旧池 —— 否则 freqtrade 白名单持续抖动,
       每换入一个新对就要重拉 5m+1h 历史 K 线, 打爆共享的 2400 weight/min 配额。
    2. **按 ?n= 切分**: 各实验可用不同规模的池子(越小启动越省配额),
       但都来自**同一份已排序主池** —— 保证标的集合一致、实验可比, 且不做二次迟滞。
    """
    try:
        default_n = int(os.environ.get("DSHC_TOP_N", "40"))
    except ValueError:
        default_n = 40
    try:
        req_n = int(request.args.get("n", default_n))
    except (TypeError, ValueError):
        req_n = default_n
    top_n = max(5, min(req_n, 120))

    now = utc_ms()
    main = list(_PAIRLIST_CACHE.get("pairs") or [])
    if not main or now - int(_PAIRLIST_CACHE.get("ms", 0)) > PAIRLIST_MIN_INTERVAL_S * 1000:
        fresh: list[str] = []
        try:
            for c in wl().get("candidates", []):
                sym = str(c.get("symbol", ""))
                if not sym.endswith("USDT"):
                    continue
                base = sym[:-4]
                if not base or not base.isascii():
                    continue          # 跳过非 ASCII 合约代码(如某些中文名的币)
                if base in _NON_TRADABLE_BASES:
                    continue
                fresh.append(f"{base}/USDT:USDT")
                if len(fresh) >= default_n:
                    break
        except Exception as exc:  # noqa: BLE001
            log.warning("读取候选池失败: %s", exc)
        if not fresh:
            fresh = ["BTC/USDT:USDT", "ETH/USDT:USDT", "SOL/USDT:USDT"]
        if main:
            changed = len(set(fresh) ^ set(main)) / max(len(set(main)), 1)
            if changed < PAIRLIST_MAX_CHURN:
                fresh = main                     # 变化不大 -> 保持稳定, 避免 K 线重拉
            else:
                keep = [p for p in main if p in fresh]
                add = [p for p in fresh if p not in keep]
                fresh = (keep + add)[:default_n]
        main = fresh
        _PAIRLIST_CACHE.update({"ms": now, "pairs": main})

    pairs = main[:top_n] if main else ["BTC/USDT:USDT", "ETH/USDT:USDT"]
    doc = {"pairs": pairs, "refresh_period": PAIRLIST_MIN_INTERVAL_S}
    return Response(json.dumps(doc, ensure_ascii=False), mimetype="application/json")


@app.get("/api/watchlist")
def api_watchlist() -> Response:
    return jsonify(wl())


@app.get("/api/trades")
def api_trades() -> Response:
    return jsonify({"status": _ft_api("/api/v1/status") or [],
                    "profit": _ft_api("/api/v1/profit") or {},
                    "balance": _ft_api("/api/v1/balance") or {}})


@app.get("/api/funding/<symbol>")
def api_funding(symbol: str) -> Response:
    conn = db()
    if not conn:
        return jsonify({"error": "db missing"}), 503
    rows = conn.execute("""
        SELECT ts_ms, funding_rate, mark_price FROM funding_hist
        WHERE symbol=? ORDER BY ts_ms DESC LIMIT 60
    """, (symbol.upper(),)).fetchall()
    return jsonify([{"ts_ms": r["ts_ms"], "utc": fmt_both(r["ts_ms"]),
                     "funding_rate": r["funding_rate"],
                     "funding_ann": (r["funding_rate"] or 0) * 3 * 365,
                     "mark_price": r["mark_price"]} for r in rows])


@app.get("/api/closed")
def api_closed() -> Response:
    """已平仓交易明细 + 汇总 (含 exit_reason / 盈亏 / 持仓时长)."""
    trades = _ft_api("/api/v1/trades?limit=200") or {}
    rows = trades.get("trades", trades) if isinstance(trades, dict) else trades
    out = []
    for t in (rows or []):
        if t.get("is_open"):
            continue
        out.append({
            "pair": t.get("pair"), "is_short": t.get("is_short"),
            "open_date": t.get("open_date"), "close_date": t.get("close_date"),
            "open_rate": t.get("open_rate"), "close_rate": t.get("close_rate"),
            "profit_abs": t.get("profit_abs"), "profit_ratio": t.get("profit_ratio"),
            "exit_reason": t.get("exit_reason"), "enter_tag": t.get("enter_tag"),
            "leverage": t.get("leverage"), "stake_amount": t.get("stake_amount"),
            "funding_fees": t.get("funding_fees"),
            "duration_min": _duration_min(t.get("open_date"), t.get("close_date")),
        })
    return jsonify({"count": len(out), "trades": out,
                    "sum_profit": sum(x["profit_abs"] or 0 for x in out),
                    "sum_funding": sum(x["funding_fees"] or 0 for x in out)})


def _duration_min(a: Any, b: Any) -> float | None:
    try:
        from datetime import datetime
        fa = datetime.fromisoformat(str(a)); fb = datetime.fromisoformat(str(b))
        return round((fb - fa).total_seconds() / 60.0, 2)
    except Exception:  # noqa: BLE001
        return None


@app.get("/api/gainers")
def api_gainers() -> Response:
    conn = db()
    if not conn:
        return jsonify([])
    rows = conn.execute("""
        SELECT t.symbol, t.price, t.price_change_pct, t.quote_vol, m.last_funding_rate
        FROM ticker_snap t
        LEFT JOIN perp_mark m ON m.symbol=t.symbol AND m.ts_ms=t.ts_ms
        WHERE t.ts_ms=(SELECT MAX(ts_ms) FROM ticker_snap) AND t.quote_vol > 5000000
        ORDER BY t.price_change_pct DESC LIMIT 100
    """).fetchall()
    return jsonify([dict(r) for r in rows])


@app.get("/health")
def health() -> Response:
    ok = DB.exists()
    return jsonify({"status": "ok" if ok else "degraded", "db": str(DB),
                    "ts": fmt_both(utc_ms())}), (200 if ok else 503)


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    port = int(os.environ.get("DSHC_DASH_PORT_INTERNAL", "8081"))
    log.info("M3-DSH 看板启动: http://0.0.0.0:%d  (北京时间 %s)", port, fmt_cn(utc_ms()))
    app.run(host="0.0.0.0", port=port, threaded=True)


if __name__ == "__main__":
    main()
