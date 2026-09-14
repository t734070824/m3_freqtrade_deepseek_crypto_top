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
# ---- 2026-09-14 根因修正 ----
# 真实等式: 单笔权益损失 = 止损距离(价格) x 杠杆。
# 旧值 2.5% 在 2x 下意味着「止损一发就亏 5.0% 权益」、3x 下 7.5% —— 实测四个实验的
# 实际单笔亏损中位数(-4.64% ~ -10.62%)精确落在这个乘积上, 而不是计划里的 1% 风险预算。
# 只用「反推仓位」永远压不到 1%: 2x 下要求距离 <= 0.5%, 而价格距离不可能低于噪声量级。
# 因此有效手段是**名义敞口封顶**(NOTIONAL_CAP), 而不是继续调 risk_budget。
DIST_MIN = 0.015   # 最小止损距离(价格): 2.5% 对高波动山寨币偏宽, 收紧到 1.5%
DIST_MAX = 0.08

# **组合**名义敞口上限(占权益比重), 按 max_open_trades 分槽。
# 2026-09-14 用 242 笔真实交易做分档扫描(全部按修正后的仓位模型重算):
#     组合敞口    合计盈亏      单笔名义      最差单笔
#       12%      +11.57 USDT     3.0%        -0.31%
#       20%      +19.95          5.0%        -0.51%
#       30%      +28.38          7.5%        -0.76%   <- 最优
#       40%      +22.39         10.0%        -1.02%
#       60%      -30.29         14.0%        -1.43%
#      不封顶     -35.55         14.0%        -1.43%
# 30% 是收益与回撤的拐点: 每槽 7.5% 权益名义(2x 下保证金 15 USDT, 远高于交易所最小额),
# 止损一发的权益代价被压在 ~0.3% 量级。
NOTIONAL_CAP = 0.30


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
    # ---- 捕获率优化: 按利润分档收紧跟踪距离 ----
    # 动机(2026-09-12 实测): 盈利单的价格峰值中位仅 +1~2%, 而 2.6×ATR 的跟踪距离
    # 会把利润几乎全部回吐(实测只吃到峰值的约 1/3)。
    trail_tight_1_mult: float = 1.6    # 浮盈 > 1.6×启动阈值 -> 收紧到 1.6×ATR
    trail_tight_2_mult: float = 1.0    # 浮盈 > 3×启动阈值   -> 收紧到 1.0×ATR
    trail_tight_1_profit_at: float = 0.032   # = TRAIL_START_PROFIT × 1.6
    trail_tight_2_profit_at: float = 0.060   # = TRAIL_START_PROFIT × 3


