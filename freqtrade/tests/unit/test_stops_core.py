"""风控数学的回归测试 (v0.5.0).

这些断言直接对应真实事故与不变量, 属于「不许再犯」级别的护栏:

  事故 ① 量纲混用: 浮盈 3.65% 被算成 0.11% 的价格止损距离, 被噪声扫出
  事故 ② 仓位漏乘杠杆: 单笔实际风险变成设计值的 4 倍
  事故 ③ 用「当前浮盈」做盈利保护: 浮盈回撤时止损被**放宽**, 已锁利润被交回
  事故 ④ 分档上限写成 atr×倍数: 高档位上限反而比低档位宽(非单调)

因此止损距离被约束为四条不变量:
  I1 距离始终落在 [DIST_MIN, DIST_MAX] 内
  I2 以**峰值浮盈**为基准时, 止损位置(peak/leverage - d)随峰值单调不减
  I3 距离随峰值浮盈单调不增, 且收紧档位只按峰值判定
  I4 换算回 freqtrade 口径: 权益风险 = d × leverage

运行: pytest -q freqtrade/tests/unit/test_stops_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "user_data"))

from stops_core import (DIST_MAX, DIST_MIN, StopParams,  # noqa: E402
                        entry_stop_distance, freqtrade_stoploss_value, funding_gate,
                        leverage_for_risk, plan_position, realized_risk_pct, stake_and_risk,
                        stake_for_risk, stop_price_distance, stop_rate)

LEVS = (1.0, 2.0, 3.0, 5.0)
ATRS = (0.005, 0.008, 0.02, 0.05, 0.12)
PEAKS = [i / 100 for i in range(3, 121, 3)]


# ---------------------------------------------------------------- I1/I2/I3
def test_i1_distance_bounds() -> None:
    for atr in ATRS:
        for lev in LEVS:
            for peak in PEAKS:
                d, _ = stop_price_distance(peak, lev, atr, peak_profit=peak)
                assert DIST_MIN - 1e-12 <= d <= DIST_MAX + 1e-12, (atr, lev, peak, d)


def test_i2_stop_level_monotonic_in_peak() -> None:
    """核心不变量: 峰值浮盈越高, 止损位置(价格口径)只能更高, 绝不能下调。"""
    for atr in ATRS:
        for lev in LEVS:
            prev = None
            for peak in PEAKS:
                d, _ = stop_price_distance(peak, lev, atr, peak_profit=peak)
                level = peak / lev - d          # 锁定的价格收益下界
                if prev is not None:
                    assert level >= prev - 1e-9, (atr, lev, peak, level, prev)
                prev = level


def test_i3_distance_non_increasing_in_peak() -> None:
    for atr in ATRS:
        for lev in LEVS:
            prev = None
            for peak in [p for p in PEAKS if p > 0.03]:
                d, _ = stop_price_distance(peak, lev, atr, peak_profit=peak)
                if prev is not None:
                    assert d <= prev + 1e-9, (atr, lev, peak, d, prev)
                prev = d


def test_i4_equity_risk_is_distance_times_leverage() -> None:
    for lev in LEVS:
        d, _ = stop_price_distance(0.10, lev, 0.02, peak_profit=0.10)
        rate = 100.0
        sp = stop_rate(rate, d, False)
        val = freqtrade_stoploss_value(rate, sp, lev, False)
        assert val / lev == pytest.approx(d, rel=1e-9)
        assert val == pytest.approx(d * lev, rel=1e-9)


# ---------------------------------------------------------------- 事故回归
def test_accident_1_old_formula_would_fail() -> None:
    """旧的错误写法(权益收益率 - 固定常数)会算出贴价止损, 必须被现在的设计排除。"""
    leverage, profit = 4.0, 0.0365
    buggy = profit / leverage - 0.008
    fixed, _ = stop_price_distance(profit, leverage, 0.0199, peak_profit=profit)
    assert buggy < 0.004 <= fixed


def test_accident_3_drawdown_keeps_stop() -> None:
    """峰值 20% 后浮盈回落, 止损距离不得放宽(必须按峰值档位)。"""
    ds = [stop_price_distance(cur, 3.0, 0.02, peak_profit=0.20)[0]
          for cur in (0.20, 0.15, 0.10, 0.05, 0.03)]
    assert all(x == pytest.approx(ds[0]) for x in ds), ds


def test_hard_stop() -> None:
    d, stage = stop_price_distance(-0.10, 4.0, 0.03, peak_profit=0.0)
    assert stage == "hard"
    assert d <= 0.026


def test_initial_stop_uses_atr_and_is_clamped() -> None:
    wide, stage = stop_price_distance(0.0, 4.0, 0.05)
    assert stage == "initial"
    assert DIST_MIN <= wide <= DIST_MAX
    tiny, _ = stop_price_distance(0.0, 4.0, 0.0001)
    assert tiny == pytest.approx(DIST_MIN)
    assert entry_stop_distance(0.05, 2.2) == pytest.approx(DIST_MAX)
    assert entry_stop_distance(0.0001, 2.2) == pytest.approx(DIST_MIN)


# ---------------------------------------------------------------- 事故 ② 回归
@pytest.mark.parametrize("leverage", LEVS)
def test_stake_respects_risk_budget(leverage: float) -> None:
    equity, budget, dist = 1000.0, 0.01, 0.05
    stake, risk = stake_and_risk(equity, budget, dist, leverage=leverage)
    assert stake <= equity * 0.35 + 1e-9
    if stake > equity * 0.05 + 1e-9:
        assert risk <= budget + 1e-12, f"lev={leverage} 风险 {risk}"
    if leverage <= 4.0:
        assert risk == pytest.approx(budget, rel=1e-9)


def test_plan_position_never_exceeds_ceiling() -> None:
    worst = 0.0
    for dist in (0.025, 0.03, 0.05, 0.08):
        for lev in LEVS:
            for budget in (0.002, 0.005, 0.008, 0.02):
                plan = plan_position(1000.0, budget, dist, leverage=lev, risk_ceiling=0.012)
                worst = max(worst, plan.risk_pct)
                assert plan.risk_pct <= 0.012 + 1e-12, (dist, lev, budget, plan)
                # 仓位上限是 max_ratio=0.35 -> 最多 350 USDT(risk_ceiling 只在必要时才压低)
                assert plan.stake <= 350.0 + 1e-9
    assert worst <= 0.012


def test_leverage_cap() -> None:
    for dist in (0.025, 0.05, 0.08):
        lev = leverage_for_risk(dist, max_leverage=5.0, base_leverage=3.0)
        assert 1.0 <= lev <= 5.0


def test_stake_for_risk_alias() -> None:
    assert stake_for_risk(1000.0, 0.008, 0.05, 3.0) == pytest.approx(
        stake_and_risk(1000.0, 0.008, 0.05, 3.0)[0])


def test_realized_risk_pct() -> None:
    assert realized_risk_pct(100.0, 1000.0, 0.05, 3.0) == pytest.approx(0.015)


# ---------------------------------------------------------------- 资金费率
def test_funding_gate() -> None:
    assert funding_gate(0.0, max_long=0.45, max_short=-0.50) == (True, True)
    assert funding_gate(0.90, max_long=0.45, max_short=-0.50) == (False, True)
    assert funding_gate(-0.90, max_long=0.45, max_short=-0.50) == (True, False)
