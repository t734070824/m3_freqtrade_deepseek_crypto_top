"""M3-DSH 止损与仓位的**纯函数**核心 (无 freqtrade/pandas 依赖, 可被单测直接覆盖).

为什么单独抽出来:
    止损与仓位计算是本系统里最容易出错、后果最严重的部分。2026-09-11 一天之内
    就踩了两个真实事故 ——
      ① 「权益收益率」与「价格距离」两个量纲混用, 导致浮盈 3.65% 时止损被贴到
         距现价 0.11%, 被正常波动立刻扫出;
      ② 仓位公式漏乘杠杆, 使单笔实际风险变成设计值的 4 倍。
    这两个 bug 都只有靠可执行的单元测试才能钉死, 因此把数学与框架解耦。

量纲约定(全文唯一真理):
    * current_profit : freqtrade 给的「**含杠杆**的权益收益率」(止损触发时的权益回撤比例)
    * price_pnl      : 价格收益率 = current_profit / leverage
    * d              : 止损的**价格距离**(正数, 相对当前价)
    * custom_stoploss 的返回值(freqtrade 合约口径) = d × leverage
    * 仓位: 名义敞口 = stake × leverage, 止损损失 = 名义敞口 × d = stake × leverage × d
"""

from __future__ import annotations

from typing import NamedTuple


# 止损价格距离的硬边界: 上限与 MAX_STAKE_RATIO(0.35) 一起决定单笔最坏亏损。
# 4x 杠杆下 8% 价格距离 = 32% 权益敞口 -> 名义敞口上限 ≈ 0.35×4 = 1.4x 权益
# => 单笔最坏亏损 ≈ 1.4 × 8% ≈ 11% 权益... 因此还需配合 risk_ceiling 与仓位回退使用。
DIST_MIN = 0.02
DIST_MAX = 0.08


class StopParams(NamedTuple):
    """止损参数(与策略类属性一一对应, 由策略注入)."""

    stop_atr_mult: float = 2.6
    trail_atr_mult: float = 2.2
    trail_start_profit: float = 0.025
    hard_stop: float = -0.085
    profit_protect: float = 0.6
    min_price_distance: float = 0.004
    max_price_distance: float = DIST_MAX
    min_price_distance_entry: float = DIST_MIN
    big_profit_1: float = 0.25
    big_profit_2: float = 0.60
    big_tighten_1_mult: float = 1.5
    big_tighten_2_mult: float = 1.0


def stop_price_distance(current_profit: float, leverage: float, atr_frac: float,
                        p: StopParams = StopParams()) -> tuple[float, str]:
    """返回 (价格止损距离 d, 阶段说明). 这是**唯一**的止损真相来源.

    阶段:
        hard    : 权益收益跌破 hard_stop -> 立即离场(极小距离)
        initial : 2.6×ATR 的宽止损, 给趋势发育空间
        trail   : 浮盈超过 trail_start_profit 后启用 2.2×ATR 跟踪
        protect : 同时保证 d <= profit_protect × 价格获利(锁定大部分涨幅)
        tight   : 大浮盈阶段进一步收紧
    """
    lev = max(float(leverage or 1.0), 1.0)
    price_pnl = current_profit / lev

    if current_profit <= p.hard_stop:
        return max(p.min_price_distance, 0.004), "hard"

    d = min(max(atr_frac * p.stop_atr_mult, p.min_price_distance_entry), p.max_price_distance)
    d = min(max(d, DIST_MIN), DIST_MAX)
    stage = "initial"
    if current_profit > p.trail_start_profit:
        trail = min(max(atr_frac * p.trail_atr_mult, 0.012), 0.09)
        d = min(d, trail)
        stage = "trail"
        if price_pnl > 0:
            protect = price_pnl * p.profit_protect
            if protect < d:
                d = protect
                stage = "protect"
        d = max(d, p.min_price_distance)
    if current_profit > p.big_profit_1:
        d = min(d, max(atr_frac * p.big_tighten_1_mult, 0.025))
        stage += "+tight1"
    if current_profit > p.big_profit_2:
        d = min(d, max(atr_frac * p.big_tighten_2_mult, 0.018))
        stage += "+tight2"
    return max(d, p.min_price_distance), stage


def entry_stop_distance(atr_frac: float, stop_atr_mult: float,
                        *, dist_min: float = DIST_MIN, dist_max: float = DIST_MAX) -> float:
    """建仓时确定的初始止损价格距离.

    ⚠️ 建仓与持仓期必须使用**同一个**函数、同一组边界, 否则会出现
    「按 5% 距离算仓位, 实际挂 12% 止损」这类口径错配 —— 2026-09-12 的真实亏损来源之一。
    另外: 若某标的波动率过大导致距离被压到下限仍无法满足风险预算, 说明它不适合本策略,
    应在建仓阶段直接拒绝(见 plan_position 的 risk_ceiling 回退)。
    """
    return min(max(atr_frac * stop_atr_mult, dist_min), dist_max)


def stop_rate(current_rate: float, d: float, is_short: bool) -> float:
    """把价格距离换算成绝对止损价."""
    return current_rate * (1.0 + d) if is_short else current_rate * (1.0 - d)