def stop_price_distance(current_profit: float, leverage: float, atr_frac: float,
                        p: StopParams = StopParams(),
                        peak_profit: float | None = None) -> tuple[float, str]:
    """返回 (价格止损距离 d, 阶段说明). 这是**唯一**的止损真相来源.

    不变量(有单测守护):
        I1. 距离始终落在 [min_price_distance, max_price_distance] 内;
        I2. 一旦进入跟踪阶段, 距离随利润**单调不增**(绝不因浮盈变大而放宽);
        I3. 在 protect 生效后, 距离 <= PROFIT_PROTECT × 价格获利(至少锁定 1-PROFIT_PROTECT 涨幅);
        I4. 返回值换算回 freqtrade 口径后, 权益风险 = d × leverage。

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
        return DIST_MIN, "hard"

    # 初始宽止损: 给趋势发育空间(在 2%~8% 之间钳制)
    d = min(max(atr_frac * p.stop_atr_mult, p.min_price_distance_entry), p.max_price_distance)
    d = min(max(d, DIST_MIN), DIST_MAX)
    stage = "initial"

    if current_profit > p.trail_start_profit:
        # ⚠️ 只使用**峰值浮盈**决定收紧档位(不用当前浮盈做保护) ——
        #    否则浮盈回撤时止损会被放宽, 已锁定的利润会被交回(2026-09-12 实测缺陷)。
        peak = peak_profit if (peak_profit is not None and peak_profit > current_profit) else current_profit
        # 距离上限表: 每档都比上一档更紧, 且全部 >= DIST_MIN(下界由最终钳制保证)
        cap = DIST_MAX
        if peak > p.trail_tight_1_profit_at:
            cap, stage = 0.035, stage + "+fast1"
        if peak > p.trail_tight_2_profit_at:
            cap, stage = 0.030, stage + "+fast2"
        if peak > p.big_profit_1:
            cap, stage = 0.028, stage + "+tight1"
        if peak > p.big_profit_2:
            cap, stage = 0.026, stage + "+tight2"
        if peak > 1.20:
            cap, stage = 0.025, stage + "+tight3"
        d = min(d, cap)
        stage = "trail" + stage[len("initial"):]
    # I1: 最终钳制, 保证距离始终落在 [DIST_MIN, DIST_MAX]
    return min(max(d, DIST_MIN), DIST_MAX), stage


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
    notional_pct: float = 0.0   # 名义敞口占权益比重(2026-09-14 新增, 用于审计)


def plan_position(equity: float, risk_budget: float, price_stop_distance: float,
                  leverage: float, *, min_ratio: float = 0.05, max_ratio: float = 0.35,
                  risk_ceiling: float = 0.015, confidence_mult: float = 1.0,
                  ann_funding: float = 0.0, notional_cap: float = NOTIONAL_CAP,
                  open_trades: int = 0, max_open_trades: int = 1) -> PositionPlan:
    """完整仓位决策. 规则优先级(自上而下, 后者不能放宽前者):

      1. **名义敞口封顶**(notional_cap): stake × lev <= equity × notional_cap;
      2. **组合等风险分配**(修正3): 同时持仓越多, 单笔敞口越小 —— 总敞口不超过 notional_cap;
      3. 按 risk_budget 反推仓位(在有仓位下限时, 它作为上限而非下限);
      4. 止损触发的权益回撤不得超过 risk_ceiling(权益层面硬上限);
      5. 尽量不低于 min_ratio; 若为了满足 4 必须跌破一半下限, 则**放弃这笔交易**。

    为什么把名义敞口放在最前面(2026-09-14 根因):
        单笔权益损失 = 止损距离(价格) x 杠杆, 与仓位反推公式无关。
        旧实现只做 3/4/5 三条, 于是实际亏损中位数落在 risk_ceiling 上(2x 下 -5% 权益),
        而函数却自报 1% —— 名实不符。加了第 1 条之后, 止损一发的代价才真正被约束住。
    """
    lev = max(float(leverage or 1.0), 1.0)
    dist = max(float(price_stop_distance), 1e-6)
    eq = max(float(equity), 0.0)
    floor_n = max(float(min_ratio), 0.0)

    # ---- 0: 距离与敞口必须匹配(2026-09-14 新增, 这是整条链的关键) ----
    # 单笔止损代价 = 名义敞口 x 价格距离, 所以要同时满足:
    #   (a) 名义敞口 <= notional_cap/(同时持仓数);
    #   (b) 名义敞口 >= 交易所最小成交额对应的下限 floor_n。
    # 对固定的距离来说 (a)(b) 可能互相矛盾 —— 距离越宽, 止损一发越贵。
    # 物理上唯一正确的解法是**按 (b) 反推允许的最大距离**, 而不是让下限把敞口顶破上限。
    slots = max(1, int(max_open_trades), int(open_trades) + 1)
    cap_total = max(float(notional_cap), 0.0)
    per_slot = cap_total / slots
    if per_slot < floor_n:
        dist = min(dist, cap_total / floor_n / lev)
        per_slot = floor_n

    # ---- 1 + 2: 名义敞口封顶, 并按同时在持仓数等风险分配 ----
    # 分批下单时, 用本轮最多允许的同时持仓数做预算(而不是当时的持仓数), 否则
    # 第一个信号按 1 份预算下单、后续每个都会被挤到不足最小成交额 -> 只会开出第一笔。
    cap = eq * per_slot
    stake_by_notional = cap / lev

    # ---- 3: 按风险预算反推 ----
    ratio = float(risk_budget) / (dist * lev)
    ratio *= max(confidence_mult, 0.1)
    if ann_funding > 0.2:
        ratio *= 0.8
    elif ann_funding < -0.1:
        ratio *= 1.1
    ratio = min(ratio, max_ratio)
    stake_by_budget = eq * ratio

    stake = min(stake_by_budget, stake_by_notional)
    reason = "notional_cap" if stake_by_notional < stake_by_budget else "risk_budget"

    # ---- 4: 权益层面硬上限 ----
    risk = realized_risk_pct(stake, eq, dist, lev)
    if risk > risk_ceiling:
        stake2 = eq * (risk_ceiling / (dist * lev))
        if stake2 < eq * floor_n:
            # 跌到单笔下限之下就放弃这笔 —— 宁可不下, 不要超风险。
            # 注意: 被拒绝时报 risk=0.0(未持仓则无风险), 与其它失败路径口径一致。
            return PositionPlan(False, 0.0, 0.0, lev, "risk_ceiling_below_min_stake", 0.0)
        stake, risk, reason = stake2, realized_risk_pct(stake2, eq, dist, lev), "capped_by_risk_ceiling"

    notional_pct = (stake * lev / eq) if eq > 0 else 0.0
    return PositionPlan(True, stake, risk, lev, reason, notional_pct)


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
