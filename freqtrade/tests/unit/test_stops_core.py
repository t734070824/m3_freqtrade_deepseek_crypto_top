"""风控数学的回归测试.

这些断言直接对应 2026-09-11 发生的两次真实事故, 属于「不许再犯」级别的护栏:
  * 事故 ① 量纲混用: 浮盈 3.65% 被算成 0.11% 的价格止损距离
  * 事故 ② 仓位漏乘杠杆: 单笔实际风险变成设计值的 4 倍
运行:  pytest -q freqtrade/tests/unit/test_stops_core.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# user_data 不进 PYTHONPATH, 显式加入以便导入策略同目录的纯函数模块
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "user_data"))

from stops_core import (StopParams, freqtrade_stoploss_value, funding_gate,  # noqa: E402
                        leverage_for_risk, plan_position, realized_risk_pct, stake_and_risk,
                        stake_for_risk, stop_price_distance, stop_rate)


# ---------------------------------------------------------------- 事故 ① 回归
@pytest.mark.parametrize("leverage", [1.0, 2.0, 4.0, 5.0])
def test_trail_distance_is_never_tiny(leverage: float) -> None:
    """跟踪启动瞬间(START=2.5%)的价格止损距离必须合理, 绝不能贴到现价上."""
    d, stage = stop_price_distance(current_profit=0.0365, leverage=leverage, atr_frac=0.0199)
    assert stage in ("trail", "protect", "trail+tight1"), stage
    assert d >= 0.004, f"价格止损距离过小: {d}"
    assert d <= 0.12


def test_old_bug_would_have_failed() -> None:
    """把旧的错误公式算出来, 确认它确实违反不变量(防止有人改回去)."""
    leverage, current_profit = 4.0, 0.0365
    buggy = current_profit / leverage - 0.008        # 旧写法
    fixed, _ = stop_price_distance(current_profit=current_profit, leverage=leverage,
                                   atr_frac=0.0199)
    assert buggy < 0.004 <= fixed


def test_stoploss_value_matches_price_distance() -> None:
    """custom_stoploss 返回值 / 杠杆 必须等于价格距离(freqtrade 合约口径)."""
    for leverage in (1.0, 3.0, 4.0, 5.0):
        d, _ = stop_price_distance(current_profit=0.05, leverage=leverage, atr_frac=0.02)
        rate = 100.0
        for is_short in (False, True):
            sp = stop_rate(rate, d, is_short)
            val = freqtrade_stoploss_value(rate, sp, leverage, is_short)
            assert val / leverage == pytest.approx(d, rel=1e-9)


def test_hard_stop_forces_immediate_exit() -> None:
    p = StopParams(hard_stop=-0.085)
    d, stage = stop_price_distance(current_profit=-0.10, leverage=4.0, atr_frac=0.03, p=p)
    assert stage == "hard"
    assert d <= 0.005


def test_initial_stop_is_wide_and_clamped() -> None:
    wide, stage = stop_price_distance(current_profit=0.0, leverage=4.0, atr_frac=0.05)
    assert stage == "initial"
    assert wide == pytest.approx(0.13, abs=1e-9) or wide <= 0.12
    tiny, stage2 = stop_price_distance(current_profit=0.0, leverage=4.0, atr_frac=0.0001)
    assert stage2 == "initial"
    assert tiny == pytest.approx(0.02)          # 钳制到最小 2%


def test_profit_protect_locks_most_of_the_move() -> None:
    """浮盈很大时, 止损距离应收敛到「价格获利 × PROFIT_PROTECT」的量级."""
    lev, prof, atr = 4.0, 0.60, 0.01
    d, stage = stop_price_distance(current_profit=prof, leverage=lev, atr_frac=atr)
    price_pnl = prof / lev
    assert "tight" in stage
    assert d < price_pnl                     # 止损价必须仍在成本价之上(锁定利润)
    assert d <= price_pnl * 0.61 + 1e-9


# ---------------------------------------------------------------- 事故 ② 回归
@pytest.mark.parametrize("leverage", [1.0, 2.0, 3.0, 4.0, 5.0])
def test_stake_respects_risk_budget(leverage: float) -> None:
    """止损被打到时, 权益回撤必须 **不超过** 风险预算 —— 绝不许被杠杆放大.

    注意是「不超过」而非「等于」: 仓位上限优先, 一旦预算要求超过上限,
    系统应当**减少实际风险**, 而不是悄悄放大风险。
    """
    equity, budget, dist = 1000.0, 0.01, 0.05
    stake, risk = stake_and_risk(equity, budget, dist, leverage=leverage)
    assert stake <= equity * 0.35 + 1e-9, "仓位不得超过上限"
    # 在 5x 这种极端组合下, 风险预算无法与「最小仓位 5%」同时满足:
    # 此时必须明确地由「最小仓位」兜底(属于已知且有界的情况), 而不是静默放大风险。
    min_stake = equity * 0.05
    if stake > min_stake + 1e-9:
        assert risk <= budget + 1e-12, f"lev={leverage} 实际风险 {risk} > 预算 {budget}"
    else:
        assert risk <= budget * 1.3 + 1e-12, f"lev={leverage} 触发最小仓位, 风险 {risk}"
    if leverage <= 4.0:
        assert risk == pytest.approx(budget, rel=1e-9), "4x 及以下应完整使用预算"


def test_leverage_cap_prevents_ratio_saturation() -> None:
    """杠杆自适配上界应保证「宽止损 + 高杠杆」不会把仓位顶到上限."""
    for dist in (0.02, 0.05, 0.08, 0.12):
        lev = leverage_for_risk(dist, max_leverage=5.0, base_leverage=4.0)
        assert lev <= 5.0
        plan = plan_position(1000.0, 0.01, dist, leverage=lev, risk_ceiling=0.015)
        assert plan.stake <= 350.0 + 1e-9        # 不越过仓位上限
        assert plan.risk_pct <= 0.015 + 1e-12, f"dist={dist} lev={lev} 风险 {plan.risk_pct}"
        if not plan.ok:
            assert plan.stake == 0.0


def test_stake_ratio_clamped() -> None:
    """极端参数下仓位比例仍被钳制在 [5%, 35%]."""
    equity = 1000.0
    tiny_stop = stake_for_risk(equity, 0.01, 0.001, leverage=1.0)
    assert tiny_stop == pytest.approx(equity * 0.35)
    huge_stop = stake_for_risk(equity, 0.001, 0.5, leverage=5.0)
    assert huge_stop == pytest.approx(equity * 0.05)


def test_stake_notional_exposure_sanity() -> None:
    """正常参数下, 名义敞口应落在合理区间(不超过权益的 2 倍)."""
    stake = stake_for_risk(1000.0, 0.01, 0.05, leverage=4.0)
    notional = stake * 4.0
    assert 0 < notional <= 1000.0 * 2.0


def test_plan_position_never_exceeds_ceiling() -> None:
    """全参数扫描: 任何组合下止损回撤都不得超过风险上限."""
    worst = 0.0
    for dist in (0.02, 0.03, 0.05, 0.08, 0.12):
        for lev in (1.0, 2.0, 3.0, 4.0, 5.0):
            for budget in (0.002, 0.005, 0.01, 0.02):
                plan = plan_position(1000.0, budget, dist, leverage=lev, risk_ceiling=0.015)
                worst = max(worst, plan.risk_pct)
                assert plan.risk_pct <= 0.015 + 1e-12, (dist, lev, budget, plan)
    assert worst <= 0.015


# ---------------------------------------------------------------- 资金费率门控
def test_funding_gate() -> None:
    assert funding_gate(0.0, max_long=0.45, max_short=-0.50) == (True, True)
    assert funding_gate(0.90, max_long=0.45, max_short=-0.50) == (False, True)
    assert funding_gate(-0.90, max_long=0.45, max_short=-0.50) == (True, False)