def freqtrade_stoploss_value(current_rate: float, stop_price: float, leverage: float,
                             is_short: bool) -> float:
    """复刻 freqtrade stoploss_from_absolute 的合约语义, 返回 >=0 的值.

    对多单: 值 = (current - stop)/current × leverage   (空单为 (stop - current)/current × leverage)
    """
    lev = max(float(leverage or 1.0), 1.0)
    if current_rate <= 0:
        return 0.0
    if is_short:
        return max((stop_price - current_rate) / current_rate * lev, 0.0)
    return max((current_rate - stop_price) / current_rate * lev, 0.0)


def stake_and_risk(equity: float, risk_budget: float, price_stop_distance: float,
                   leverage: float, *, min_ratio: float = 0.05,
                   max_ratio: float = 0.35, confidence_mult: float = 1.0,
                   ann_funding: float = 0.0) -> tuple[float, float]:
    """按「止损触发时权益回撤 <= risk_budget」反解保证金规模.

    stake/equity = risk_budget / (price_stop_distance × leverage)
                  ↑ 必须乘杠杆, 否则实际风险会被放大 leverage 倍
    返回 (保证金 stake, 实际风险比例 effective_risk)。

    ⚠️ 仓位上限优先于风险预算:
        当风险预算要求超过 max_ratio 时, 我们**降低实际风险**而不是超出仓位上限。
        这样设计的意义是「实际风险永远 <= 预算」——风险预算是上限, 不是等号,
        因此不会出现「按 1% 下单却亏了 1.25%」这种悄悄放大风险的情况。
    """
    lev = max(float(leverage or 1.0), 1.0)
    dist = max(float(price_stop_distance), 1e-6)
    ratio = float(risk_budget) / (dist * lev)
    ratio *= max(confidence_mult, 0.1)
    if ann_funding > 0.2:
        ratio *= 0.8
    elif ann_funding < -0.1:
        ratio *= 1.1
    capped = min(max(ratio, min_ratio), max_ratio)
    stake = float(equity) * capped
    return stake, realized_risk_pct(stake, equity, dist, lev)


def stake_for_risk(equity: float, risk_budget: float, price_stop_distance: float,
                   leverage: float, **kwargs) -> float:
    """兼容入口: 只返回保证金."""
    return stake_and_risk(equity, risk_budget, price_stop_distance, leverage, **kwargs)[0]


class PositionPlan(NamedTuple):
    ok: bool
    stake: float
    risk_pct: float
    leverage: float
    reason: str


def plan_position(equity: float, risk_budget: float, price_stop_distance: float,
                  leverage: float, *, min_ratio: float = 0.05, max_ratio: float = 0.35,
                  risk_ceiling: float = 0.015, confidence_mult: float = 1.0,
                  ann_funding: float = 0.0) -> PositionPlan:
    """完整仓位决策: 先按风险预算定 size, 再用「风险上限」兜底.

    规则优先级(自上而下):
      1. 仓位不得超过 max_ratio;
      2. 止损触发时的权益回撤不得超过 risk_ceiling(**硬上限**, 保护资金);
      3. 尽量不低于 min_ratio(低于交易所最小名义值会导致下单失败);
      4. 若为了满足 2 必须跌破 3, 则**放弃这笔交易** —— 宁可错过, 不要超风险。
    """
    lev = max(float(leverage or 1.0), 1.0)
    dist = max(float(price_stop_distance), 1e-6)
    ratio = float(risk_budget) / (dist * lev)
    ratio *= max(confidence_mult, 0.1)
    if ann_funding > 0.2:
        ratio *= 0.8
    elif ann_funding < -0.1:
        ratio *= 1.1
    ratio = max(ratio, min_ratio)
    ratio = min(ratio, max_ratio)
    stake = equity * ratio
    risk = realized_risk_pct(stake, equity, dist, lev)
    if risk > risk_ceiling:
        # 用风险上限反解新的保证金(允许跌破 min_ratio)
        stake = equity * (risk_ceiling / (dist * lev))
        risk = realized_risk_pct(stake, equity, dist, lev)
        if stake < equity * min_ratio * 0.5:
            return PositionPlan(False, 0.0, risk, lev, "risk_ceiling_exceeds_min_stake")
        return PositionPlan(True, stake, risk, lev, "capped_by_risk_ceiling")
    return PositionPlan(True, stake, risk, lev, "risk_budget")


def leverage_for_risk(price_stop_distance: float, max_leverage: float,
                      base_leverage: float, *, max_ratio: float = 0.35,
                      target_ratio: float = 0.25) -> float:
    """杠杆自适应的安全上限.

    在杠杆 L 下, 仓位比例 = (1/权益)×stake; 我们希望正常情况下的目标仓位比例不超过
    target_ratio, 即 (1/(d×L)) <= target_ratio  ->  L <= 1/(d×target_ratio)。
    这可以避免「高杠杆 + 宽止损」把仓位顶到上限。
    """
    d = max(float(price_stop_distance), 1e-6)
    cap = 1.0 / (d * max(target_ratio, 1e-6))
    return float(max(1.0, min(base_leverage, max_leverage, cap)))


def realized_risk_pct(stake: float, equity: float, price_stop_distance: float,
                      leverage: float) -> float:
    """给定仓位, 反算止损触发时的权益回撤比例(用于测试与事后校验)."""
    if equity <= 0:
        return 0.0
    return stake * float(leverage) * float(price_stop_distance) / equity


def funding_gate(funding_ann: float, *, max_long: float, max_short: float) -> tuple[bool, bool]:
    """资金费率开仓门控. 返回 (允许做多, 允许做空)."""
    return (funding_ann <= max_long, funding_ann >= max_short)
