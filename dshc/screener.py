"""选币打分引擎: 从「涨幅榜」+ 场内外衍生数据生成交易候选.

核心思想 (M3-DSH)
-----------------
涨幅榜合约的特点是「快涨 -> 高资金费率 -> 多头拥挤 -> 插针回撤」。
单纯追涨必死, 所以要同时衡量:

1. 趋势强度   : 多周期动量/均线结构 (K线)
2. 持仓结构   : OI 变化方向与幅度 (资金是进场还是撤退)
3. 拥挤度     : 资金费率 + 大户/散户多空比 + taker 主动买卖比
4. 流动性     : 24h 成交额、盘口价差、最小名义值
5. 宏观风险   : 恐贪指数极端值 + 新闻情绪

输出 score ∈ [-100, +100]:
  > 0 表示「做多有利」, < 0 表示「做空有利」。
  关键: 资金费率对持仓方向做「收益/成本」修正 —— 高正费率利多空头、利空多头。

时间: 全部使用 UTC 毫秒时间戳; 展示时显式标注北京时间(UTC+8)。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .binance import MAJOR_BASES, NON_TRADABLE_BASES

log = logging.getLogger("dshc.screener")

# ---------------------------------------------------------------- 权重(可迭代调优)
DEFAULT_WEIGHTS: dict[str, float] = {
    "mom": 30.0,        # 24h 动量(涨幅榜核心, 但非线性: 过热的追高反而扣分)
    "mom_mid": 18.0,    # 1h/4h 中周期动量
    "funding": 22.0,    # 资金费率: 持有成本/收益 (最重要)
    "oi": 14.0,         # 持仓量变化: 真实资金进出
    "ls": 10.0,         # 多空比拥挤度(反向指标)
    "taker": 8.0,       # 主动买卖比(短期资金流向)
    "basis": 6.0,       # 基差/年化
    "liq": 6.0,         # 流动性/价差
    "fng": 4.0,         # 宏观恐贪
}

# funding 年化阈值: 超过该值认为多头持仓成本过高
FUNDING_ANN_EXTREME = 1.10      # 110% 年化
FUNDING_ANN_HIGH = 0.40         # 40% 年化
FUNDING_ANN_NEG_EXTREME = -0.35


@dataclass
class Candidate:
    symbol: str
    base: str
    price: float = 0.0
    change_24h: float = 0.0
    quote_vol: float = 0.0
    funding_rate: float = 0.0       # 当前周期(通常8h)费率
    funding_ann: float = 0.0        # 年化
    next_funding_ms: int = 0
    mark_price: float = 0.0
    oi_value: float = 0.0
    oi_chg_1h: float = 0.0
    oi_chg_24h: float = 0.0
    ls_ratio: float = 0.0           # 大户持仓多空比
    global_ls: float = 0.0
    taker_ratio: float = 1.0
    basis_rate: float = 0.0
    spread_bps: float = 0.0
    vol_ratio: float = 0.0          # 24h成交额 / 7日均值(需历史, 可缺省)
    score: float = 0.0
    rank_gain: int = 0
    tags: list[str] = field(default_factory=list)
    details: dict[str, float] = field(default_factory=dict)

    def to_payload(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "base": self.base, "price": self.price,
            "change_24h": self.change_24h, "quote_vol": self.quote_vol,
            "funding_rate": self.funding_rate, "funding_ann": self.funding_ann,
            "next_funding_ms": self.next_funding_ms, "mark_price": self.mark_price,
            "oi_value": self.oi_value, "oi_chg_1h": self.oi_chg_1h,
            "oi_chg_24h": self.oi_chg_24h, "ls_ratio": self.ls_ratio,
            "global_ls": self.global_ls, "taker_ratio": self.taker_ratio,
            "basis_rate": self.basis_rate, "spread_bps": self.spread_bps,
            "score": self.score, "rank_gain": self.rank_gain, "tags": self.tags,
        }


def annualize_funding(rate: float, interval_hours: int = 8) -> float:
    """把单期资金费率换算成年化(复利近似用简单乘)."""
    per_day = rate * (24.0 / max(interval_hours, 1))
    return per_day * 365.0


def _squash(x: float, scale: float) -> float:
    """把任意幅度压到 [-1, 1], 用 tanh 防止极端值主导."""
    if scale <= 0:
        return 0.0
    return math.tanh(x / scale)


def load_features(conn: sqlite3.Connection, ts_ms: int,
                  symbols: Sequence[str] | None = None) -> dict[str, dict[str, Any]]:
    """从数据库装配每个合约的特征向量(全部基于已落库的历史, 无未来函数)."""
    feats: dict[str, dict[str, Any]] = {}
    where = ""
    args: list[Any] = []
    if symbols:
        where = " WHERE symbol IN (%s)" % ",".join("?" * len(symbols))
        args = list(symbols)

    # --- 最新 ticker (每 symbol 取最新一条)
    for row in conn.execute(f"""
        SELECT t.symbol, t.price, t.price_change_pct, t.quote_vol, t.high_24h, t.low_24h
        FROM ticker_snap t
        JOIN (SELECT symbol, MAX(ts_ms) mx FROM ticker_snap GROUP BY symbol) m
          ON t.symbol=m.symbol AND t.ts_ms=m.mx {where.replace('WHERE','AND') if where else ''}
    """, args):
        feats.setdefault(row["symbol"], {}).update({
            "price": row["price"], "change_24h": row["price_change_pct"],
            "quote_vol": row["quote_vol"], "high_24h": row["high_24h"], "low_24h": row["low_24h"],
        })

    # --- 最新 mark/funding
    for row in conn.execute(f"""
        SELECT m.symbol, m.mark_price, m.last_funding_rate, m.next_funding_time_ms,
               m.funding_interval_hours, m.index_price
        FROM perp_mark m
        JOIN (SELECT symbol, MAX(ts_ms) mx FROM perp_mark GROUP BY symbol) x
          ON m.symbol=x.symbol AND m.ts_ms=x.mx {where.replace('WHERE','AND') if where else ''}
    """, args):
        feats.setdefault(row["symbol"], {}).update({
            "mark_price": row["mark_price"], "funding_rate": row["last_funding_rate"],
            "next_funding_ms": row["next_funding_time_ms"],
            "funding_interval_hours": row["funding_interval_hours"] or 8,
            "index_price": row["index_price"],
        })

    # --- OI: 最新值 + 1h 前 + 24h 前
    oi_hist = _load_series(conn, "oi_now", "oi_value", ["1h", "24h"], symbols)
    for sym, per in oi_hist.items():
        f = feats.setdefault(sym, {})
        f["oi_value"] = per.get("last")
        f["oi_chg_1h"] = _pct_change(per.get("last"), per.get("1h"))
        f["oi_chg_24h"] = _pct_change(per.get("last"), per.get("24h"))

    # --- 多空比
    for kind, key in (("top_position", "ls_ratio"), ("global_account", "global_ls"),
                      ("taker", "taker_ratio")):
        for sym, val in _load_latest(conn, "ls_ratio", "ratio", kind, symbols).items():
            feats.setdefault(sym, {})[key] = val

    # --- 盘口价差(最近一次)
    for sym, val in _load_latest(conn, "book_snap", "spread_bps", None, symbols).items():
        feats.setdefault(sym, {})["spread_bps"] = val

    # --- 基差
    for sym, val in _load_latest(conn, "basis_snap", "basis_rate", None, symbols).items():
        feats.setdefault(sym, {})["basis_rate"] = val

    return feats


def _load_latest(conn: sqlite3.Connection, table: str, col: str,
                 kind: str | None, symbols: Sequence[str] | None) -> dict[str, float]:
    try:
        if kind is None:
            sql = (f"SELECT t.symbol, t.{col} v FROM {table} t JOIN "
                   f"(SELECT symbol, MAX(ts_ms) mx FROM {table} GROUP BY symbol) m "
                   f"ON t.symbol=m.symbol AND t.ts_ms=m.mx")
            rows = conn.execute(sql).fetchall()
        else:
            sql = (f"SELECT t.symbol, t.{col} v FROM {table} t JOIN "
                   f"(SELECT symbol, MAX(ts_ms) mx FROM {table} WHERE kind=? GROUP BY symbol) m "
                   f"ON t.symbol=m.symbol AND t.ts_ms=m.mx AND t.kind=?")
            rows = conn.execute(sql, (kind, kind)).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["symbol"]: r["v"] for r in rows if r["v"] is not None}


def _load_series(conn: sqlite3.Connection, table: str, col: str,
                 lookbacks: Sequence[str],
                 symbols: Sequence[str] | None) -> dict[str, dict[str, float]]:
    """取最新值以及 N 小时前的值."""
    from .timeutil import utc_ms
    now = utc_ms()
    out: dict[str, dict[str, float]] = {}
    try:
        for r in conn.execute(f"SELECT symbol, ts_ms, {col} v FROM {table}"):
            d = out.setdefault(r["symbol"], {})
            if d.get("last_ts", 0) < r["ts_ms"]:
                d["last_ts"] = r["ts_ms"]
                d["last"] = r["v"]
            for lb in lookbacks:
                hours = float(lb.rstrip("hdm"))
                target = now - hours * 3600_000
                prev = d.get(f"_ts_{lb}")
                if prev is None or abs(r["ts_ms"] - target) < abs(prev - target):
                    d[f"_ts_{lb}"] = r["ts_ms"]
                    d[lb] = r["v"]
    except sqlite3.OperationalError:
        return {}
    return out


def _pct_change(now_v: float | None, then_v: float | None) -> float:
    if not now_v or not then_v:
        return 0.0
    return (now_v - then_v) / then_v * 100.0


def score_candidate(cand: Candidate, w: dict[str, float] | None = None,
                    macro: dict[str, float] | None = None) -> tuple[float, dict[str, float]]:
    """返回 (score, 各分量贡献). score>0 利多, <0 利空."""
    w = {**DEFAULT_WEIGHTS, **(w or {})}
    macro = macro or {}
    parts: dict[str, float] = {}

    # --- 动量: 24h 涨幅, 但>25% 视为过热, 收益递减并转负
    chg = cand.change_24h
    overheat = max(0.0, abs(chg) - 25.0) / 25.0
    mom_raw = _squash(chg, 12.0) * (1.0 - min(overheat, 0.7))
    parts["mom"] = w["mom"] * mom_raw

    # --- 中周期动量: 用 OI 与价格的一致性代理 + taker 方向
    mid = 0.55 * _squash(cand.oi_chg_1h, 8.0) + 0.45 * _squash(cand.taker_ratio - 1.0, 0.35)
    parts["mom_mid"] = w["mom_mid"] * mid

    # --- 资金费率: 正费率=持多成本(扣多头), 负费率=持多收益(加多头)
    ann = cand.funding_ann
    f_pen = 0.0
    if ann > FUNDING_ANN_EXTREME:
        f_pen = -1.0
    elif ann > FUNDING_ANN_HIGH:
        f_pen = -0.55 * (ann - FUNDING_ANN_HIGH) / (FUNDING_ANN_EXTREME - FUNDING_ANN_HIGH) - 0.15
    elif ann < FUNDING_ANN_NEG_EXTREME:
        f_pen = -0.6   # 极端负费率往往伴随空头挤压尾声, 做空要小心
    else:
        f_pen = -_squash(ann, 0.35) * 0.5
    # 负费率对多头是补贴, 轻微加分
    if -0.05 <= ann <= 0.05:
        f_pen += 0.25
    if ann < -0.10:
        f_pen += 0.45
    parts["funding"] = w["funding"] * max(-1.0, min(1.0, f_pen))

    # --- OI: 与价格同向放大趋势, 反向则可疑
    oi_dir = 1.0 if chg >= 0 else -1.0
    oi_score = _squash(cand.oi_chg_1h, 10.0) * (1.0 if chg >= 0 else 1.0)
    parts["oi"] = w["oi"] * oi_score * (0.6 + 0.4 * abs(oi_dir)) * (1.0 if chg >= 0 else -1.0 if chg < 0 and cand.oi_chg_1h > 0 else 1.0)

    # --- 多空比: 大户极度看多 -> 反向扣分
    ls = cand.ls_ratio or 1.0
    parts["ls"] = w["ls"] * (-_squash(ls - 1.0, 0.9))

    # --- taker 主动买卖比
    parts["taker"] = w["taker"] * _squash(cand.taker_ratio - 1.0, 0.4)

    # --- 基差: 期货大幅升水=多头拥挤
    parts["basis"] = w["basis"] * (-_squash(cand.basis_rate, 0.0015))

    # --- 流动性/价差
    spread_pen = _squash(max(cand.spread_bps - 3.0, 0.0), 12.0)
    parts["liq"] = w["liq"] * (-spread_pen)

    # --- 宏观恐贪: 极度贪婪抑制追多, 极度恐慌抑制追空
    fng = macro.get("fear_greed", 50.0)
    parts["fng"] = w["fng"] * (-_squash((fng - 50.0) / 50.0, 0.6))

    total = sum(parts.values())
    return round(max(-100.0, min(100.0, total)), 4), {k: round(v, 4) for k, v in parts.items()}


def build_candidates(conn: sqlite3.Connection, *, top_n: int = 40,
                     min_quote_vol: float = 30_000_000.0,
                     weights: dict[str, float] | None = None,
                     ts_ms: int | None = None) -> list[Candidate]:
    """生成按 |score| 排序的候选池."""
    from .timeutil import utc_ms
    ts = ts_ms or utc_ms()
    feats = load_features(conn, ts)
    macro = latest_macro(conn)

    # 24h 涨幅榜排名(仅在通过流动性门槛的合约内排名)
    ranked = sorted(
        [(s, f) for s, f in feats.items()
         if (f.get("quote_vol") or 0) >= min_quote_vol and (f.get("price") or 0) > 0],
        key=lambda kv: kv[1].get("change_24h") or 0.0, reverse=True,
    )
    rank_map = {s: i + 1 for i, (s, _) in enumerate(ranked)}

    # 候选 = 涨幅榜前 top_n 名 + 跌幅榜前 10 名(做空侧) + 核心锚点
    gainers = [s for s, _ in ranked[:top_n]]
    losers = [s for s, _ in ranked[-10:]]
    anchors = [s for s in feats if _base(s) in MAJOR_BASES]
    universe = []
    for s in gainers + losers + anchors:
        if s not in universe:
            universe.append(s)

    out: list[Candidate] = []
    for sym in universe:
        f = feats[sym]
        c = Candidate(
            symbol=sym, base=_base(sym),
            price=f.get("price") or 0.0,
            change_24h=f.get("change_24h") or 0.0,
            quote_vol=f.get("quote_vol") or 0.0,
            funding_rate=f.get("funding_rate") or 0.0,
            funding_ann=annualize_funding(f.get("funding_rate") or 0.0,
                                          f.get("funding_interval_hours") or 8),
            next_funding_ms=int(f.get("next_funding_ms") or 0),
            mark_price=f.get("mark_price") or 0.0,
            oi_value=f.get("oi_value") or 0.0,
            oi_chg_1h=f.get("oi_chg_1h") or 0.0,
            oi_chg_24h=f.get("oi_chg_24h") or 0.0,
            ls_ratio=f.get("ls_ratio") or 0.0,
            global_ls=f.get("global_ls") or 0.0,
            taker_ratio=f.get("taker_ratio") or 1.0,
            basis_rate=f.get("basis_rate") or 0.0,
            spread_bps=f.get("spread_bps") or 0.0,
            rank_gain=rank_map.get(sym, 9999),
        )
        s, parts = score_candidate(c, weights, macro)
        c.score = s
        c.details = parts
        c.tags = _tags(c)
        out.append(c)

    out.sort(key=lambda c: abs(c.score), reverse=True)
    return out


def _tags(c: Candidate) -> list[str]:
    t: list[str] = []
    if c.rank_gain <= 10:
        t.append("TOP10")
    if c.funding_ann > FUNDING_ANN_HIGH:
        t.append("FUND_HIGH")
    elif c.funding_ann < -0.10:
        t.append("FUND_NEG")
    if c.oi_chg_1h > 6:
        t.append("OI_UP")
    elif c.oi_chg_1h < -6:
        t.append("OI_DOWN")
    if (c.ls_ratio or 1) > 2.0:
        t.append("CROWD_LONG")
    elif 0 < (c.ls_ratio or 1) < 0.6:
        t.append("CROWD_SHORT")
    if c.spread_bps > 15:
        t.append("WIDE_SPREAD")
    return t


def _base(symbol: str) -> str:
    for q in ("USDT", "USDC", "BUSD"):
        if symbol.endswith(q):
            return symbol[: -len(q)]
    return symbol


def latest_macro(conn: sqlite3.Connection) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        for r in conn.execute("""
            SELECT m.metric, m.value FROM macro m
            JOIN (SELECT metric, MAX(ts_ms) mx FROM macro GROUP BY metric) x
              ON m.metric=x.metric AND m.ts_ms=x.mx
        """):
            if r["value"] is not None:
                out[r["metric"]] = r["value"]
    except sqlite3.OperationalError:
        pass
    return out
