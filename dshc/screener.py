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
# ---------------------------------------------------------------------------
# 因子权重
# 依据: 用 scripts/dshc_analyze.py alpha 在 dry-run 真实数据上做分桶检验 + 相关性检验
#       2026-09-11 23:05 北京时间 首轮小样本(n=55, horizon=15m)结果:
#         Pearson(score, 未来收益)   = +0.381
#         Pearson(OI 1h 变化, 收益)   = +0.502   <- 最强单因子
#         Pearson(年化费率, 收益)     = +0.194
#         打分分桶平均收益单调递增: -0.11% -> +0.13% -> +0.32% -> +0.47% -> +0.93%
#         涨幅榜 TOP10 平均 +1.50%(胜率80%) vs 全体市场 +0.35%(60%) -> 追涨在该样本期有效
#       因此上调 oi 权重、略降 funding(其在高费率桶内反而偏正, 需更多样本确认)。
# 注意: 这只是 20 分钟样本, 仅用于给方向, 不作为最终结论;
#       后续必须用数天数据复检, 权重也应随市场状态再平衡。
# ---------------------------------------------------------------------------
DEFAULT_WEIGHTS: dict[str, float] = {
    "mom": 28.0,        # 24h 动量(涨幅榜核心, 但非线性: 过热的追高反而扣分)
    "mom_mid": 16.0,    # 中周期动量(OI + taker 合成)
    "funding": 20.0,    # 资金费率: 持有成本/收益
    "oi": 24.0,         # 持仓量变化: 真实资金进出(实测相关性最强)
    "ls": 8.0,          # 多空比拥挤度(反向指标)
    "taker": 6.0,       # 主动买卖比(短期资金流向)
    "basis": 5.0,       # 基差/年化
    "liq": 5.0,         # 流动性/价差
    "fng": 3.0,         # 宏观恐贪
    "stick": 18.0,      # 榜单稳定性: 长期霸榜 = 真趋势; 脉冲票 = 追进去就被埋
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
    stability: float = 0.0
    # 费率分位信息(供「费率极值反转」类策略使用)
    funding_pct_rank: float = 0.0     # 当前费率在该币自身历史中的分位(0~1)
    funding_p25: float = 0.0
    funding_p75: float = 0.0
    funding_p95: float = 0.0
    funding_samples: int = 0
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
            "stability": self.stability, "funding_pct_rank": self.funding_pct_rank,
            "funding_p95": self.funding_p95, "funding_samples": self.funding_samples,
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


def funding_percentile(conn: sqlite3.Connection, symbols: Sequence[str] | None = None,
                       *, min_samples: int = 30) -> dict[str, dict[str, float]]:
    """计算每个合约「当前费率在其自身历史中的分位」.

    为什么要分位而不是绝对阈值(2026-09-12 实测):
        市场整体的费率水平会随时间大幅漂移 —— 当天全体候选的最大年化仅 5.5%,
        若把门槛写死成「年化 >= 100%」则永不触发; 反之在费率普遍高企的行情里
        绝对阈值又形同虚设。
        用「该币自身历史分位」可以自适应: 无论市场处于何种费率环境,
        都能捕捉到「相对自己而言极端」的那一批。

    返回 {symbol: {"cur": 当前费率, "p50"/"p90"/"p95"/"p99": 历史分位,
                   "pct_rank": 当前值的历史分位(0~1), "n": 样本数}}
    """
    out: dict[str, dict[str, float]] = {}
    sql = "SELECT symbol, ts_ms, funding_rate FROM funding_hist WHERE funding_rate IS NOT NULL"
    args: list[Any] = []
    if symbols:
        sql += " WHERE symbol IN (%s)" % ",".join("?" * len(symbols))
        args = list(symbols)
    per: dict[str, list[float]] = {}
    latest: dict[str, float] = {}
    try:
        for r in conn.execute(sql + " ORDER BY symbol, ts_ms", args):
            per.setdefault(r["symbol"], []).append(float(r["funding_rate"]))
        for r in conn.execute("""
            SELECT m.symbol, m.last_funding_rate FROM perp_mark m
            JOIN (SELECT symbol, MAX(ts_ms) mx FROM perp_mark GROUP BY symbol) x
              ON m.symbol=x.symbol AND m.ts_ms=x.mx
        """):
            if r["last_funding_rate"] is not None:
                latest[r["symbol"]] = float(r["last_funding_rate"])
    except sqlite3.OperationalError:
        return {}          # 表结构不完整(如精简测试库) -> 返回空, 不影响其它因子
    for sym, hist in per.items():
        cur = latest.get(sym)
        if cur is None or len(hist) < min_samples:
            continue
        sv = sorted(hist)
        def q(p: float) -> float:
            return sv[min(int(len(sv) * p), len(sv) - 1)]
        below = sum(1 for v in sv if v <= cur)
        out[sym] = {"cur": cur, "p50": q(0.50), "p90": q(0.90), "p95": q(0.95),
                    "p99": q(0.99), "pct_rank": below / len(sv), "n": float(len(sv))}
    return out


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


def rank_stability(conn: sqlite3.Connection, *, lookback_hours: float = 6.0,
                   top_n: int = 15) -> dict[str, dict[str, float]]:
    """统计每个合约在**涨幅榜 TOP N** 内的「稳定性」特征.

    动机(2026-09-12 实测): 涨幅榜标的可以清晰分成两类 ——
      * 持续趋势票: 长期霸榜(如 LAB/龙虾/RAYSOL: 平均名次 2~6, 连续在榜 180~240 分钟)
      * 脉冲票    : 上榜次数多但每次只有几分钟, 追进去就被埋
    因此把「在榜时长 / 名次」做成因子, 而不是只看瞬时涨幅。

    返回 {symbol: {"in_top_min": 累计在榜分钟, "max_run_min": 最长连续分钟,
                    "mean_rank": 平均名次, "pct_in_top": 在榜时间占比, "n": 样本数}}
    """
    from .timeutil import utc_ms
    since = utc_ms() - int(lookback_hours * 3600_000)
    rows = list(conn.execute(
        "SELECT ts_ms, symbol, rank FROM rank_snap WHERE ts_ms >= ? ORDER BY symbol, ts_ms",
        (since,)))
    if not rows:
        return {}
    out: dict[str, dict[str, float]] = {}
    per: dict[str, list[tuple[int, int]]] = {}
    for r in rows:
        per.setdefault(r["symbol"], []).append((int(r["ts_ms"]), int(r["rank"] or 9999)))
    # 采样步长(通常 5 分钟)
    step_ms = 300_000
    all_ts = sorted({int(r["ts_ms"]) for r in rows})
    if len(all_ts) > 2:
        ts_sorted = all_ts
        diffs = sorted(b - a for a, b in zip(ts_sorted, ts_sorted[1:]) if b > a)
        if diffs:
            step_ms = diffs[len(diffs) // 2]
    n_slots = max(len(all_ts), 1)
    for sym, v in per.items():
        v.sort()
        in_top = [(t, rk) for t, rk in v if rk <= top_n]
        if not in_top:
            continue
        runs, cur = [], 1
        ts_in_top = [t for t, _ in in_top]
        for a, b in zip(ts_in_top, ts_in_top[1:]):
            if b - a <= step_ms * 1.5:
                cur += 1
            else:
                runs.append(cur); cur = 1
        runs.append(cur)
        out[sym] = {
            "in_top_min": len(in_top) * step_ms / 60_000.0,
            "max_run_min": max(runs) * step_ms / 60_000.0,
            "mean_rank": _mean([float(rk) for _, rk in in_top]),
            "pct_in_top": len(in_top) / n_slots * 100.0,
            "n": float(len(in_top)),
        }
    return out


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def _stickiness(st: dict[str, float] | None) -> float:
    """把稳定性特征压成 [-1, 1] 的因子: 越长、名次越靠前 -> 越大."""
    if not st:
        return 0.0
    # 标定说明: 连续 60 分钟 / 覆盖 60% / 名次前 3 即视为「满值」。
    # 早期把连续时长满值设为 180 分钟, 在冷启动阶段(样本 < 1 小时)会让因子恒为 0, 失去作用。
    run = min(st.get("max_run_min", 0.0) / 60.0, 1.0)
    cover = min(st.get("pct_in_top", 0.0) / 60.0, 1.0)
    rank = 1.0 - min(max((st.get("mean_rank", 15.0) - 1.0) / 10.0, 0.0), 1.0)
    return max(-1.0, min(1.0, 0.45 * run + 0.30 * cover + 0.25 * rank))


def build_candidates(conn: sqlite3.Connection, *, top_n: int = 40,
                     min_quote_vol: float = 30_000_000.0,
                     weights: dict[str, float] | None = None,
                     ts_ms: int | None = None, use_stability: bool = True) -> list[Candidate]:
    """生成按 |score| 排序的候选池."""
    from .timeutil import utc_ms
    ts = ts_ms or utc_ms()
    feats = load_features(conn, ts)
    macro = latest_macro(conn)
    stab = rank_stability(conn) if use_stability else {}
    fq = funding_percentile(conn) if use_stability else {}

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
        # 稳定性因子: 只在「有真实霸榜历史」时加分(避免冷启动噪声)
        fqi = fq.get(sym)
        if fqi:
            c.funding_pct_rank = round(fqi["pct_rank"], 4)
            c.funding_p25 = round(fqi["p50"], 8)
            c.funding_p75 = round(fqi["p90"], 8)
            c.funding_p95 = round(fqi["p95"], 8)
            c.funding_samples = int(fqi["n"])
        st = stab.get(sym)
        if st and st.get("n", 0) >= 3:      # 至少 3 个采样点(约 15 分钟)才启用稳定性因子
            stick = _stickiness(st)
            w = (weights or DEFAULT_WEIGHTS).get("stick", DEFAULT_WEIGHTS["stick"])
            parts["stick"] = round(w * stick, 4)
            s = max(-100.0, min(100.0, s + parts["stick"]))
            c.stability = stick
        else:
            parts["stick"] = 0.0
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
    if c.stability >= 0.6:
        t.append("STICKY")      # 长期霸榜 -> 可持有
    elif c.stability > 0 and c.stability <= 0.2:
        t.append("BURST")       # 脉冲型 -> 容易追高被埋
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
