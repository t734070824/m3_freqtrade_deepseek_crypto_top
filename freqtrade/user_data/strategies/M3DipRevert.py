"""M3-DSH 反向假设策略: 急跌反弹 (与 M3GainersTrend 并行 dry-run 对照).

动机(2026-09-12 用 26~28 万条分钟样本实测):
    未来 60 分钟收益 vs 过去 15 分钟动量:
        跌超 5%  -> +2.379%  (胜率 58.2%)   <-- 本策略要吃的一段
        -5~-2%   -> -0.317%  (43.9%)
        横盘     -> +0.071%  (53.4%)
        涨超 5%  -> +3.901%  (56.5%)
    即**两端**都有动量延续, 中间是均值回归区。
    M3GainersTrend 吃「涨超」那一端, 本策略吃「跌超」那一端, 两者互不重叠, 可并行对照。

设计要点:
    * 不做趋势跟随, 只做超跌反弹; 因此止损必须在时限内完成, 否则逻辑失效
    * 目标是小而快的收益: 反弹 2~4% 即走, 不追求大趋势
    * 资金费率为负(空头付钱给多头)时额外加分 —— 持有还能收钱
    * 与主策略相同的风控内核(stops_core): 风险预算、距离钳制、风险上限

⚠️ 本策略由同一个采集器与同一个 stops_core 驱动, 数据与风控口径完全一致,
   这样两个 dry-run 的对比才具有可比性。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import talib.abstract as ta
from pandas import DataFrame

from freqtrade.persistence import Trade
from freqtrade.strategy import IStrategy, informative

_STRAT_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_STRAT_DIR, os.path.dirname(_STRAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from stops_core import (DIST_MAX, DIST_MIN, StopParams,  # noqa: E402
                        entry_stop_distance, freqtrade_stoploss_value, leverage_for_risk,
                        plan_position, stop_price_distance, stop_rate)

log = logging.getLogger("freqtrade.M3DipRevert")

DATA_DIR = Path(os.environ.get("DSHC_DATA_DIR", "/workspace/data"))
WATCHLIST_PATH = DATA_DIR / "live" / "watchlist.json"
MARKET_DB = DATA_DIR / "m3dsc_market.db"


def _ms_to_utc(ms: int) -> str:
    return datetime.utcfromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S") + " UTC"


class M3DipRevert(IStrategy):
    """急跌反弹: 只做超跌, 快进快出."""

    INTERFACE_VERSION = 3
    timeframe = "5m"
    can_short = False          # 先只验证「超跌做多反弹」这一侧
    process_only_new_candles = True
    use_exit_signal = True
    ignore_roi_if_entry_signal = False
    startup_candle_count = 200

    use_custom_stoploss = True
    position_adjustment_enable = False     # 快进快出, 不做分批
    stoploss = -0.20
    minimal_roi = {}

    # ---- 入场阈值(与实测分桶对齐) ----
    # ⚠️ 实测校准(2026-09-12): 「24h 跌幅 <= -12%」每小时触发上千次, 但那是「长期弱」
    # 而非「急跌」, 会把策略变成接飞刀机器。因此改成: **必须**有一次快跌(15m 或 1h),
    # 24h 跌幅只作为「排除崩盘」的上限, 不再作为入场理由。
    DIP_15M_PCT = -3.5         # 近 15 分钟跌幅(快跌)
    DIP_H1_PCT = -6.0          # 近 1 小时跌幅(更快更急)
    DIP_24H_FLOOR = -45.0      # 24h 跌幅下限: 跌破即为崩盘型, 不接
    MIN_QUOTE_VOL = 15_000_000.0
    MAX_SPREAD_BPS = 15.0
    RSI5_FLOOR = 8.0           # 5m RSI 地板(低于此值通常是崩盘, 直接跳过)
    EXIT_PROFIT = 0.030        # 权益收益 +3% 即离场(约 1% 价格 @3x)
    # ---- 2026-09-15 出场几何修正: 给止损距离设硬上限 ----
    # 问题: ATR 算出的距离中位 4.87% 价格(= -14.6% 权益 @3x), 而止盈只 +3.0% 权益,
    #       设计赔率 1:4.9 —— 要保本需 81% 胜率, 实测只有 62%, 结构上必亏。
    # 依据: 对 116 笔真实交易用 freqtrade 记录的 min_rate/max_rate 做双边界检验
    #       (悲观=碰止损即算亏损 / 乐观=按分钟路径判先后):
    #         现状            悲观 -1.567%/笔  乐观 -0.977%/笔
    #         上限1.5%+止盈3%  悲观 -1.088%/笔  乐观 -0.491%/笔
    #       两个边界一致改善约 +0.5%/笔, 且止损一发的代价从 -14.6% 降到 -4.5% 权益。
    #       注意: 这**不足以让 B 转正** —— 剩下的问题在入场信号, 不在出场。
    MAX_STOP_DIST = 0.015      # 止损距离硬上限(价格口径); 不再让 ATR 把距离推到 3%~5%
    MAX_HOLD_MIN = 240         # 最长 4 小时: 反弹逻辑不成立就撤

    # ---- 风控(与主策略同口径) ----
    leverage_value = 3.0
    max_leverage = 3.0
    STOP_ATR_MULT = 2.0
    TRAIL_ATR_MULT = 1.6
    TRAIL_START_PROFIT = 0.012
    HARD_STOP = -0.045
    PROFIT_PROTECT = 0.55
    RISK_BUDGET = 0.007
    RISK_CEILING = 0.011
    MIN_STAKE_RATIO = 0.03   # 名义口径的单笔下限(交易所最小成交额); 必须 <= NOTIONAL_CAP/max_open_trades(修正1的兼容条件)
    MAX_STAKE_RATIO = 0.25
    TARGET_STAKE_RATIO = 0.18
    # 2026-09-14 根因修正: 单笔权益损失 = 止损距离 x 杠杆。B 用 3x + 4.5% 距离 = 止损一发
    # 就是 -13.5% 权益, 实测单位亏损中位 -10.62% 正好落在这里(而不是计划里的 1.1%)。
    NOTIONAL_CAP = 0.30
    REENTRY_COOLDOWN_MIN = 30

    def bot_start(self, **kwargs: Any) -> None:
        self._wl: dict[str, dict[str, Any]] = {}
        self._wl_ms = 0
        self._live_funding: dict[str, dict[str, float]] = {}
        self._diag: dict[str, dict[str, float]] = {}
        self._last_exit: dict[str, int] = {}
        self._sig_stat: dict[str, int] = {"pairs": 0, "long": 0}
        log.info("[DIP] 急跌反弹策略启动 (%s)", _ms_to_utc(int(time.time() * 1000)))
        log.info("[DIP] 门槛: 15m<=%.1f%% 或 1h<=%.1f%% 或 24h<=%.1f%% | 止盈%.1f%% 硬止损%.1f%% "
                 "风险预算%.2f%% 上限%.2f%% 杠杆%.1fx 最长持有%d分钟",
                 self.DIP_15M_PCT, self.DIP_H1_PCT, self.DIP_24H_FLOOR, self.EXIT_PROFIT * 100,
                 self.HARD_STOP * 100, self.RISK_BUDGET * 100, self.RISK_CEILING * 100,
                 self.leverage_value, self.MAX_HOLD_MIN)

    # ---------------------------------------------------------------- 数据
    def informative_pairs(self):
        return [(p, "1h") for p in self.dp.current_whitelist()]

    @informative("1h")
    def populate_indicators_1h(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        # 1h 趋势: 只在「长期上升趋势中的回调」里抄底, 避免接飞刀
        # 不接飞刀: 只要求「不是明确的下跌趋势」—— 价格不低于 1h EMA50 的 97%,
        # 且 EMA21 未明显下穿 EMA50。这样在急跌当天(EMA 尚未转空)仍能入场。
        dataframe["up_trend"] = ((dataframe["close"] > dataframe["ema50"] * 0.97) &
                                 (dataframe["ema21"] > dataframe["ema50"] * 0.985)).astype(float)
        return dataframe

    def populate_indicators(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        dataframe["ema21"] = ta.EMA(dataframe, timeperiod=21)
        dataframe["ema50"] = ta.EMA(dataframe, timeperiod=50)
        dataframe["rsi"] = ta.RSI(dataframe, timeperiod=14)
        dataframe["atr"] = ta.ATR(dataframe, timeperiod=14)
        dataframe["atr_pct"] = dataframe["atr"] / dataframe["close"] * 100.0
        dataframe["mom_15m"] = dataframe["close"].pct_change(3) * 100.0     # 15 分钟
        dataframe["mom_1h"] = dataframe["close"].pct_change(12) * 100.0     # 1 小时
        dataframe["mom_4h"] = dataframe["close"].pct_change(48) * 100.0
        dataframe["vol_sma"] = dataframe["volume"].rolling(20).mean()
        dataframe["vol_ratio"] = dataframe["volume"] / dataframe["vol_sma"].replace(0, float("nan"))
        dataframe["low_5"] = dataframe["low"].rolling(5).min()
        dataframe["reclaim"] = (dataframe["close"] > dataframe["low_5"] * 1.004).astype(float)
        dataframe["low_3"] = dataframe["low"].rolling(3).min()
        self._attach_external(dataframe, metadata)
        return dataframe

    def _attach_external(self, dataframe: DataFrame, metadata: dict) -> None:
        pair = metadata.get("pair", "")
        sym = pair.split("/")[0] + "USDT" if pair else ""
        self._refresh_watchlist()
        wl = self._wl.get(sym, {})
        dataframe["m3_change_24h"] = float(wl.get("change_24h", 0.0) or 0.0)
        dataframe["m3_funding_ann"] = float(wl.get("funding_ann", 0.0) or 0.0)
        dataframe["m3_score"] = float(wl.get("score", 0.0) or 0.0)
        dataframe["m3_quote_vol"] = float(wl.get("quote_vol", 0.0) or 0.0)
        dataframe["m3_spread_bps"] = float(wl.get("spread_bps", 0.0) or 0.0)
        dataframe["m3_in_pool"] = bool(wl)

    def _refresh_watchlist(self) -> None:
        now = int(time.time() * 1000)
        if now - self._wl_ms < 20_000 and self._wl:
            return
        try:
            raw = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
            self._wl = {c["symbol"]: c for c in raw.get("candidates", []) if c.get("symbol")}
            self._wl_ms = now
        except Exception as exc:  # noqa: BLE001
            if now - self._wl_ms > 300_000:
                log.warning("[DIP] watchlist 读取失败: %s", exc)
                self._wl_ms = now

    # ---------------------------------------------------------------- 入场
    def populate_entry_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        cols = dataframe.columns
        pair = metadata.get("pair", "")
        up1h = dataframe.get("up_trend_1h", pd.Series(1.0, index=dataframe.index)).fillna(1.0)

        # 急跌条件: **必须**有快跌(15m 或 1h), 24h 跌幅只用于排除崩盘(见下方 floor)
        dip = ((dataframe["mom_15m"] <= self.DIP_15M_PCT)
               | (dataframe["mom_1h"] <= self.DIP_H1_PCT))
        liquid = ((dataframe["m3_quote_vol"] >= self.MIN_QUOTE_VOL)
                  & (dataframe["m3_spread_bps"] <= self.MAX_SPREAD_BPS))
        # 反弹确认: 出现下影线后收回 / 放量 / RSI 从极低位回升
        # 弱确认: 不在当根 K 线的新低(避免接下落的刀), 或出现放量/收阳
        bounce = ((dataframe["close"] > dataframe["low_3"] * 1.002)
                  | (dataframe["vol_ratio"] > 1.5))

        # 入场放宽说明(2026-09-12 实测漏斗): 原条件在 100 次评估中 0 信号,
        # 瓶颈是 up1h(32%) 与 rsi_weak(33%) 的乘积 —— 无法产生可评估样本。
        # 放宽为「1h 不能是明确下跌趋势」(即不接飞刀), 并要求 RSI 仍在 45 以下。
        # ⚠️ 设计取舍(2026-09-12): 实测每次评估只有 ~1% 满足「急跌」, 再叠加
        # 趋势/RSI/反弹确认后信号率低于 1 笔/天, 无法在合理时间内验证假设。
        # 因此只保留「急跌 + 流动性 + 不在新低 + 费率不过高 + 非崩盘」,
        # 其余判断交给风控(2×ATR 止损、1.1% 风险上限、4 小时超时)。
        ok = (dip & liquid & bounce
              & (dataframe["rsi"] >= self.RSI5_FLOOR)
              & (dataframe["m3_funding_ann"] <= 0.30)      # 拒绝高费率(做多成本高)
              & (dataframe["m3_change_24h"] > self.DIP_24H_FLOOR)   # 排除崩盘型标的
              & (dataframe["volume"] > 0))
        dataframe.loc[ok, ["enter_long", "enter_tag"]] = (1, "dip_revert")

        # ---- 漏斗统计: 用数据定位「为何没有信号」 ----
        i = len(dataframe) - 1
        row = dataframe.iloc[i]
        gates = {
            "dip": bool(dip.iloc[i]),
            "liquid": bool(liquid.iloc[i]),
            "bounce": bool(bounce.iloc[i]),
            "up1h": bool(up1h.iloc[i] >= 0.5),
            "rsi_floor": bool(row.get("rsi", 50) >= self.RSI5_FLOOR),
            "rsi_weak": bool(row.get("rsi", 50) < 45),
            "funding": bool((row.get("m3_funding_ann", 0) or 0) <= 0.30),
            "floor": bool((row.get("m3_change_24h", 0) or 0) > self.DIP_24H_FLOOR),
        }
        st = self._sig_stat
        st["pairs"] = st.get("pairs", 0) + 1
        st["long"] = st.get("long", 0) + int(bool(ok.iloc[i]))
        acc = st.setdefault("gates", {})
        for k, v in gates.items():
            acc[k] = acc.get(k, 0) + int(bool(v))
        if st["pairs"] % 100 == 0:
            n = st["pairs"]
            log.info("[DIP] 评估 %d 次, 入场信号 %d 次 | 各门槛通过率: %s", n, st["long"],
                     "  ".join("%s=%.0f%%" % (k, acc.get(k, 0) / n * 100) for k in gates))
        return dataframe

    def populate_exit_trend(self, dataframe: DataFrame, metadata: dict) -> DataFrame:
        # 反弹到 1h 均线附近即视为目标达成; 其余交给 custom_exit
        target = dataframe["close"] >= dataframe.get(
            "ema21_1h", pd.Series(float("inf"), index=dataframe.index))
        dataframe.loc[target, ["exit_long", "exit_tag"]] = (1, "revert_target")
        return dataframe

    # ---------------------------------------------------------------- 仓位/杠杆
    def leverage(self, pair: str, current_time: datetime, current_rate: float,
                 proposed_leverage: float, max_leverage: float, entry_tag: Optional[str],
                 side: str, **kwargs: Any) -> float:
        atr_frac = 0.03
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            if df is not None and len(df):
                atr_frac = max(float(df.iloc[-1].get("atr_pct", 3.0) or 3.0) / 100.0, 0.005)
        except Exception:  # noqa: BLE001
            pass
        stop_dist = min(entry_stop_distance(atr_frac, self.STOP_ATR_MULT), self.MAX_STOP_DIST)
        safe = leverage_for_risk(stop_dist, max_leverage=max_leverage,
                                 base_leverage=min(self.leverage_value, self.max_leverage),
                                 target_ratio=self.TARGET_STAKE_RATIO)
        return float(max(1.0, min(self.leverage_value, max_leverage, safe)))

    def custom_stake_amount(self, pair: str, current_time: datetime, current_rate: float,
                            proposed_stake: float, min_stake: Optional[float],
                            max_stake: float, leverage: float, entry_tag: Optional[str],
                            side: str, **kwargs: Any) -> float:
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            if row is None:
                return proposed_stake
            atr_pct = float(row.get("atr_pct", 3.0) or 3.0)
            stop_dist = min(entry_stop_distance(atr_pct / 100.0, self.STOP_ATR_MULT),
                            self.MAX_STOP_DIST)
            wallet = self.wallets.get_total_stake_amount() if self.wallets else 0.0
            if wallet <= 0:
                return proposed_stake
            # 跌得越深、费率越负(持有收钱), 越值得下注
            ann = float(row.get("m3_funding_ann", 0.0) or 0.0)
            conf = 1.15 if ann < -0.05 else 1.0
            plan = plan_position(equity=wallet, risk_budget=self.RISK_BUDGET,
                                 price_stop_distance=stop_dist, leverage=leverage,
                                 min_ratio=self.MIN_STAKE_RATIO, max_ratio=self.MAX_STAKE_RATIO,
                                 risk_ceiling=self.RISK_CEILING, confidence_mult=conf,
                                 ann_funding=ann, notional_cap=self.NOTIONAL_CAP,
                                 open_trades=len(Trade.get_open_trades()),
                                 max_open_trades=self.config.get("max_open_trades", 1))
            if not plan.ok:
                log.warning("[DIP] %s 放弃: %s", pair, plan.reason)
                return 0.0
            stake = min(plan.stake, max_stake * 0.9 if max_stake else plan.stake)
            # 2026-09-14 修正: 原实现在 stake < min_stake 时把仓位顶回 min_stake, 这会
            # **绕过 risk_ceiling 与名义敞口封顶** —— 典型的「为了能成交而放大仓位」。
            if min_stake and stake < min_stake:
                log.info("[DIP] %s 跳过: 计算仓位 %.2f 低于交易所最小 %.2f, 不为凑单放大仓位",
                         pair, stake, min_stake)
                return 0.0
            self._diag[pair] = {"stop_dist": stop_dist, "leverage": float(leverage),
                                "stake": float(stake), "design_risk": float(plan.risk_pct),
                                "notional_pct": float(plan.notional_pct),
                                "ann": ann}
            log.info("[DIP] %s 仓位: 止损距离%.2f%% 杠杆%.1f -> 保证金%.2f (名义%.2f=权益%.1f%%, "
                     "止损一发风险%.2f%%) [%s]", pair, stop_dist * 100, leverage, stake,
                     stake * leverage, plan.notional_pct * 100, plan.risk_pct * 100, plan.reason)
            return float(max(stake, 0.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("[DIP] 仓位异常 %s: %s", pair, exc)
            return proposed_stake

    def custom_stoploss(self, pair: str, trade: Trade, current_time: datetime,
                        current_rate: float, current_profit: float, after_fill: bool,
                        **kwargs: Any) -> Optional[float]:
        lev = float(trade.leverage or 1.0)
        try:
            df, _ = self.dp.get_analyzed_dataframe(pair, self.timeframe)
            row = df.iloc[-1] if df is not None and len(df) else None
            atr_frac = float(row.get("atr_pct", 3.0) or 3.0) / 100.0 if row is not None else 0.03
        except Exception:  # noqa: BLE001
            atr_frac = 0.03
        # max_price_distance 一起封顶: 否则浮盈后跟踪距离又放开回 ATR×倍数
        p = StopParams(max_price_distance=self.MAX_STOP_DIST,
                       stop_atr_mult=self.STOP_ATR_MULT, trail_atr_mult=self.TRAIL_ATR_MULT,
                       trail_start_profit=self.TRAIL_START_PROFIT, hard_stop=self.HARD_STOP,
                       profit_protect=self.PROFIT_PROTECT)
        d, stage = stop_price_distance(current_profit, lev, atr_frac, p)
        stop_p = stop_rate(current_rate, d, bool(trade.is_short))
        val = freqtrade_stoploss_value(current_rate, stop_p, lev, bool(trade.is_short))
        return float(val) if val > 0 else None

    def custom_exit(self, pair: str, trade: Trade, current_time: datetime, current_rate: float,
                    current_profit: float, **kwargs: Any) -> Optional[str]:
        hold_min = (current_time - trade.open_date_utc).total_seconds() / 60.0
        if current_profit >= self.EXIT_PROFIT:
            return "revert_take_profit"
        if hold_min >= self.MAX_HOLD_MIN:
            return "revert_timeout"
        return None

    def confirm_trade_exit(self, pair: str, trade: Trade, order_type: str, amount: float,
                           rate: float, time_in_force: str, exit_reason: str,
                           current_time: datetime, **kwargs: Any) -> bool:
        self._last_exit[pair] = int(current_time.timestamp() * 1000)
        d = self._diag.pop(pair, None)
        ratio = trade.calc_profit_ratio(rate) or 0.0
        if d:
            log.info("[DIP][平仓] %s 原因=%s | 设计风险%.2f%% 实际%.2f%% | 止损距离%.2f%% "
                     "杠杆%.1f 保证金%.2f | 持有%.0f分钟",
                     pair, exit_reason, d["design_risk"] * 100, ratio * 100,
                     d["stop_dist"] * 100, d["leverage"], d["stake"],
                     (current_time - trade.open_date_utc).total_seconds() / 60.0)
        return True
