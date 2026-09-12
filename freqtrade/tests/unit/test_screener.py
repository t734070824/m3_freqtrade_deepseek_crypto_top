"""选币打分引擎的关键不变量测试.

重点保护两件事(都踩过坑):
  1. 资金费率年化换算必须乘上结算周期(8h 周期 -> ×3×365), 不能当成日费率;
  2. 榜单「稳定性」必须基于**全市场名次**, 而不是候选池内序号 —— 后者会让所有候选项
     都变成 100% 在榜, 因子彻底失效(2026-09-12 实际发生)。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from dshc.screener import (Candidate, _stickiness, _tags, annualize_funding,  # noqa: E402
                           build_candidates, rank_stability, score_candidate)


# ---------------------------------------------------------------- 资金费率
@pytest.mark.parametrize("rate,interval,expected", [
    (0.0001, 8, 0.0001 * 3 * 365),      # 0.01%/8h -> 10.95%/年
    (0.0005, 8, 0.0005 * 3 * 365),
    (0.0001, 4, 0.0001 * 6 * 365),      # 4h 周期 -> 每天结算 6 次
    (-0.0002, 8, -0.0002 * 3 * 365),    # 负费率 = 空头付给多头
])
def test_annualize_funding(rate: float, interval: int, expected: float) -> None:
    assert annualize_funding(rate, interval) == pytest.approx(expected)


# ---------------------------------------------------------------- 稳定性因子
def test_stickiness_prefers_long_lasting_symbols() -> None:
    """长期霸榜(3 小时以上)应显著优于只闪现几分钟的脉冲票."""
    sticky = {"max_run_min": 240.0, "pct_in_top": 90.0, "mean_rank": 3.0, "n": 48}
    burst = {"max_run_min": 5.0, "pct_in_top": 3.0, "mean_rank": 12.0, "n": 2}
    assert _stickiness(sticky) > 0.8
    assert _stickiness(burst) < 0.3
    assert _stickiness(sticky) > _stickiness(burst)
    assert _stickiness(None) == 0.0


def test_stickiness_bounds() -> None:
    extreme = {"max_run_min": 10000.0, "pct_in_top": 100.0, "mean_rank": 1.0, "n": 999}
    assert -1.0 <= _stickiness(extreme) <= 1.0


# ---------------------------------------------------------------- 榜单稳定性
def _mk_db(rows: list[tuple[int, str, int]]) -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("""CREATE TABLE rank_snap (
        ts_ms INTEGER, symbol TEXT, rank INTEGER, score REAL, change_24h REAL,
        funding_ann REAL, oi_chg_1h REAL, ls_ratio REAL, taker_ratio REAL,
        spread_bps REAL, tags TEXT, payload TEXT)""")
    for ts, sym, rk in rows:
        c.execute("INSERT INTO rank_snap (ts_ms, symbol, rank, score) VALUES (?,?,?,0)",
                  (ts, sym, rk))
    c.commit()
    return c


def test_rank_stability_distinguishes_trend_from_burst() -> None:
    """持续在榜的合约: 最长连续时间长; 脉冲票: 在榜次数多但每次都短."""
    # 必须使用「真实的当前时间」: rank_stability 按 lookback 窗口过滤, 硬编码历史时间戳会被过滤掉
    from dshc.timeutil import utc_ms
    now = utc_ms()
    rows = []
    # TREND: 连续 12 个 5 分钟槽都在 TOP5
    for i in range(12):
        rows.append((now - i * 300_000, "TREND", 3))
    # BURST: 只有 2 个孤立槽在 TOP5, 且相隔很久
    rows.append((now - 60_000, "BURST", 4))
    rows.append((now - 3_600_000, "BURST", 5))
    # 另外补一些填充时间点, 让采样步长稳定
    for i in range(12):
        rows.append((now - i * 300_000, "FILLER", 900))
    st = rank_stability(_mk_db(rows), lookback_hours=48, top_n=5)
    assert "TREND" in st and "BURST" in st
    assert st["TREND"]["max_run_min"] > st["BURST"]["max_run_min"]
    assert st["TREND"]["pct_in_top"] > st["BURST"]["pct_in_top"]
    assert _stickiness(st["TREND"]) > _stickiness(st["BURST"])


# ---------------------------------------------------------------- 打分方向
def test_score_direction_long_vs_short() -> None:
    long_c = Candidate(symbol="XUSDT", base="X", change_24h=8.0, quote_vol=5e7,
                       funding_ann=-0.30, oi_chg_1h=6.0, taker_ratio=1.2, spread_bps=2.0)
    short_c = Candidate(symbol="YUSDT", base="Y", change_24h=-9.0, quote_vol=5e7,
                        funding_ann=0.90, oi_chg_1h=4.0, taker_ratio=0.7, spread_bps=2.0)
    s_long, _ = score_candidate(long_c, macro={"fear_greed": 50.0})
    s_short, _ = score_candidate(short_c, macro={"fear_greed": 50.0})
    assert s_long > 0, f"负费率+上涨应看多, 实际 {s_long}"
    assert s_short < 0, f"高费率+下跌应看空, 实际 {s_short}"


def test_extreme_funding_penalizes_long() -> None:
    base = dict(symbol="ZUSDT", base="Z", change_24h=10.0, quote_vol=5e7, oi_chg_1h=5.0,
                taker_ratio=1.1, spread_bps=2.0)
    cheap = score_candidate(Candidate(**base, funding_ann=-0.20))[0]
    expensive = score_candidate(Candidate(**base, funding_ann=1.50))[0]
    assert cheap > expensive, "年化 150% 的持仓成本必须显著压低评分"


def test_tags_cover_stickiness() -> None:
    c = Candidate(symbol="AUSDT", base="A", stability=0.8, rank_gain=5)
    assert "STICKY" in _tags(c)
    c2 = Candidate(symbol="BUSDT", base="B", stability=0.1, rank_gain=20)
    assert "BURST" in _tags(c2)


# ---------------------------------------------------------------- 端到端装配
def test_build_candidates_includes_stability_term() -> None:
    from dshc.timeutil import utc_ms
    now = utc_ms()
    c = _mk_db([(now - i * 300_000, "AAPLUSDT", 3) for i in range(12)])
    for tbl, cols in (
        ("ticker_snap", "ts_ms INTEGER, symbol TEXT, price REAL, price_change_pct REAL, "
                        "quote_vol REAL, base_vol REAL, trade_count INTEGER, high_24h REAL, "
                        "low_24h REAL, open_24h REAL, weighted_avg REAL"),
        ("perp_mark", "ts_ms INTEGER, symbol TEXT, mark_price REAL, index_price REAL, "
                      "last_funding_rate REAL, next_funding_time_ms INTEGER, interest_rate REAL, "
                      "funding_interval_hours INTEGER, pred_funding_rate REAL"),
        ("oi_now", "ts_ms INTEGER, symbol TEXT, oi REAL, oi_value REAL"),
        ("ls_ratio", "ts_ms INTEGER, symbol TEXT, kind TEXT, long_account REAL, "
                     "short_account REAL, long_pos REAL, short_pos REAL, buy_ratio REAL, "
                     "sell_ratio REAL, ratio REAL"),
        ("book_snap", "ts_ms INTEGER, symbol TEXT, bid REAL, ask REAL, bid_qty REAL, "
                      "ask_qty REAL, spread_bps REAL, depth_bid_usd REAL, depth_ask_usd REAL"),
        ("basis_snap", "ts_ms INTEGER, symbol TEXT, futures_price REAL, index_price REAL, "
                       "basis REAL, basis_rate REAL, ann_basis_rate REAL"),
        ("macro", "ts_ms INTEGER, metric TEXT, value REAL, value_txt TEXT, source TEXT"),
    ):
        c.execute(f"CREATE TABLE {tbl} ({cols})")
    for i in range(12):
        ts = now - i * 300_000
        c.execute("INSERT INTO ticker_snap (ts_ms,symbol,price,price_change_pct,quote_vol) "
                  "VALUES (?,?,?,?,?)", (ts, "AAPLUSDT", 10.0, 12.0, 5e7))
        c.execute("INSERT INTO perp_mark (ts_ms,symbol,mark_price,last_funding_rate,"
                  "funding_interval_hours) VALUES (?,?,?,?,?)", (ts, "AAPLUSDT", 10.0, -0.0001, 8))
    c.commit()
    cands = build_candidates(c, top_n=5, min_quote_vol=1e6)
    assert cands, "应至少产出一个候选"
    top = cands[0]
    assert top.symbol == "AAPLUSDT"
    assert top.details.get("stick") is not None
    assert top.stability > 0
